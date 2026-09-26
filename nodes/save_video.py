import os
import shutil

import folder_paths

from ..utils.logging import debug_log, log


class DistributedSaveVideo:
    """Save a collected video file into the output directory.

    This is the video counterpart of SaveImage, and exists for the same reason. A
    DistributedCollector hands back what it collected without writing it anywhere: images
    stay tensors until a SaveImage node saves them, and a video stays in the temp directory
    the job_complete_video route staged it in until this node moves it. Receiving a file is
    not the same as saving one, and where it lands is the workflow's decision, not the
    transport's.

    Nothing is decoded or re-encoded. The file is moved when it is already on the same
    filesystem and copied otherwise, so a 45s clip costs a rename rather than an ffmpeg
    pass - which is the entire point of collecting a finished video instead of frames.
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "video": (
                    "VHS_FILENAMES",
                    {"tooltip": "The collected video, from a Distributed Collector's video output."},
                ),
                "filename_prefix": (
                    "STRING",
                    {
                        "default": "video/ComfyUI",
                        "tooltip": "Where to save it, relative to the output directory. May "
                                   "include a subfolder, e.g. 'WAN/clip'.",
                    },
                ),
                "save_output": (
                    "BOOLEAN",
                    {
                        "default": True,
                        "tooltip": "Save into the output folder. Turn off to preview the "
                                   "collected clip without keeping it - it stays in the temp "
                                   "folder and is cleaned up like any other temp file.",
                    },
                ),
            },
        }

    RETURN_TYPES = ("VHS_FILENAMES",)
    RETURN_NAMES = ("video",)
    FUNCTION = "save"
    OUTPUT_NODE = True
    CATEGORY = "image"
    DESCRIPTION = (
        "Saves a video collected from a Distributed worker into the output directory, "
        "without re-encoding it."
    )

    def save(self, video, filename_prefix="video/ComfyUI", save_output=True):
        paths = self._source_paths(video)
        if not paths:
            log("[Distributed] SaveVideo - nothing to save: the video input carried no files")
            return {"ui": {"images": [], "animated": (True,)}, "result": ((save_output, []),)}

        output_dir = folder_paths.get_output_directory()
        result_paths, entries = [], []
        for source in paths:
            if not os.path.exists(source):
                log(f"[Distributed] SaveVideo - skipping missing file: {source}")
                continue

            if save_output:
                destination, subfolder, filename = self._destination_for(
                    source, filename_prefix, output_dir
                )
                self._relocate(source, destination)
                folder_type = "output"
                debug_log(f"SaveVideo - {source} -> {destination}")
            else:
                # Preview only: leave the staged file where the route put it and describe it
                # in place, the way Video Combine reports a temp file when save_output is off.
                destination = source
                folder_type, subfolder = self._staged_location(source)
                filename = os.path.basename(source)
                if folder_type is None:
                    log(
                        f"[Distributed] SaveVideo - cannot preview {source}: it is outside "
                        f"ComfyUI's directories, so the UI has no way to address it"
                    )
                    continue
                debug_log(f"SaveVideo - previewing {source} without saving")

            result_paths.append(destination)
            entries.append(
                {
                    "filename": filename,
                    "subfolder": subfolder,
                    "type": folder_type,
                    "format": self._format_of(destination),
                }
            )

        if not result_paths:
            raise ValueError(
                "DistributedSaveVideo found none of the files it was given. The clip may "
                "have been cleaned from the temp directory already."
            )

        # {"images": [...], "animated": (True,)} is core's own shape for a video -- it is what
        # comfy_api's PreviewVideo serialises to, and what core's SaveVideo returns. Two
        # things follow from it: the frontend renders an inline player on this node, and
        # asset_enrichment registers the file so it appears in the outputs sidebar. VHS's own
        # 'gifs' key is deliberately not used -- its player is attached per node type in its
        # own JS, so it would do nothing here, and emitting both keys would register the same
        # file as an asset twice.
        return {
            "ui": {"images": entries, "animated": (True,)},
            "result": ((save_output, result_paths),),
        }

    @staticmethod
    def _format_of(path):
        """A mime-ish format string for the preview, mirroring Video Combine's 'format'."""
        extension = os.path.splitext(path)[1].lower().lstrip(".")
        return f"video/{extension or 'mp4'}"

    @staticmethod
    def _staged_location(path):
        """Describe a not-yet-saved file as (folder_type, subfolder) for the UI."""
        candidates = (
            ("temp", folder_paths.get_temp_directory()),
            ("output", folder_paths.get_output_directory()),
            ("input", folder_paths.get_input_directory()),
        )
        absolute = os.path.abspath(path)
        for folder_type, base in candidates:
            if not base:
                continue
            base = os.path.abspath(base)
            try:
                if os.path.commonpath([base, absolute]) != base:
                    continue
            except ValueError:
                continue
            relative = os.path.relpath(absolute, base)
            return folder_type, os.path.dirname(relative).replace(os.sep, "/")
        return None, ""

    @staticmethod
    def _source_paths(video):
        """Read the file list out of a VHS_FILENAMES value, tolerating the list wrapper."""
        if isinstance(video, list) and len(video) == 1:
            video = video[0]
        if not (isinstance(video, (list, tuple)) and len(video) == 2):
            raise ValueError(
                "DistributedSaveVideo expects a VHS_FILENAMES pair of (save_output, [paths])"
            )
        paths = video[1]
        return list(paths) if isinstance(paths, (list, tuple)) else []

    @staticmethod
    def _destination_for(source, filename_prefix, output_dir):
        """Pick an output path using ComfyUI's own counter, keeping the source extension.

        get_save_image_path applies the same prefix parsing, subfolder handling and
        NNNNN counter that SaveImage and core's SaveVideo use, so a saved clip is named
        consistently with everything else in the output directory.
        """
        full_output_folder, filename, counter, subfolder, _prefix = (
            folder_paths.get_save_image_path(filename_prefix, output_dir)
        )
        extension = os.path.splitext(source)[1] or ".mp4"
        name = f"{filename}_{counter:05}_{extension}"
        return os.path.join(full_output_folder, name), subfolder, name

    @staticmethod
    def _relocate(source, destination):
        """Move within a filesystem, copy across one. Never re-encode.

        os.replace fails with EXDEV when the temp directory and the output directory are on
        different filesystems, which is the normal case when output is a network share, so
        fall back to a copy and remove the staged file afterwards.
        """
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        try:
            os.replace(source, destination)
        except OSError:
            shutil.copy2(source, destination)
            try:
                os.remove(source)
            except OSError:
                debug_log(f"SaveVideo - could not remove staged file {source}")
