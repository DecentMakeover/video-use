from __future__ import annotations

import json
import sys
import tempfile
import unittest
from fractions import Fraction
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
        video_filter = command[command.index("-vf") + 1]
        self.assertIn("setpts=PTS-STARTPTS", video_filter)
        self.assertIn("fps=fps=24:round=near", video_filter)
        self.assertIn("trim=end_frame=48", video_filter)
        self.assertIn("setpts=N/(24*TB)", video_filter)
        self.assertIn("scale=-2:1920", video_filter)
        self.assertNotIn("-r", command)
        self.assertNotIn("aac", command)
        self.assertIn("pcm_s24le", command)
        audio_filter = command[command.index("-af") + 1]
        self.assertIn("asetpts=PTS-STARTPTS", audio_filter)
        self.assertIn("atrim=end_sample=96000", audio_filter)
        self.assertIn("asetpts=N/48000/TB", audio_filter)
        self.assertIn("afade=t=in:st=0:d=0.030000", audio_filter)
        self.assertIn("afade=t=out:st=1.970000:d=0.030000", audio_filter)

    def test_fps_and_crf_overrides_reach_the_encoder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "segment.mp4"
            with (
                patch.object(render, "is_portrait_source", return_value=False),
                patch.object(render, "is_hdr_source", return_value=False),
                patch.object(render.subprocess, "run") as run,
            ):
                render.extract_segment(
                    Path("source.mp4"),
                    seg_start=0.0,
                    duration=2.0,
                    grade_filter="",
                    out_path=output,
                    fps="30/1",
                    crf=18,
                )

        command = run.call_args.args[0]
        video_filter = command[command.index("-vf") + 1]
        self.assertIn("fps=fps=30:round=near", video_filter)
        self.assertEqual(command[command.index("-frames:v") + 1], "60")
        self.assertEqual(command[command.index("-crf") + 1], "18")

    def test_fade_through_black_reaches_video_and_audio_filters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "segment.mp4"
            with (
                patch.object(render, "is_portrait_source", return_value=False),
                patch.object(render, "is_hdr_source", return_value=False),
                patch.object(render.subprocess, "run") as run,
            ):
                render.extract_segment(
                    Path("source.mp4"),
                    seg_start=0.0,
                    duration=5.0,
                    grade_filter="",
                    out_path=output,
                    fade_in=0.5,
                    fade_out=0.75,
                )

        command = run.call_args.args[0]
        video_filter = command[command.index("-vf") + 1]
        self.assertIn("fade=t=in:st=0:d=0.500", video_filter)
        self.assertIn("fade=t=out:st=4.250000:d=0.750000", video_filter)
        audio_filter = command[command.index("-af") + 1]
        self.assertIn("afade=t=in:st=0:d=0.500000", audio_filter)
        self.assertIn("afade=t=out:st=4.250000:d=0.750000", audio_filter)

    def test_fades_exceeding_segment_duration_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            render.extract_segment(
                Path("source.mp4"),
                seg_start=0.0,
                duration=1.0,
                grade_filter="",
                out_path=Path("/tmp/never-written.mp4"),
                fade_in=0.6,
                fade_out=0.6,
            )
        with self.assertRaises(ValueError):
            render.extract_segment(
                Path("source.mp4"),
                seg_start=0.0,
                duration=1.0,
                grade_filter="",
                out_path=Path("/tmp/never-written.mp4"),
                fade_in=-0.1,
            )

    def test_range_fades_flow_from_edl_to_extraction(self) -> None:
        edl = {
            "sources": {"a": "a.mp4"},
            "ranges": [
                {"source": "a", "start": 0.0, "end": 4.0, "fade_in": 0.3, "fade_out": 0.3},
                {"source": "a", "start": 5.0, "end": 9.0},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(render, "extract_segment") as extract:
                render.extract_all_segments(edl, Path(tmp), preview=False)

        first, second = extract.call_args_list
        self.assertEqual(first.kwargs["fade_in"], 0.3)
        self.assertEqual(first.kwargs["fade_out"], 0.3)
        self.assertEqual(second.kwargs["fade_in"], 0.0)
        self.assertEqual(second.kwargs["fade_out"], 0.0)

    def test_long_edge_sets_output_scale_and_defaults_to_1080p(self) -> None:
        for long_edge, expected in ((1920, "scale=1920:-2"), (3840, "scale=3840:-2")):
            with tempfile.TemporaryDirectory() as tmp:
                with (
                    patch.object(render, "is_portrait_source", return_value=False),
                    patch.object(render, "is_hdr_source", return_value=False),
                    patch.object(render.subprocess, "run") as run,
                ):
                    render.extract_segment(
                        Path("source.mp4"), seg_start=0.0, duration=2.0,
                        grade_filter="", out_path=Path(tmp) / "s.mp4",
                        long_edge=long_edge,
                    )
                vf = run.call_args.args[0][run.call_args.args[0].index("-vf") + 1]
                self.assertIn(expected, vf)

    def test_source_fps_mode_probes_each_source_once(self) -> None:
        edl = {
            "sources": {"a": "a.mp4"},
            "ranges": [
                {"source": "a", "start": 0.0, "end": 1.0},
                {"source": "a", "start": 2.0, "end": 3.0},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(render, "source_fps", return_value="30/1") as probe,
                patch.object(render, "extract_segment") as extract,
            ):
                render.extract_all_segments(
                    edl, Path(tmp), preview=False, fps="source", crf=17
                )

        self.assertEqual(probe.call_count, 1)
        for call in extract.call_args_list:
            self.assertEqual(call.kwargs["fps"], "30")
            self.assertEqual(call.kwargs["crf"], 17)

    def test_cumulative_rational_schedule_prevents_per_segment_frame_growth(self) -> None:
        ranges = [
            {"source": "a", "start": 0, "end": "0.0834"}
            for _ in range(1030)
        ]
        schedule = render.build_render_schedule(ranges, "30")

        canonical_frames = render._round_fraction(Fraction(834, 10_000) * 1030 * 30)
        independently_rounded = 1030 * render._round_fraction(Fraction(834, 10_000) * 30)
        self.assertEqual(schedule.total_frames, canonical_frames)
        self.assertNotEqual(schedule.total_frames, independently_rounded)
        self.assertEqual(
            schedule.total_samples,
            render._round_fraction(Fraction(834, 10_000) * 1030 * 48_000),
        )
        self.assertEqual(
            sum(item.frame_count for item in schedule.segments), schedule.total_frames
        )
        self.assertEqual(
            sum(item.sample_count for item in schedule.segments), schedule.total_samples
        )

    def test_concat_keeps_video_and_pcm_timelines_independent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            segments = [
                render.SegmentPaths(root / "a.video.mp4", root / "a.audio.wav"),
                render.SegmentPaths(root / "b.video.mp4", root / "b.audio.wav"),
            ]
            with patch.object(render.subprocess, "run") as run:
                render.concat_segments(segments, root / "base.mov", root)

        self.assertEqual(run.call_count, 3)
        video_command, audio_command, mux_command = [call.args[0] for call in run.call_args_list]
        self.assertEqual(video_command[video_command.index("-map") + 1], "0:v:0")
        self.assertEqual(audio_command[audio_command.index("-map") + 1], "0:a:0")
        self.assertIn("-c", mux_command)
        self.assertNotIn("aac", " ".join(video_command + audio_command + mux_command))

    def test_no_loudnorm_finalizer_performs_one_aac_encode(self) -> None:
        with patch.object(render.subprocess, "run") as run:
            render.encode_aac_once(Path("prenorm.mov"), Path("final.mp4"))
        command = run.call_args.args[0]
        self.assertEqual(command.count("aac"), 1)
        self.assertEqual(command[command.index("-c:v") + 1], "copy")
        self.assertEqual(command[command.index("-c:a") + 1], "aac")

    def test_loudnorm_finalizer_performs_one_aac_encode(self) -> None:
        measurement = {
            "input_i": "-20.0", "input_tp": "-3.0", "input_lra": "4.0",
            "input_thresh": "-30.0", "target_offset": "0.1",
        }
        with (
            patch.object(render, "measure_loudness", return_value=measurement),
            patch.object(render.subprocess, "run") as run,
        ):
            self.assertTrue(
                render.apply_loudnorm_two_pass(Path("prenorm.mov"), Path("final.mp4"))
            )
        command = run.call_args.args[0]
        self.assertEqual(command.count("aac"), 1)
        self.assertIn("loudnorm=", command[command.index("-af") + 1])

    def test_source_fps_rejects_mixed_selected_rates(self) -> None:
        edl = {
            "sources": {"a": "a.mp4", "b": "b.mp4"},
            "ranges": [
                {"source": "a", "start": 0, "end": 1},
                {"source": "b", "start": 0, "end": 1},
            ],
        }
        with patch.object(render, "source_fps", side_effect=["30/1", "24000/1001"]):
            with self.assertRaisesRegex(ValueError, "share one rate"):
                render.resolve_output_fps(edl, Path("/tmp"), "source")

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
        self.assertIn("setpts=PTS-STARTPTS+1.500000000/TB", filter_graph)
        self.assertIn(
            "overlay=enable='gte(t,1.500000000)*lt(t,3.500000000)'", filter_graph
        )
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
