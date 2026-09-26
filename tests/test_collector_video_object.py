"""The collector's core VIDEO output.

A fourth output carrying the collected clip as a core VIDEO, so the master side can use
ComfyUI's own video nodes - notably SaveVideo, which remuxes rather than re-encodes and
writes the workflow into the container metadata. It is a handle, not a decode:
VideoFromFile only stores the path.
"""

import sys
import types

from test_collector_list_inputs import _load_collector_module


class _FakeVideoFromFile:
    def __init__(self, path):
        self.path = path


class _FakeVideoFromList:
    def __init__(self, videos, complete_audio=None, codec=None):
        self.videos = list(videos)


def _with_fake_input_impl(module):
    """Stand in for comfy_api.latest.InputImpl without pulling in av."""
    module._InputImpl = types.SimpleNamespace(
        VideoFromFile=_FakeVideoFromFile,
        VideoFromList=_FakeVideoFromList,
    )
    return module


def test_the_video_output_is_declared_last():
    module = _load_collector_module()
    node = module.DistributedCollectorNode
    assert node.RETURN_TYPES == ("IMAGE", "AUDIO", "VHS_FILENAMES", "VIDEO")
    assert node.RETURN_NAMES == ("images", "audio", "filenames", "video")


def test_one_collected_clip_becomes_a_video_handle():
    module = _with_fake_input_impl(_load_collector_module())
    collector = module.DistributedCollectorNode()

    video = collector._video_object((True, ["/out/clip_00001.mp4"]))

    assert isinstance(video, _FakeVideoFromFile)
    assert video.path == "/out/clip_00001.mp4"


def test_several_clips_are_not_joined():
    """N workers produce N variants of a clip, not N segments of one.

    Nothing splits a video across workers - there is no frame-range divider, and
    DistributedSeed gives each participant its own seed - so concatenating them would be
    joining unrelated takes. The handle carries the first; filenames carries all of them.
    """
    module = _with_fake_input_impl(_load_collector_module())
    collector = module.DistributedCollectorNode()

    video = collector._video_object((True, ["/out/a.mp4", "/out/b.mp4"]))

    assert isinstance(video, _FakeVideoFromFile)
    assert video.path == "/out/a.mp4"


def test_several_clips_are_reported_rather_than_dropped_quietly():
    module = _with_fake_input_impl(_load_collector_module())
    collector = module.DistributedCollectorNode()
    messages = []
    module.log = messages.append

    collector._video_object((True, ["/out/a.mp4", "/out/b.mp4"]))

    assert any("2 separate clips" in m and "a.mp4" in m for m in messages), messages


def test_no_video_means_no_handle():
    """An images-or-audio job leaves the fourth output empty rather than inventing one."""
    module = _with_fake_input_impl(_load_collector_module())
    collector = module.DistributedCollectorNode()

    assert collector._video_object(None) is None
    assert collector._video_object((True, [])) is None


def test_an_old_comfyui_without_comfy_api_degrades_quietly():
    """The import is guarded, so the node still loads; the output is just empty."""
    module = _load_collector_module()
    module._InputImpl = None
    collector = module.DistributedCollectorNode()

    assert collector._video_object((True, ["/out/clip.mp4"])) is None


def test_run_appends_the_handle_to_what_execute_built():
    module = _with_fake_input_impl(_load_collector_module())
    collector = module.DistributedCollectorNode()

    images, audio, filenames, video = collector._with_video_object(
        (None, module.DistributedCollectorNode.EMPTY_AUDIO, (True, ["/out/clip.mp4"]))
    )

    assert filenames == (True, ["/out/clip.mp4"])
    assert isinstance(video, _FakeVideoFromFile)


def test_pass_through_also_carries_the_handle():
    module = _with_fake_input_impl(_load_collector_module())
    collector = module.DistributedCollectorNode()
    clip_filenames = (True, ["/out/clip_00001.mp4"])

    images, audio, filenames, video = collector.run(
        images=None, filenames=[clip_filenames], multi_job_id=[""]
    )

    assert filenames == clip_filenames
    assert isinstance(video, _FakeVideoFromFile)
