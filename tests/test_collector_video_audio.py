"""Audio wired alongside a collected video.

Wiring audio into Video Combine puts it inside the file. Wiring it into the collector
means the caller wants it on the master's audio output, and the video path used to send
only the file, so that audio vanished with no error.
"""

import asyncio
import json

from test_collector_list_inputs import _load_collector_module
from test_collector_video import _FakeSession, _collector_with_session, _write_clip


def test_audio_rides_along_on_the_video_request(tmp_path):
    session = _FakeSession()
    module, collector = _collector_with_session(session)
    module.encode_audio_payload = lambda audio: {"encoded": audio is not None}
    path, _digest = _write_clip(tmp_path)

    asyncio.run(
        collector.send_video_to_master(
            (False, [path]), {"waveform": "x"}, "vid-job", "http://master", "worker-a"
        )
    )

    assert len(session.posts) == 1, "audio must not cost a second request"
    fields = session.posts[0]["data"].as_dict()
    assert json.loads(fields["audio"]) == {"encoded": True}


def test_no_audio_means_no_audio_field(tmp_path):
    session = _FakeSession()
    module, collector = _collector_with_session(session)
    module.encode_audio_payload = lambda _audio: None
    path, _digest = _write_clip(tmp_path)

    asyncio.run(
        collector.send_video_to_master(
            (False, [path]), None, "vid-job", "http://master", "worker-a"
        )
    )

    assert "audio" not in session.posts[0]["data"].as_dict()


def test_the_worker_sends_audio_with_the_video_rather_than_dropping_it(tmp_path):
    """The whole point: a collector with both wired must not lose the audio."""
    import torch

    session = _FakeSession()
    module, collector = _collector_with_session(session)
    module.encode_audio_payload = lambda audio: None if audio is None else {"sr": 48000}
    path, _digest = _write_clip(tmp_path)

    asyncio.run(
        collector.execute(
            images=None,
            audio={"waveform": torch.ones(1, 2, 4), "sample_rate": 48000},
            filenames=(False, [path]),
            multi_job_id="vid-job",
            is_worker=True,
            master_url="http://master",
            worker_id="worker-a",
        )
    )

    urls = [post["url"] for post in session.posts]
    assert urls == ["http://master/distributed/job_complete_video"]
    assert json.loads(session.posts[0]["data"].as_dict()["audio"]) == {"sr": 48000}
