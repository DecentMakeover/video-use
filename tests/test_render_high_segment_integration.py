from __future__ import annotations

import math
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELPERS = ROOT / "helpers"
sys.path.insert(0, str(HELPERS))

import render  # noqa: E402


FPS = 30
RATE = 48_000
WIDTH = 96
HEIGHT = 54
SEGMENTS = 1030
SEGMENT_DURATION = "0.0834"


def command(args: list[str], *, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        check=True,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def write_audio(path: Path, mode: str) -> None:
    samples: list[int] = []
    for index in range(int(0.6 * RATE)):
        t = index / RATE
        if mode == "pulse" and 0.031 <= t < 0.038:
            value = int(25_000 * math.sin(2 * math.pi * 1000 * t))
        elif mode == "final":
            value = int(18_000 * math.sin(2 * math.pi * 880 * t))
        elif mode == "fade":
            value = int(18_000 * math.sin(2 * math.pi * 440 * t))
        else:
            value = 0
        samples.append(value)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(RATE)
        output.writeframes(struct.pack(f"<{len(samples)}h", *samples))


def make_source(root: Path, name: str, kind: str) -> Path:
    wav = root / f"{name}.wav"
    video = root / f"{name}.video.mp4"
    output = root / f"{name}.mov"
    write_audio(wav, "pulse" if kind == "marker" else kind)

    if kind == "marker":
        source = f"color=c=black:s={WIDTH}x{HEIGHT}:r={FPS}:d=0.6"
        vf = "drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:enable=between(t\\,0.033\\,0.066)"
    elif kind == "title":
        source = f"color=c=blue:s={WIDTH}x{HEIGHT}:r={FPS}:d=0.6"
        vf = "null"
    elif kind == "fade":
        source = f"color=c=white:s={WIDTH}x{HEIGHT}:r={FPS}:d=0.6"
        vf = "null"
    else:
        source = f"color=c=yellow:s={WIDTH}x{HEIGHT}:r={FPS}:d=0.6"
        vf = "null"

    command(
        [
            "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", source,
            "-vf", vf, "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", video.as_posix(),
        ]
    )
    command(
        [
            "ffmpeg", "-v", "error", "-y", "-i", video.as_posix(),
            "-i", wav.as_posix(), "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "pcm_s16le", output.as_posix(),
        ]
    )
    return output


def decoded_audio(path: Path, sample_format: str = "s16le") -> list[int]:
    result = command(
        [
            "ffmpeg", "-v", "error", "-i", path.as_posix(), "-map", "0:a:0",
            "-ac", "1", "-ar", str(RATE), "-f", sample_format, "-",
        ],
        capture=True,
    )
    width = 2 if sample_format == "s16le" else 4
    code = "h" if width == 2 else "i"
    return list(struct.unpack(f"<{len(result.stdout) // width}{code}", result.stdout))


def decoded_rgb(path: Path) -> list[bytes]:
    result = command(
        [
            "ffmpeg", "-v", "error", "-i", path.as_posix(), "-map", "0:v:0",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ],
        capture=True,
    )
    frame_size = WIDTH * HEIGHT * 3
    if len(result.stdout) % frame_size:
        raise AssertionError("decoded video did not contain complete frames")
    return [
        result.stdout[offset : offset + frame_size]
        for offset in range(0, len(result.stdout), frame_size)
    ]


def srt_time(seconds: float) -> str:
    millis = round(seconds * 1000)
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
class HighSegmentRendererIntegrationTests(unittest.TestCase):
    def test_1030_segment_schedule_stays_synchronized_and_keeps_the_ending(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker_source = make_source(root, "marker", "marker")
            title_source = make_source(root, "title", "title")
            final_source = make_source(root, "final", "final")
            fade_source = make_source(root, "fade", "fade")

            ranges = [
                {"source": "marker", "start": 0, "end": SEGMENT_DURATION}
                for _ in range(SEGMENTS)
            ]
            ranges[0]["end"] = "0.5"
            schedule = render.build_render_schedule(ranges, str(FPS))
            title_index = 400
            final_index = SEGMENTS - 1

            # Use one real extraction per distinct canonical allocation. Reusing
            # those files keeps this 1,030-entry integration fixture small while
            # still exercising the actual extraction, concat, composite, subtitle,
            # and final-AAC code paths.
            bank: dict[tuple[int, int], render.SegmentPaths] = {}
            for allocation in schedule.segments:
                key = (allocation.frame_count, allocation.sample_count)
                if key in bank:
                    continue
                bank[key] = render.extract_segment(
                    marker_source, 0.0, float(SEGMENT_DURATION), "",
                    root / f"normal_{key[0]}_{key[1]}.video.mp4",
                    audio_out_path=root / f"normal_{key[0]}_{key[1]}.audio.wav",
                    draft=True, fps=str(FPS), long_edge=WIDTH,
                    frame_count=key[0], sample_count=key[1],
                )

            faded_allocation = schedule.segments[0]
            faded = render.extract_segment(
                fade_source, 0.0, 0.5, "",
                root / "faded.video.mp4", audio_out_path=root / "faded.audio.wav",
                draft=True, fps=str(FPS), long_edge=WIDTH,
                frame_count=faded_allocation.frame_count,
                sample_count=faded_allocation.sample_count,
                fade_in=0.1, fade_out=0.1,
            )
            title_allocation = schedule.segments[title_index]
            title = render.extract_segment(
                title_source, 0.0, float(SEGMENT_DURATION), "",
                root / "title-segment.video.mp4",
                audio_out_path=root / "title-segment.audio.wav",
                draft=True, fps=str(FPS), long_edge=WIDTH,
                frame_count=title_allocation.frame_count,
                sample_count=title_allocation.sample_count,
            )
            final_allocation = schedule.segments[final_index]
            final_marker = render.extract_segment(
                final_source, 0.0, float(SEGMENT_DURATION), "",
                root / "final-segment.video.mp4",
                audio_out_path=root / "final-segment.audio.wav",
                draft=True, fps=str(FPS), long_edge=WIDTH,
                frame_count=final_allocation.frame_count,
                sample_count=final_allocation.sample_count,
            )

            segments = [
                bank[(item.frame_count, item.sample_count)] for item in schedule.segments
            ]
            segments[0] = faded
            segments[title_index] = title
            segments[final_index] = final_marker

            base = root / "base.mov"
            render.concat_segments(segments, base, root)
            base_frames = decoded_rgb(base)
            base_audio = decoded_audio(base, "s32le")
            self.assertEqual(len(base_frames), schedule.total_frames)
            self.assertEqual(len(base_audio), schedule.total_samples)

            # Silent title card is exact on the lossless timeline.
            title_slice = base_audio[
                title_allocation.start_samples : title_allocation.end_samples
            ]
            self.assertEqual(max(map(abs, title_slice), default=0), 0)

            # Place a subtitle on a black frame following a three-frame segment.
            cue_index = next(
                index for index in range(2, title_index)
                if schedule.segments[index - 1].frame_count == 3
                and index != title_index
            )
            cue_frame = schedule.segments[cue_index].start_frames
            cue_start = cue_frame / FPS
            cue_end = cue_start + 0.2
            subtitles = root / "fixture.srt"
            subtitles.write_text(
                f"1\n{srt_time(cue_start)} --> {srt_time(cue_end)}\nSYNC\n",
                encoding="utf-8",
            )

            overlay = root / "overlay.mp4"
            command(
                [
                    "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"color=c=green:s=16x16:r={FPS}:d=0.2", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", overlay.as_posix(),
                ]
            )
            title_start = title_allocation.start_frames / FPS
            prenorm = root / "prenorm.mov"
            render.build_final_composite(
                base,
                [{"file": overlay.name, "start_in_output": title_start,
                  "duration": title_allocation.frame_count / FPS}],
                subtitles,
                prenorm,
                root,
                subtitle_force_style=(
                    "FontName=Helvetica,FontSize=40,Bold=1,PrimaryColour=&H000000FF,"
                    "Outline=0,Shadow=0,Alignment=2,MarginV=2"
                ),
                fps=str(FPS),
            )
            final = root / "final.mp4"
            render.encode_aac_once(prenorm, final)

            # Full decode is a required assertion, not merely a command-string test.
            command(["ffmpeg", "-v", "error", "-i", final.as_posix(), "-f", "null", "-"])
            frames = decoded_rgb(final)
            audio = decoded_audio(final)
            self.assertEqual(len(frames), schedule.total_frames)
            self.assertLessEqual(abs(len(audio) - schedule.total_samples), render.AAC_FRAME_SAMPLES)

            # Exercise the real two-pass loudnorm delivery path as media, not
            # only as a mocked command contract.
            normalized = root / "final-loudnorm.mp4"
            self.assertTrue(render.apply_loudnorm_two_pass(prenorm, normalized))
            command(
                ["ffmpeg", "-v", "error", "-i", normalized.as_posix(), "-f", "null", "-"]
            )
            normalized_frames = decoded_rgb(normalized)
            normalized_audio = decoded_audio(normalized)
            self.assertEqual(len(normalized_frames), schedule.total_frames)
            self.assertEqual(normalized_frames, frames)  # loudnorm copies the composed video
            self.assertLessEqual(
                abs(len(normalized_audio) - schedule.total_samples),
                render.AAC_FRAME_SAMPLES,
            )

            def pixel(frame: bytes, x: int, y: int) -> tuple[int, int, int]:
                offset = (y * WIDTH + x) * 3
                return tuple(frame[offset : offset + 3])  # type: ignore[return-value]

            # The custom visual and audio fade-in/fade-out envelopes survive
            # real extraction and concatenation (not just command construction).
            fade_frames = frames[
                faded_allocation.start_frames : faded_allocation.end_frames
            ]
            fade_brightness = [sum(frame) / len(frame) for frame in fade_frames]
            middle_brightness = fade_brightness[len(fade_brightness) // 2]
            self.assertLess(fade_brightness[0], middle_brightness * 0.25)
            self.assertLess(fade_brightness[-1], middle_brightness * 0.75)
            fade_audio = base_audio[
                faded_allocation.start_samples : faded_allocation.end_samples
            ]

            def rms(values: list[int]) -> float:
                return math.sqrt(sum(value * value for value in values) / len(values))

            edge_samples = round(0.025 * RATE)
            middle_start = round(0.225 * RATE)
            self.assertLess(
                rms(fade_audio[:edge_samples]),
                rms(fade_audio[middle_start : middle_start + edge_samples]) * 0.5,
            )
            self.assertLess(
                rms(fade_audio[-edge_samples:]),
                rms(fade_audio[middle_start : middle_start + edge_samples]) * 0.5,
            )

            # Distributed marker lag <=20ms and head-to-tail drift < one frame
            # on both finalization paths.
            def assert_marker_sync(track: list[int]) -> None:
                lags: list[float] = []
                for index in (10, 200, 600, 900):
                    allocation = schedule.segments[index]
                    expected_video = allocation.start_frames + 1
                    candidates = range(max(0, expected_video - 1), expected_video + 2)
                    flash_frame = max(
                        candidates,
                        key=lambda frame_index: sum(frames[frame_index]) / len(frames[frame_index]),
                    )
                    expected_sample = allocation.start_samples + round(0.033 * RATE)
                    radius = round(0.020 * RATE)
                    lo = max(0, expected_sample - radius)
                    hi = min(len(track), expected_sample + radius + 1)
                    impulse_sample = lo + max(
                        range(hi - lo), key=lambda offset: abs(track[lo + offset])
                    )
                    lag = impulse_sample / RATE - flash_frame / FPS
                    self.assertLessEqual(abs(lag), 0.020)
                    lags.append(lag)
                self.assertLess(abs(lags[-1] - lags[0]), 1 / FPS)

            assert_marker_sync(audio)
            assert_marker_sync(normalized_audio)

            # Title-card and overlay timing stay on their canonical frame window.
            blue_frames = []
            green_frames = []
            for index, frame in enumerate(frames):
                center = pixel(frame, WIDTH // 2, HEIGHT // 2)
                corner = pixel(frame, 4, 4)
                if center[2] > 120 and center[2] > center[0] * 1.5:
                    blue_frames.append(index)
                if corner[1] > corner[0] * 1.3 and corner[1] > corner[2] * 1.3:
                    green_frames.append(index)
            expected_title = list(range(title_allocation.start_frames, title_allocation.end_frames))
            self.assertEqual(blue_frames, expected_title)
            self.assertEqual(green_frames, expected_title)

            # Subtitle pixels appear on the intended black cue frame, within one frame.
            subtitle_frames: list[int] = []
            subtitle_red_counts: dict[int, int] = {}
            for index in range(max(0, cue_frame - 1), cue_frame + 3):
                frame = frames[index]
                red_pixels = sum(
                    1
                    for y in range(HEIGHT)
                    for x in range(WIDTH)
                    if (
                        pixel(frame, x, y)[0] > 120
                        and pixel(frame, x, y)[0] > pixel(frame, x, y)[1] + 50
                    )
                )
                subtitle_red_counts[index] = red_pixels
                if red_pixels > 5:
                    subtitle_frames.append(index)
            self.assertTrue(subtitle_frames, subtitle_red_counts)
            self.assertLessEqual(abs(subtitle_frames[0] - cue_frame), 1)

            # Unique final marker survives completely through the final frame/audio end.
            final_frame_range = range(final_allocation.start_frames, final_allocation.end_frames)
            for frame_index in final_frame_range:
                red, green, blue = pixel(frames[frame_index], WIDTH // 2, HEIGHT // 2)
                self.assertGreater(red, 120)
                self.assertGreater(green, 120)
                self.assertLess(blue, 100)
            final_audio = audio[final_allocation.start_samples : min(
                final_allocation.end_samples, len(audio)
            )]
            self.assertGreater(max(map(abs, final_audio), default=0), 1000)
            self.assertGreaterEqual(len(final_audio), final_allocation.sample_count - render.AAC_FRAME_SAMPLES)
            normalized_final_audio = normalized_audio[
                final_allocation.start_samples : min(
                    final_allocation.end_samples, len(normalized_audio)
                )
            ]
            self.assertGreater(max(map(abs, normalized_final_audio), default=0), 1000)

            # Negative control: the former multiplexed AAC-per-segment path grows
            # independently and must not satisfy this fixture's endpoint contract.
            old_segment = root / "old-aac-segment.mp4"
            command(
                [
                    "ffmpeg", "-v", "error", "-y", "-ss", "0", "-i",
                    marker_source.as_posix(), "-t", SEGMENT_DURATION,
                    "-vf", f"scale={WIDTH}:-2", "-r", str(FPS),
                    "-af", "afade=t=in:st=0:d=0.03,afade=t=out:st=0.0534:d=0.03",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "192k", "-ar", str(RATE),
                    old_segment.as_posix(),
                ]
            )
            old_list = root / "old-concat.txt"
            old_list.write_text(
                "".join(f"file '{old_segment.as_posix()}'\n" for _ in range(SEGMENTS)),
                encoding="utf-8",
            )
            old_output = root / "old-output.mp4"
            command(
                [
                    "ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0",
                    "-i", old_list.as_posix(), "-c", "copy", old_output.as_posix(),
                ]
            )
            probe = command(
                [
                    "ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                    "-show_entries", "stream=nb_read_frames,duration",
                    "-of", "csv=p=0", old_output.as_posix(),
                ],
                capture=True,
            ).stdout.decode().strip()
            old_duration, old_frames = probe.split(",")
            # Newer ffmpeg versions may honor each repeated MP4 AAC edit list,
            # but the legacy independent -t/-r path still adds 513 video frames
            # and misses the canonical endpoint by many seconds.
            self.assertNotEqual(int(old_frames), schedule.total_frames)
            self.assertGreater(
                abs(float(old_duration) - schedule.total_frames / FPS), 1.0
            )


if __name__ == "__main__":
    unittest.main()
