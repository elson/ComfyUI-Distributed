import asyncio
import hashlib
import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path


class _FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status


class _FakePart:
    """One multipart part, either a scalar field or the streamed video body."""

    def __init__(self, name, value=b"", filename=None, chunk_size=None):
        self.name = name
        self.filename = filename
        self._value = value
        self._offset = 0
        self._chunk_size = chunk_size

    async def read(self, decode=False):
        return self._value

    async def read_chunk(self, size):
        size = self._chunk_size or size
        chunk = self._value[self._offset:self._offset + size]
        self._offset += len(chunk)
        return chunk


class _FakeMultipartReader:
    def __init__(self, parts):
        self._parts = list(parts)

    async def next(self):
        if not self._parts:
            return None
        return self._parts.pop(0)


class _FakeRequest:
    def __init__(self, parts=None, headers=None, content_type="multipart/form-data"):
        self._parts = parts or []
        self.headers = headers or {}
        self.content_type = content_type

    async def multipart(self):
        return _FakeMultipartReader(self._parts)


class _Routes:
    def post(self, _path):
        def _decorator(fn):
            return fn

        return _decorator

    def get(self, _path):
        def _decorator(fn):
            return fn

        return _decorator


def _load_job_routes_module(output_dir):
    module_path = Path(__file__).resolve().parents[2] / "api" / "job_routes.py"
    package_name = "dist_api_jobroutes_testpkg"

    for mod_name in list(sys.modules):
        if mod_name == package_name or mod_name.startswith(f"{package_name}."):
            del sys.modules[mod_name]

    for suffix in ("", ".api", ".utils"):
        pkg = types.ModuleType(f"{package_name}{suffix}")
        pkg.__path__ = []
        sys.modules[f"{package_name}{suffix}"] = pkg

    server_module = types.ModuleType("server")
    prompt_server = types.SimpleNamespace(
        routes=_Routes(),
        distributed_jobs_lock=asyncio.Lock(),
        distributed_pending_jobs={},
    )
    server_module.PromptServer = types.SimpleNamespace(instance=prompt_server)
    sys.modules["server"] = server_module

    folder_paths_module = types.ModuleType("folder_paths")
    folder_paths_module.get_output_directory = lambda: str(output_dir)
    folder_paths_module.get_annotated_filepath = lambda name: str(Path(output_dir) / name)
    sys.modules["folder_paths"] = folder_paths_module

    logging_module = types.ModuleType(f"{package_name}.utils.logging")
    logging_module.debug_log = lambda *_a, **_k: None
    sys.modules[f"{package_name}.utils.logging"] = logging_module

    image_module = types.ModuleType(f"{package_name}.utils.image")
    image_module.pil_to_tensor = lambda *_a, **_k: None
    image_module.ensure_contiguous = lambda value: value
    sys.modules[f"{package_name}.utils.image"] = image_module

    network_module = types.ModuleType(f"{package_name}.utils.network")

    async def _handle_api_error(_request, error, status=500):
        return _FakeResponse({"status": "error", "message": str(error)}, status=status)

    network_module.handle_api_error = _handle_api_error
    sys.modules[f"{package_name}.utils.network"] = network_module

    constants_module = types.ModuleType(f"{package_name}.utils.constants")
    constants_module.JOB_INIT_GRACE_PERIOD = 0.2
    constants_module.MEMORY_CLEAR_DELAY = 0
    sys.modules[f"{package_name}.utils.constants"] = constants_module

    queue_orch_module = types.ModuleType(f"{package_name}.api.queue_orchestration")
    queue_orch_module.ensure_distributed_state = lambda: prompt_server
    queue_orch_module.orchestrate_distributed_execution = lambda *_a, **_k: None
    sys.modules[f"{package_name}.api.queue_orchestration"] = queue_orch_module

    queue_request_module = types.ModuleType(f"{package_name}.api.queue_request")
    queue_request_module.parse_queue_request_payload = lambda *_a, **_k: None
    sys.modules[f"{package_name}.api.queue_request"] = queue_request_module

    spec = importlib.util.spec_from_file_location(f"{package_name}.api.job_routes", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)

    module.web = types.SimpleNamespace(
        json_response=lambda payload, status=200: _FakeResponse(payload, status=status)
    )
    module._prompt_server = prompt_server
    return module


def _video_parts(body, job_id="vid-job", worker_id="worker-a", basename="clip_00001.mp4",
                 md5=None, chunk_size=None):
    if md5 is None:
        md5 = hashlib.md5(body).hexdigest()
    parts = [
        _FakePart("job_id", job_id.encode()),
        _FakePart("worker_id", worker_id.encode()),
        _FakePart("basename", basename.encode()),
    ]
    if md5 is not False:
        parts.append(_FakePart("md5", md5.encode()))
    parts.append(_FakePart("is_last", b"true"))
    parts.append(_FakePart("video", body, filename=basename, chunk_size=chunk_size))
    return parts


class SafeFilenameTests(unittest.TestCase):
    """The name comes off the wire, so it must not be able to escape the output dir."""

    def setUp(self):
        self.module = _load_job_routes_module("/tmp")

    def test_traversal_is_reduced_to_a_basename(self):
        self.assertEqual(
            self.module._safe_video_filename("../../etc/passwd.mp4", "j", "w"),
            "passwd.mp4",
        )

    def test_absolute_path_is_reduced_to_a_basename(self):
        self.assertEqual(
            self.module._safe_video_filename("/var/tmp/evil.mp4", "j", "w"),
            "evil.mp4",
        )

    def test_windows_separators_are_handled(self):
        self.assertEqual(
            self.module._safe_video_filename(r"C:\\temp\\clip.mkv", "j", "w"),
            "clip.mkv",
        )

    def test_non_video_extension_falls_back(self):
        self.assertEqual(
            self.module._safe_video_filename("payload.exe", "job1", "worker1"),
            "distributed_job1_worker1.mp4",
        )

    def test_dotdot_alone_falls_back(self):
        self.assertEqual(
            self.module._safe_video_filename("..", "job1", "worker1"),
            "distributed_job1_worker1.mp4",
        )

    def test_fallback_name_cannot_contain_separators_from_ids(self):
        name = self.module._safe_video_filename(None, "../evil", "w")
        self.assertNotIn("/", name)


class JobCompleteVideoEndpointTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.output_dir = Path(self._tmp.name)
        self.module = _load_job_routes_module(self.output_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, request, job_id="vid-job", with_queue=True):
        queue = asyncio.Queue()
        if with_queue:
            self.module._prompt_server.distributed_pending_jobs = {job_id: queue}
        else:
            self.module._prompt_server.distributed_pending_jobs = {}
        response = asyncio.run(self.module.job_complete_video_endpoint(request))
        return response, queue

    def test_streams_file_to_output_dir_and_queues_the_path(self):
        body = os.urandom(200_000)
        response, queue = self._run(_FakeRequest(_video_parts(body, chunk_size=4096)))

        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["status"], "success")
        self.assertEqual(response.payload["bytes"], len(body))

        written = self.output_dir / "clip_00001.mp4"
        self.assertTrue(written.exists())
        self.assertEqual(written.read_bytes(), body)

        item = queue.get_nowait()
        self.assertEqual(item["video_path"], str(written))
        self.assertEqual(item["worker_id"], "worker-a")
        self.assertIsNone(item["tensor"])
        self.assertIsNone(item["image_index"])
        self.assertTrue(item["is_last"])

    def test_no_part_file_is_left_behind(self):
        body = b"x" * 1024
        self._run(_FakeRequest(_video_parts(body)))
        self.assertEqual([p.name for p in self.output_dir.glob("*.part")], [])

    def test_md5_mismatch_is_rejected_and_nothing_is_kept(self):
        body = b"y" * 2048
        request = _FakeRequest(_video_parts(body, md5="0" * 32))
        response, queue = self._run(request)

        self.assertEqual(response.status, 400)
        self.assertIn("md5 mismatch", str(response.payload["message"]))
        self.assertEqual(sorted(p.name for p in self.output_dir.iterdir()), [])
        self.assertTrue(queue.empty())

    def test_second_file_with_the_same_name_does_not_overwrite(self):
        first = b"a" * 512
        second = b"b" * 512
        self._run(_FakeRequest(_video_parts(first)))
        self._run(_FakeRequest(_video_parts(second)))

        self.assertEqual((self.output_dir / "clip_00001.mp4").read_bytes(), first)
        self.assertEqual((self.output_dir / "clip_00001_1.mp4").read_bytes(), second)

    def test_traversal_filename_stays_in_the_output_dir(self):
        body = b"z" * 256
        self._run(_FakeRequest(_video_parts(body, basename="../../escaped.mp4")))
        self.assertTrue((self.output_dir / "escaped.mp4").exists())

    def test_oversized_content_length_is_rejected_before_reading(self):
        request = _FakeRequest(
            _video_parts(b"q" * 16),
            headers={"content-length": str(self.module.MAX_VIDEO_PAYLOAD_SIZE + 1)},
        )
        response, _queue = self._run(request)
        self.assertEqual(response.status, 413)
        self.assertEqual(list(self.output_dir.iterdir()), [])

    def test_non_multipart_body_is_rejected(self):
        response, _queue = self._run(_FakeRequest([], content_type="application/json"))
        self.assertEqual(response.status, 400)
        self.assertIn("multipart", str(response.payload["message"]))

    def test_missing_video_part_is_rejected(self):
        parts = [
            _FakePart("job_id", b"vid-job"),
            _FakePart("worker_id", b"worker-a"),
        ]
        response, _queue = self._run(_FakeRequest(parts))
        self.assertEqual(response.status, 400)
        self.assertIn("non-empty file part", str(response.payload["message"]))

    def test_video_part_before_ids_is_rejected(self):
        """job_id has to arrive first; the handler needs it to name the file."""
        parts = [_FakePart("video", b"data", filename="clip.mp4")]
        response, _queue = self._run(_FakeRequest(parts))
        self.assertEqual(response.status, 400)
        self.assertIn("must precede", str(response.payload["message"]))

    def test_unknown_job_returns_404_but_keeps_the_file(self):
        body = b"w" * 128
        response, _queue = self._run(_FakeRequest(_video_parts(body)), with_queue=False)
        self.assertEqual(response.status, 404)
        # The bytes were already received; discarding them would lose the render.
        self.assertTrue((self.output_dir / "clip_00001.mp4").exists())


if __name__ == "__main__":
    unittest.main()
