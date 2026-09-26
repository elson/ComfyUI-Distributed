"""Collecting a finished video file instead of every frame as a base64 PNG."""

import asyncio
import hashlib
import types

import pytest

from test_collector_list_inputs import _load_collector_module


class _FakeResponse:
    def __init__(self, status=200):
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _FakeSession:
    """Records every post and can be told to fail the first N attempts."""

    def __init__(self, fail_times=0):
        self.posts = []
        self._fail_times = fail_times

    def post(self, url, json=None, data=None, timeout=None):
        self.posts.append(
            {
                "url": url,
                "json": json,
                "data": data,
                "timeout": getattr(timeout, "total", timeout),
            }
        )
        if self._fail_times > 0:
            self._fail_times -= 1
            return _FakeResponse(status=500)
        return _FakeResponse()


def _collector_with_session(session):
    module = _load_collector_module()

    async def _fake_get_client_session():
        return session

    module.get_client_session = _fake_get_client_session
    return module, module.DistributedCollectorNode()


def _write_clip(tmp_path, name="clip_00001.mp4", body=b"not really an mp4"):
    path = tmp_path / name
    path.write_bytes(body)
    return str(path), hashlib.md5(body).hexdigest()


# ---------------------------------------------------------------------------
# Node shape
# ---------------------------------------------------------------------------

def test_collector_exposes_video_as_optional_input():
    module = _load_collector_module()
    input_types = module.DistributedCollectorNode.INPUT_TYPES()
    assert "video" not in input_types["required"]
    assert input_types["optional"]["video"][0] == "VHS_FILENAMES"


def test_collector_returns_video_as_a_third_output():
    module = _load_collector_module()
    node = module.DistributedCollectorNode
    assert node.RETURN_TYPES == ("IMAGE", "AUDIO", "VHS_FILENAMES")
    assert node.RETURN_NAMES == ("images", "audio", "video")


def test_collector_rejects_a_run_with_no_media_at_all():
    collector = _load_collector_module().DistributedCollectorNode()
    with pytest.raises(ValueError, match="image, audio, or video"):
        collector.run(images=None, audio=None, video=None)


def test_pass_through_returns_the_video_unchanged():
    collector = _load_collector_module().DistributedCollectorNode()
    filenames = (True, ["/out/clip_00001.mp4"])

    images, audio, video = collector.run(images=None, video=[filenames], multi_job_id=[""])

    assert images is None
    assert video == (True, ["/out/clip_00001.mp4"])


# ---------------------------------------------------------------------------
# VHS_FILENAMES handling
# ---------------------------------------------------------------------------

def test_the_last_filename_is_the_one_sent():
    """Video Combine appends the muxed file last, and the -audio variant after it."""
    collector = _load_collector_module().DistributedCollectorNode()
    video = (True, ["/out/clip_00001.mp4", "/out/clip_00001-audio.mp4"])
    assert collector._video_path_from_filenames(video) == "/out/clip_00001-audio.mp4"


def test_empty_filenames_is_an_error_not_a_silent_skip():
    """(save_output, []) happens for a zero-frame batch and mid-Meta Batch."""
    collector = _load_collector_module().DistributedCollectorNode()
    with pytest.raises(ValueError, match="empty video input"):
        collector._video_path_from_filenames((True, []))


def test_malformed_video_input_is_rejected():
    collector = _load_collector_module().DistributedCollectorNode()
    with pytest.raises(ValueError, match="VHS_FILENAMES"):
        collector._normalize_video_input(["just-a-string"])


def test_list_video_input_is_collapsed():
    collector = _load_collector_module().DistributedCollectorNode()
    assert collector._normalize_video_input([(False, ["/tmp/a.mp4"])]) == (
        False,
        ["/tmp/a.mp4"],
    )


# ---------------------------------------------------------------------------
# Worker send
# ---------------------------------------------------------------------------

def test_worker_sends_one_multipart_request_with_the_file(tmp_path):
    session = _FakeSession()
    module, collector = _collector_with_session(session)
    path, digest = _write_clip(tmp_path)

    asyncio.run(
        collector.send_video_to_master(
            (False, [path]), "vid-job", "http://master", "worker-a"
        )
    )

    assert len(session.posts) == 1
    post = session.posts[0]
    assert post["url"] == "http://master/distributed/job_complete_video"
    # Multipart, not a JSON envelope: the point is not to hold the clip in memory.
    assert post["json"] is None
    fields = post["data"].as_dict()
    assert fields["job_id"] == "vid-job"
    assert fields["worker_id"] == "worker-a"
    assert fields["basename"] == "clip_00001.mp4"
    assert fields["md5"] == digest
    assert fields["is_last"] == "true"
    # The file is handed over as an open handle, so aiohttp streams it off disk.
    assert hasattr(fields["video"], "read")


def test_worker_does_not_also_send_frames_when_a_video_is_present(tmp_path):
    session = _FakeSession()
    module, collector = _collector_with_session(session)
    module.encode_audio_payload = lambda _audio: None
    path, _digest = _write_clip(tmp_path)

    import torch

    asyncio.run(
        collector.execute(
            images=torch.zeros(4, 2, 2, 3),
            audio=None,
            video=(False, [path]),
            multi_job_id="vid-job",
            is_worker=True,
            master_url="http://master",
            worker_id="worker-a",
        )
    )

    urls = [post["url"] for post in session.posts]
    assert urls == ["http://master/distributed/job_complete_video"]


def test_a_missing_file_fails_before_any_request(tmp_path):
    session = _FakeSession()
    _module, collector = _collector_with_session(session)

    with pytest.raises(ValueError, match="does not exist"):
        asyncio.run(
            collector.send_video_to_master(
                (False, [str(tmp_path / "gone.mp4")]),
                "vid-job",
                "http://master",
                "worker-a",
            )
        )
    assert session.posts == []


def test_send_is_retried_because_it_carries_the_whole_job(tmp_path, monkeypatch):
    session = _FakeSession(fail_times=2)
    module, collector = _collector_with_session(session)
    path, _digest = _write_clip(tmp_path)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(module.asyncio, "sleep", lambda _delay: real_sleep(0))

    asyncio.run(
        collector.send_video_to_master(
            (False, [path]), "vid-job", "http://master", "worker-a"
        )
    )

    assert len(session.posts) == 3


def test_send_gives_up_after_the_attempt_limit(tmp_path, monkeypatch):
    session = _FakeSession(fail_times=99)
    module, collector = _collector_with_session(session)
    path, _digest = _write_clip(tmp_path)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(module.asyncio, "sleep", lambda _delay: real_sleep(0))

    with pytest.raises(RuntimeError):
        asyncio.run(
            collector.send_video_to_master(
                (False, [path]), "vid-job", "http://master", "worker-a"
            )
        )

    assert len(session.posts) == module.VIDEO_SEND_ATTEMPTS


# ---------------------------------------------------------------------------
# Master combine
# ---------------------------------------------------------------------------

def test_combine_videos_is_none_when_there_is_no_video():
    collector = _load_collector_module().DistributedCollectorNode()
    assert collector._combine_videos(None, {}, ["worker-a"]) is None


def test_combine_videos_puts_master_first_then_workers_in_configured_order():
    collector = _load_collector_module().DistributedCollectorNode()
    combined = collector._combine_videos(
        (True, ["/out/master.mp4"]),
        {"worker-b": "/out/b.mp4", "worker-a": "/out/a.mp4"},
        ["worker-a", "worker-b"],
    )
    assert combined == (True, ["/out/master.mp4", "/out/a.mp4", "/out/b.mp4"])


def test_combine_videos_appends_unexpected_workers_sorted():
    collector = _load_collector_module().DistributedCollectorNode()
    combined = collector._combine_videos(
        None,
        {"worker-a": "/out/a.mp4", "zeta": "/out/z.mp4", "alpha": "/out/al.mp4"},
        ["worker-a"],
    )
    assert combined == (True, ["/out/a.mp4", "/out/al.mp4", "/out/z.mp4"])


def test_master_returns_a_collected_video_as_the_third_output():
    module = _load_collector_module()
    collector = module.DistributedCollectorNode()

    module.prompt_server.distributed_jobs_lock = asyncio.Lock()
    queue = asyncio.Queue()
    queue.put_nowait(
        {
            "tensor": None,
            "worker_id": "worker-a",
            "image_index": None,
            "is_last": True,
            "audio": None,
            "video_path": "/out/from-worker.mp4",
        }
    )
    module.prompt_server.distributed_pending_jobs = {"vid-job": queue}

    images, _audio, video = asyncio.run(
        collector.execute(
            images=None,
            audio=None,
            video=None,
            multi_job_id="vid-job",
            enabled_worker_ids='["worker-a"]',
            delegate_only=True,
        )
    )

    assert images is None
    assert video == (True, ["/out/from-worker.mp4"])


def test_a_video_item_does_not_disturb_image_assembly():
    """Video paths are kept out of worker_images: the assembler counts one row per item."""
    import torch

    module = _load_collector_module()
    collector = module.DistributedCollectorNode()

    module.prompt_server.distributed_jobs_lock = asyncio.Lock()
    queue = asyncio.Queue()
    queue.put_nowait(
        {
            "tensor": torch.zeros(1, 2, 2, 3),
            "worker_id": "worker-a",
            "image_index": 0,
            "is_last": False,
            "audio": None,
        }
    )
    queue.put_nowait(
        {
            "tensor": None,
            "worker_id": "worker-a",
            "image_index": None,
            "is_last": True,
            "audio": None,
            "video_path": "/out/side-car.mp4",
        }
    )
    module.prompt_server.distributed_pending_jobs = {"mixed-job": queue}

    images, _audio, video = asyncio.run(
        collector.execute(
            images=None,
            audio=None,
            video=None,
            multi_job_id="mixed-job",
            enabled_worker_ids='["worker-a"]',
            delegate_only=True,
        )
    )

    assert images is not None
    assert images.shape[0] == 1
    assert video == (True, ["/out/side-car.mp4"])
