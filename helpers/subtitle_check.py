"""Extract full-resolution subtitle samples from a rendered video.

This helper exists because thumbnail contact sheets make oversized or
face-blocking subtitles look deceptively acceptable. It reads the master SRT,
chooses representative cues, and extracts one unscaled PNG at each cue midpoint.
Every PNG must be inspected at full resolution before final rendering.

Usage:
    python helpers/subtitle_check.py preview.mp4 --srt edit/master.srt
    python helpers/subtitle_check.py preview.mp4 --srt edit/master.srt --samples 8
    python helpers/subtitle_check.py preview.mp4 --srt edit/master.srt \
      --match OPERATIONS --output-dir edit/verify/subtitles
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


TIMING_RE = re.compile(
    r"(?P<start>\d{2}:\d{2}:\d{2},\d{3})\s+-->\s+"
    r"(?P<end>\d{2}:\d{2}:\d{2},\d{3})"
)


@dataclass(frozen=True)
class Cue:
    index: int
    start: float
    end: float
    text: str


def parse_timestamp(value: str) -> float:
    hours, minutes, rest = value.split(":")
    seconds, millis = rest.split(",")
    return (
        int(hours) * 3600
        + int(minutes) * 60
        + int(seconds)
        + int(millis) / 1000.0
    )


def parse_srt(path: Path) -> list[Cue]:
    blocks = re.split(r"\n\s*\n", path.read_text(encoding="utf-8-sig").strip())
    cues: list[Cue] = []
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        timing_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            continue
        match = TIMING_RE.search(lines[timing_index])
        if not match:
            continue
        text = " ".join(lines[timing_index + 1 :]).strip()
        if not text:
            continue
        try:
            cue_index = int(lines[0]) if timing_index > 0 else len(cues) + 1
        except ValueError:
            cue_index = len(cues) + 1
        cues.append(
            Cue(
                index=cue_index,
                start=parse_timestamp(match.group("start")),
                end=parse_timestamp(match.group("end")),
                text=text,
            )
        )
    return cues


def select_cues(cues: list[Cue], sample_count: int, matches: list[str]) -> list[Cue]:
    selected: dict[int, Cue] = {}

    def add(cue: Cue) -> None:
        selected[cue.index] = cue

    for needle in matches:
        matched = next((cue for cue in cues if needle.casefold() in cue.text.casefold()), None)
        if matched is None:
            print(f"warning: no subtitle cue matched {needle!r}", file=sys.stderr)
        else:
            add(matched)

    add(cues[0])
    add(cues[-1])
    add(max(cues, key=lambda cue: len(cue.text)))

    if sample_count > 1:
        for i in range(sample_count):
            index = round(i * (len(cues) - 1) / (sample_count - 1))
            add(cues[index])

    target = max(sample_count, len(selected))
    for cue in cues:
        if len(selected) >= target:
            break
        add(cue)

    return sorted(selected.values(), key=lambda cue: cue.start)


def video_dimensions(video: Path) -> tuple[int, int]:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=p=0", str(video),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return tuple(map(int, out.stdout.strip().split(",")[:2]))  # type: ignore[return-value]


def safe_slug(text: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return (slug or "cue")[:36]


def extract_frame(video: Path, time_s: float, output: Path) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{time_s:.3f}", "-i", str(video),
            "-frames:v", "1", "-c:v", "png", str(output),
        ],
        check=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract full-resolution subtitle frames for visual QC"
    )
    parser.add_argument("video", type=Path, help="Rendered video with burned subtitles")
    parser.add_argument("--srt", type=Path, required=True, help="Master SRT used for the render")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Output directory (default: <video_parent>/verify/subtitles)",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=7,
        help="Minimum representative cues to sample (default: 7)",
    )
    parser.add_argument(
        "--match",
        action="append",
        default=[],
        help="Also sample the first cue containing this text; repeatable",
    )
    args = parser.parse_args()

    video = args.video.resolve()
    srt = args.srt.resolve()
    if not video.is_file():
        sys.exit(f"video not found: {video}")
    if not srt.is_file():
        sys.exit(f"SRT not found: {srt}")
    if args.samples < 1:
        parser.error("--samples must be >= 1")

    cues = parse_srt(srt)
    if not cues:
        sys.exit(f"no subtitle cues found in {srt}")

    output_dir = (
        args.output_dir.resolve()
        if args.output_dir
        else video.parent / "verify" / "subtitles"
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    width, height = video_dimensions(video)
    selected = select_cues(cues, args.samples, args.match)
    report_samples: list[dict[str, object]] = []

    for order, cue in enumerate(selected, start=1):
        midpoint = cue.start + (cue.end - cue.start) / 2.0
        output = output_dir / (
            f"sample_{order:02d}_{midpoint:07.3f}_{safe_slug(cue.text)}.png"
        )
        extract_frame(video, midpoint, output)
        report_samples.append(
            {
                "cue_index": cue.index,
                "time_s": round(midpoint, 3),
                "text": cue.text,
                "image": output.name,
            }
        )
        print(f"{output.name}: {cue.text}")

    report = {
        "video": str(video),
        "srt": str(srt),
        "resolution": {"width": width, "height": height},
        "samples": report_samples,
    }
    report_path = output_dir / "subtitle_qc.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"\nsubtitle QC: {len(selected)} full-resolution samples at {width}x{height}")
    print(f"report: {report_path}")
    print("Inspect every PNG at full resolution. Do not approve subtitle size or position from a contact sheet.")


if __name__ == "__main__":
    main()
