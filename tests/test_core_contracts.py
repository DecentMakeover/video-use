from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
HELPERS = ROOT / "helpers"
sys.path.insert(0, str(HELPERS))

import render  # noqa: E402


class RenderContractTests(unittest.TestCase):
    def test_relative_paths_resolve_from_edl_parent(self) -> None:
        edit_dir = Path("/tmp/video-use/edit")

        self.assertEqual(
            render.resolve_path("animations/card/render.mp4", edit_dir),
            Path("/tmp/video-use/edit/animations/card/render.mp4").resolve(),
        )
        self.assertEqual(
            render.resolve_path("master.srt", edit_dir),
            Path("/tmp/video-use/edit/master.srt").resolve(),
        )

    def test_portrait_and_landscape_subtitle_defaults_remain_distinct(self) -> None:
        video = Path("placeholder.mp4")

        with patch.object(render, "video_dimensions", return_value=(1080, 1920)):
            portrait = render.build_subtitle_force_style(video)
        with patch.object(render, "video_dimensions", return_value=(1920, 1080)):
            landscape = render.build_subtitle_force_style(video)

        self.assertIn("FontSize=10", portrait)
        self.assertIn("MarginV=90", portrait)
        self.assertIn("FontSize=18", landscape)
        self.assertIn("MarginV=35", landscape)

    def test_segment_extract_keeps_fades_scale_and_frame_rate_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "segment.mp4"
            with (
                patch.object(render, "is_portrait_source", return_value=True),
                patch.object(render, "is_hdr_source", return_value=False),
                patch.object(render.subprocess, "run") as run,
            ):
                render.extract_segment(
                    Path("source.mp4"),
                    seg_start=1.25,
                    duration=2.0,
                    grade_filter="",
                    out_path=output,
                )

        command = run.call_args.args[0]
        self.assertEqual(command[command.index("-vf") + 1], "scale=-2:1920")
        self.assertEqual(command[command.index("-r") + 1], "24")
        audio_filter = command[command.index("-af") + 1]
        self.assertIn("afade=t=in:st=0:d=0.03", audio_filter)
        self.assertIn("afade=t=out:st=1.970:d=0.03", audio_filter)

    def test_composite_shifts_overlays_and_applies_subtitles_last(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            edit_dir = Path(tmp)
            base = edit_dir / "base.mp4"
            overlay = edit_dir / "overlay.mp4"
            subtitles = edit_dir / "master.srt"
            output = edit_dir / "final.mp4"
            for path in (base, overlay, subtitles):
                path.touch()

            with patch.object(render.subprocess, "run") as run:
                render.build_final_composite(
                    base,
                    [
                        {
                            "file": "overlay.mp4",
                            "start_in_output": 1.5,
                            "duration": 2.0,
                        }
                    ],
                    subtitles,
                    output,
                    edit_dir,
                    subtitle_force_style="FontName=Helvetica",
                )

        command = run.call_args.args[0]
        filter_graph = command[command.index("-filter_complex") + 1]
        self.assertIn("setpts=PTS-STARTPTS+1.5/TB", filter_graph)
        self.assertIn("overlay=enable='between(t,1.500,3.500)'", filter_graph)
        self.assertGreater(filter_graph.rfind("subtitles="), filter_graph.rfind("overlay="))


class TranscriptTimelineTests(unittest.TestCase):
    def test_master_srt_uses_output_offsets_and_preserves_optional_case(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            edit_dir = Path(tmp)
            transcripts = edit_dir / "transcripts"
            transcripts.mkdir()
            (transcripts / "part-one.json").write_text(
                json.dumps(
                    {
                        "words": [
                            {"type": "word", "text": "First", "start": 10.1, "end": 10.4},
                            {"type": "word", "text": "idea.", "start": 10.5, "end": 10.9},
                            {"type": "word", "text": "Second", "start": 20.2, "end": 20.6},
                            {"type": "word", "text": "idea.", "start": 20.7, "end": 21.1},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            edl = {
                "sources": {"part-one": "../part-one.mp4"},
                "ranges": [
                    {"source": "part-one", "start": 10.0, "end": 12.0},
                    {"source": "part-one", "start": 20.0, "end": 22.0},
                ],
            }

            natural = edit_dir / "natural.srt"
            upper = edit_dir / "upper.srt"
            render.build_master_srt(edl, edit_dir, natural, text_case="natural")
            render.build_master_srt(edl, edit_dir, upper, text_case="upper")

            natural_text = natural.read_text(encoding="utf-8")
            upper_text = upper.read_text(encoding="utf-8")

        self.assertIn("00:00:00,100 --> 00:00:00,900", natural_text)
        self.assertIn("00:00:02,200 --> 00:00:03,100", natural_text)
        self.assertIn("First idea.", natural_text)
        self.assertIn("FIRST IDEA.", upper_text)


if __name__ == "__main__":
    unittest.main()
