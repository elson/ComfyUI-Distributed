"""DistributedSaveVideo: the video counterpart of SaveImage.

A collected clip is staged in the temp dir by the job_complete_video route and is not
saved until this node publishes it, the same way a collected IMAGE is not written until a
SaveImage node writes it. Nothing here decodes or re-encodes, and nothing consumes the
staged file - other nodes may be reading the same path.
"""

import importlib.util
import sys
import types
import unittest
from pathlib import Path


def _load_save_video_module(root):
    """Load nodes/save_video.py with folder_paths pointed at a real directory tree."""
    module_path = Path(__file__).resolve().parents[1] / "nodes" / "save_video.py"
    package_name = "dist_save_video_testpkg"

    for mod_name in list(sys.modules):
        if mod_name == package_name or mod_name.startswith(f"{package_name}."):
            del sys.modules[mod_name]

    for suffix in ("", ".nodes", ".utils"):
        pkg = types.ModuleType(f"{package_name}{suffix}")
        pkg.__path__ = []
        sys.modules[f"{package_name}{suffix}"] = pkg

    for name in ("output", "temp", "input"):
        (root / name).mkdir(parents=True, exist_ok=True)

    folder_paths_module = types.ModuleType("folder_paths")
    folder_paths_module.get_output_directory = lambda: str(root / "output")
    folder_paths_module.get_temp_directory = lambda: str(root / "temp")
    folder_paths_module.get_input_directory = lambda: str(root / "input")

    def get_save_image_path(filename_prefix, output_dir, *_args):
        """Mirror folder_paths.get_save_image_path, including its counter parser.

        The counter is derived the way core derives it - digits from
        ``name[prefix_len + 1:].split('.')[0].split('_')[0]`` - rather than by counting
        files, so these tests actually exercise whether a filename this node produces can
        be parsed back into a counter. A glob-based stand-in would pass regardless, and the
        naming scheme is exactly what could break incrementing.
        """
        import os as _os

        subfolder = _os.path.dirname(_os.path.normpath(filename_prefix))
        filename = _os.path.basename(_os.path.normpath(filename_prefix))
        full_output_folder = Path(output_dir) / subfolder
        full_output_folder.mkdir(parents=True, exist_ok=True)

        digits = []
        for existing in full_output_folder.iterdir():
            if not existing.name.startswith(filename):
                continue
            try:
                remainder = existing.name[len(filename) + 1:]
                digits.append(int(remainder.split(".")[0].split("_")[0]))
            except ValueError:
                continue
        counter = max(digits) + 1 if digits else 1
        return str(full_output_folder), filename, counter, subfolder, filename_prefix

    folder_paths_module.get_save_image_path = get_save_image_path
    sys.modules["folder_paths"] = folder_paths_module

    logging_module = types.ModuleType(f"{package_name}.utils.logging")
    logging_module.debug_log = lambda *_a, **_k: None
    logging_module.log = lambda *_a, **_k: None
    sys.modules[f"{package_name}.utils.logging"] = logging_module

    spec = importlib.util.spec_from_file_location(f"{package_name}.nodes.save_video", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


class SaveVideoTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.module = _load_save_video_module(self.root)
        self.node = self.module.DistributedSaveVideo()

    def tearDown(self):
        self._tmp.cleanup()

    def _staged(self, name="clip_00001.mp4", body=b"not really an mp4"):
        path = self.root / "temp" / name
        path.write_bytes(body)
        return path

    # -- node shape ------------------------------------------------------------

    def test_it_is_an_output_node_so_it_can_terminate_the_graph(self):
        self.assertTrue(self.module.DistributedSaveVideo.OUTPUT_NODE)

    def test_it_takes_and_returns_vhs_filenames_so_it_can_chain(self):
        node = self.module.DistributedSaveVideo
        self.assertEqual(node.INPUT_TYPES()["required"]["filenames"][0], "VHS_FILENAMES")
        self.assertEqual(node.RETURN_TYPES, ("VHS_FILENAMES",))

    # -- saving ----------------------------------------------------------------

    def test_it_publishes_the_staged_file_into_output(self):
        staged = self._staged(body=b"payload")
        result = self.node.save((True, [str(staged)]), "video/ComfyUI")

        saved = self.root / "output" / "video" / "ComfyUI_00001.mp4"
        self.assertTrue(saved.exists(), list((self.root / "output").rglob("*")))
        self.assertEqual(saved.read_bytes(), b"payload")
        self.assertEqual(result["result"][0], (True, [str(saved)]))

    def test_saving_leaves_the_staged_file_for_other_consumers(self):
        """The reported bug: core SaveVideo on the collector's VIDEO output blew up.

        Both save nodes are OUTPUT_NODEs reading the same staged path with no edge
        between them, so consuming it made the graph succeed or fail on execution order.
        The collector's VIDEO output is a VideoFromFile holding this exact path, and
        av.open raised FileNotFoundError from get_dimensions when this node had already
        taken the file away.
        """
        staged = self._staged(body=b"payload")
        self.node.save((True, [str(staged)]), "video/ComfyUI")

        self.assertTrue(
            staged.exists(),
            "the staged file must survive: the collector's VIDEO output points at it",
        )
        self.assertEqual(staged.read_bytes(), b"payload")

    def test_two_save_nodes_can_publish_the_same_clip_in_either_order(self):
        """Two consumers is the case that broke; neither ordering may fail."""
        staged = self._staged(body=b"payload")

        first = self.node.save((True, [str(staged)]), "video/one")
        second = self.node.save((True, [str(staged)]), "video/two")

        for result in (first, second):
            saved = Path(result["result"][0][1][0])
            self.assertTrue(saved.exists())
            self.assertEqual(saved.read_bytes(), b"payload")
        self.assertTrue(staged.exists())

    def test_the_source_extension_is_preserved(self):
        staged = self._staged(name="clip.webm")
        self.node.save((True, [str(staged)]), "video/ComfyUI")
        self.assertTrue((self.root / "output" / "video" / "ComfyUI_00001.webm").exists())

    def test_filename_prefix_controls_the_subfolder(self):
        staged = self._staged()
        self.node.save((True, [str(staged)]), "WAN/2026-09/clip")
        self.assertTrue((self.root / "output" / "WAN" / "2026-09" / "clip_00001.mp4").exists())

    def test_repeated_saves_do_not_overwrite(self):
        first = self._staged(name="a.mp4", body=b"a")
        self.node.save((True, [str(first)]), "video/ComfyUI")
        second = self._staged(name="b.mp4", body=b"b")
        self.node.save((True, [str(second)]), "video/ComfyUI")

        folder = self.root / "output" / "video"
        self.assertEqual(
            sorted(p.name for p in folder.iterdir()),
            ["ComfyUI_00001.mp4", "ComfyUI_00002.mp4"],
        )
        # The counter advanced, so a name without the trailing underscore still parses.
        self.assertEqual((folder / "ComfyUI_00001.mp4").read_bytes(), b"a")
        self.assertEqual((folder / "ComfyUI_00002.mp4").read_bytes(), b"b")

    def test_every_file_is_saved_for_a_multi_worker_collection(self):
        first = self._staged(name="a.mp4", body=b"a")
        second = self._staged(name="b.mp4", body=b"b")
        result = self.node.save((True, [str(first), str(second)]), "video/ComfyUI")

        self.assertEqual(len(result["result"][0][1]), 2)
        self.assertEqual(len(result["ui"]["images"]), 2)

    # -- preview ---------------------------------------------------------------

    def test_it_reports_the_file_in_core_s_video_shape(self):
        """That shape is what draws an inline player and registers the asset."""
        staged = self._staged()
        result = self.node.save((True, [str(staged)]), "video/ComfyUI")

        self.assertEqual(result["ui"]["animated"], (True,))
        entry = result["ui"]["images"][0]
        self.assertEqual(entry["filename"], "ComfyUI_00001.mp4")
        self.assertEqual(entry["subfolder"], "video")
        self.assertEqual(entry["type"], "output")
        self.assertEqual(entry["format"], "video/mp4")

    def test_it_does_not_use_the_vhs_gifs_key(self):
        """Emitting both would register the same file as an asset twice."""
        staged = self._staged()
        result = self.node.save((True, [str(staged)]), "video/ComfyUI")
        self.assertNotIn("gifs", result["ui"])

    def test_save_output_off_previews_without_moving_the_file(self):
        staged = self._staged()
        result = self.node.save((True, [str(staged)]), "video/ComfyUI", save_output=False)

        self.assertTrue(staged.exists(), "the file should stay staged")
        self.assertEqual(list((self.root / "output").rglob("*.mp4")), [])
        entry = result["ui"]["images"][0]
        self.assertEqual(entry["type"], "temp")
        self.assertEqual(entry["filename"], "clip_00001.mp4")
        self.assertEqual(result["result"][0], (False, [str(staged)]))

    # -- edge cases ------------------------------------------------------------

    def test_an_empty_video_input_is_not_an_error(self):
        """Video Combine returns (save_output, []) for a zero-frame or unfinished batch."""
        result = self.node.save((True, []), "video/ComfyUI")
        self.assertEqual(result["result"][0], (True, []))
        self.assertEqual(result["ui"]["images"], [])

    def test_a_missing_file_raises_rather_than_silently_saving_nothing(self):
        with self.assertRaises(ValueError):
            self.node.save((True, [str(self.root / "temp" / "gone.mp4")]), "video/ComfyUI")

    def test_a_malformed_video_input_is_rejected(self):
        with self.assertRaises(ValueError):
            self.node.save("not-a-filenames-pair", "video/ComfyUI")

    def test_the_comfyui_list_wrapper_is_tolerated(self):
        staged = self._staged()
        result = self.node.save([(True, [str(staged)])], "video/ComfyUI")
        self.assertEqual(len(result["result"][0][1]), 1)

    def test_a_hardlink_is_used_when_output_shares_the_staging_filesystem(self):
        """Then publishing the clip costs no bytes at all."""
        staged = self._staged(body=b"payload")
        self.node.save((True, [str(staged)]), "video/ComfyUI")

        saved = self.root / "output" / "video" / "ComfyUI_00001.mp4"
        self.assertEqual(staged.stat().st_ino, saved.stat().st_ino)

    def test_a_copy_is_used_when_the_link_fails(self):
        """output is frequently a network share, so os.link fails with EXDEV there."""
        staged = self._staged(body=b"payload")
        original_link = self.module.os.link

        def failing_link(src, dst):
            raise OSError(18, "Invalid cross-device link")

        self.module.os.link = failing_link
        try:
            self.node.save((True, [str(staged)]), "video/ComfyUI")
        finally:
            self.module.os.link = original_link

        saved = self.root / "output" / "video" / "ComfyUI_00001.mp4"
        self.assertEqual(saved.read_bytes(), b"payload")
        self.assertNotEqual(staged.stat().st_ino, saved.stat().st_ino)
        self.assertTrue(staged.exists(), "a copy must still leave the original staged")


if __name__ == "__main__":
    unittest.main()
