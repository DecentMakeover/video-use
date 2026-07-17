"""Render and QC a JSON batch of transcript-snapped vertical podcast clips.

The batch spec is a JSON object with ``source_name`` (transcript stem under
<edit>/transcripts/), ``source_path`` (the media file), and ``clips`` — a list
whose entries contain ``id``, ``slug``, ``approx_start``, ``approx_end``, and
``crop_x``. Optional ``exclude_words`` removes a transcript word plus a guard
on both sides by emitting two EDL ranges.

Usage:
    python helpers/batch_clips.py /path/to/edit/batch_spec.json
    python helpers/batch_clips.py /path/to/edit/batch_spec.json --only S1
    python helpers/batch_clips.py /path/to/edit/batch_spec.json --resume
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


CANVAS = "1080x1920"

# Hook title card (persistent top overlay) — locked style from the S2 pilot:
# white rounded card, Montserrat Black 48, top-center, clear of platform UI.
TITLE_FONT_FILE = "Montserrat-Black.ttf"
TITLE_FONT_SIZE = 48
TITLE_CARD_Y = 120
TITLE_PAD_X = 40
TITLE_PAD_Y = 26
TITLE_LINE_GAP = 8
TITLE_RADIUS = 26
TITLE_CARD_FILL = (255, 255, 255, 242)
TITLE_TEXT_FILL = (12, 12, 12, 255)
CROP_WIDTH = 608
CROP_HEIGHT = 1080
OUTPUT_FPS = 24
LEAD_PAD_S = 0.09
TAIL_PAD_S = 0.30
SNAP_WINDOW_S = 0.15
NEXT_WORD_CLEARANCE_S = 0.05
EXCLUDE_GUARD_S = 0.06
MAX_RENDER_ATTEMPTS = 3  # initial render plus two fix attempts
MAX_CUE_CHARS = 26

HERE = Path(__file__).resolve().parent
RENDER_HELPER = HERE / "render.py"
SUBTITLE_CHECK_HELPER = HERE / "subtitle_check.py"


def _as_float(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def load_spec(path: Path) -> tuple[str, Path, list[dict[str, Any]]]:
    """Load a batch spec: {"source_name", "source_path", "clips": [...]}.

    source_name must match the transcript stem under <edit>/transcripts/;
    source_path is the media file every clip cuts from.
    """
    root = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(root, dict):
        raise ValueError(
            'batch spec must be an object: {"source_name": ..., '
            '"source_path": ..., "clips": [...]}'
        )
    source_name = root.get("source_name")
    source_path = root.get("source_path")
    if not isinstance(source_name, str) or not source_name.strip():
        raise ValueError("batch spec needs a non-empty source_name")
    if not isinstance(source_path, str) or not source_path.strip():
        raise ValueError("batch spec needs a non-empty source_path")
    data = root.get("clips")
    if not isinstance(data, list) or not data:
        raise ValueError("batch spec clips must be a non-empty JSON list")

    required = {"id", "slug", "approx_start", "approx_end", "crop_x"}
    seen_ids: set[str] = set()
    seen_slugs: set[str] = set()
    for index, clip in enumerate(data):
        if not isinstance(clip, dict):
            raise ValueError(f"clip {index} must be an object")
        missing = required - clip.keys()
        if missing:
            raise ValueError(f"clip {index} missing: {', '.join(sorted(missing))}")
        clip_id = str(clip["id"])
        slug = str(clip["slug"])
        if clip_id in seen_ids:
            raise ValueError(f"duplicate clip id: {clip_id}")
        if slug in seen_slugs:
            raise ValueError(f"duplicate clip slug: {slug}")
        seen_ids.add(clip_id)
        seen_slugs.add(slug)

        approx_start = _as_float(clip["approx_start"], f"{clip_id}.approx_start")
        approx_end = _as_float(clip["approx_end"], f"{clip_id}.approx_end")
        if approx_end <= approx_start:
            raise ValueError(f"{clip_id}: approx_end must be after approx_start")
        try:
            crop_x = int(clip["crop_x"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{clip_id}.crop_x must be an integer") from exc
        if crop_x < 0 or crop_x + CROP_WIDTH > 1920:
            raise ValueError(f"{clip_id}: crop_x {crop_x} is outside the 1920px source")
        title = clip.get("title")
        if title is not None:
            if (
                not isinstance(title, list)
                or not 1 <= len(title) <= 3
                or not all(isinstance(l, str) and l.strip() for l in title)
            ):
                raise ValueError(
                    f"{clip_id}.title must be a list of 1-3 non-empty strings"
                )
    return source_name.strip(), Path(source_path).expanduser(), data


def load_transcript_words(path: Path) -> list[dict[str, Any]]:
    transcript = json.loads(path.read_text(encoding="utf-8"))
    words: list[dict[str, Any]] = []
    for raw in transcript.get("words", []):
        if raw.get("type") != "word":
            continue
        try:
            start = float(raw["start"])
            end = float(raw["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(start) or not math.isfinite(end) or end <= start:
            continue
        words.append({"text": str(raw.get("text") or ""), "start": start, "end": end})
    if not words:
        raise ValueError(f"no timed words found in transcript: {path}")
    return words


def _floor_frame(seconds: float) -> float:
    return math.floor(seconds * OUTPUT_FPS + 1e-9) / OUTPUT_FPS


def _ceil_frame(seconds: float) -> float:
    return math.ceil(seconds * OUTPUT_FPS - 1e-9) / OUTPUT_FPS


def snap_clip(
    clip: dict[str, Any], words: list[dict[str, Any]]
) -> tuple[float, float, int, int]:
    """Snap approximate boundaries to words, pads, and outward output frames."""
    approx_start = float(clip["approx_start"])
    approx_end = float(clip["approx_end"])
    start_floor = approx_start - SNAP_WINDOW_S
    end_ceiling = approx_end + SNAP_WINDOW_S

    first_index = next(
        (index for index, word in enumerate(words) if word["start"] >= start_floor),
        None,
    )
    last_index = next(
        (
            index
            for index in range(len(words) - 1, -1, -1)
            if words[index]["end"] <= end_ceiling
        ),
        None,
    )
    if first_index is None or last_index is None or first_index > last_index:
        raise ValueError(
            f"{clip['id']}: no valid transcript span around "
            f"{approx_start:.3f}-{approx_end:.3f}"
        )

    previous_end = words[first_index - 1]["end"] if first_index > 0 else 0.0
    next_start = (
        words[last_index + 1]["start"] if last_index + 1 < len(words) else math.inf
    )

    padded_start = max(words[first_index]["start"] - LEAD_PAD_S, previous_end)
    padded_end = min(
        words[last_index]["end"] + TAIL_PAD_S,
        next_start - NEXT_WORD_CLEARANCE_S,
    )

    # The renderer produces 24 fps video. Round outward so sub-frame padding
    # is preserved at encode time, then re-apply the word-neighbour clamps.
    snapped_start = max(_floor_frame(padded_start), previous_end)
    snapped_end = min(_ceil_frame(padded_end), next_start - NEXT_WORD_CLEARANCE_S)
    if snapped_end <= snapped_start:
        raise ValueError(f"{clip['id']}: snapped range has no duration")
    return (
        round(snapped_start, 6),
        round(snapped_end, 6),
        first_index,
        last_index,
    )


def _normalized_word(text: str) -> str:
    return re.sub(r"^\W+|\W+$", "", text, flags=re.UNICODE).casefold()


def find_excluded_word(
    clip: dict[str, Any], words: list[dict[str, Any]]
) -> dict[str, Any] | None:
    request = clip.get("exclude_words")
    if request is None:
        return None
    if not isinstance(request, dict) or "text" not in request or "window" not in request:
        raise ValueError(f"{clip['id']}: exclude_words requires text and window")
    window = request["window"]
    if not isinstance(window, list) or len(window) != 2:
        raise ValueError(f"{clip['id']}: exclude_words.window must have two values")
    window_start = _as_float(window[0], f"{clip['id']}.exclude_words.window[0]")
    window_end = _as_float(window[1], f"{clip['id']}.exclude_words.window[1]")
    target = _normalized_word(str(request["text"]))
    matches = [
        word
        for word in words
        if word["start"] >= window_start
        and word["end"] <= window_end
        and _normalized_word(word["text"]) == target
    ]
    if len(matches) != 1:
        raise ValueError(
            f"{clip['id']}: expected one {request['text']!r} in "
            f"{window_start:g}-{window_end:g}, found {len(matches)}"
        )
    return matches[0]


def build_clip_edl(
    clip: dict[str, Any],
    words: list[dict[str, Any]],
    source_name: str,
    source_path: Path,
) -> tuple[dict[str, Any], float, float]:
    snapped_start, snapped_end, _first_index, _last_index = snap_clip(clip, words)
    crop = f"{CROP_WIDTH}:{CROP_HEIGHT}:{int(clip['crop_x'])}:0"
    excluded = find_excluded_word(clip, words)

    if excluded is None:
        ranges = [
            {
                "source": source_name,
                "start": snapped_start,
                "end": snapped_end,
                "crop": crop,
            }
        ]
    else:
        left_end = round(excluded["start"] - EXCLUDE_GUARD_S, 6)
        right_start = round(excluded["end"] + EXCLUDE_GUARD_S, 6)
        if not (snapped_start < left_end < right_start < snapped_end):
            raise ValueError(f"{clip['id']}: excluded word guard falls outside the clip")
        ranges = [
            {
                "source": source_name,
                "start": snapped_start,
                "end": left_end,
                "crop": crop,
            },
            {
                "source": source_name,
                "start": right_start,
                "end": snapped_end,
                "crop": crop,
            },
        ]

    total_duration = round(
        sum(float(item["end"]) - float(item["start"]) for item in ranges), 6
    )
    edl = {
        "version": 1,
        "canvas": CANVAS,
        "sources": {source_name: str(source_path)},
        "ranges": ranges,
        "grade": "auto",
        "subtitles": "master.srt",
        "total_duration_s": total_duration,
    }
    return edl, snapped_start, snapped_end


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def build_title_card(
    lines: list[str], font_path: Path, out_path: Path, canvas_wh: tuple[int, int]
) -> tuple[int, int]:
    """Render the hook title card as a transparent full-canvas PNG.

    Returns (card_width, card_bottom_y) for composition checks. The card is
    horizontally centered (Hard Rule 14 anchor: x = (canvas - card_w) / 2).
    """
    from PIL import Image, ImageDraw, ImageFont

    W, H = canvas_wh
    font = ImageFont.truetype(str(font_path), TITLE_FONT_SIZE)
    widths = [font.getbbox(l)[2] - font.getbbox(l)[0] for l in lines]
    line_h = font.getbbox("Ag")[3] - font.getbbox("Ag")[1]
    card_w = max(widths) + 2 * TITLE_PAD_X
    card_h = 2 * TITLE_PAD_Y + len(lines) * line_h + (len(lines) - 1) * TITLE_LINE_GAP
    if card_w > W - 80:
        raise ValueError(f"title too wide for canvas: {lines!r} ({card_w}px)")
    card_x = (W - card_w) // 2

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle(
        [card_x, TITLE_CARD_Y, card_x + card_w, TITLE_CARD_Y + card_h],
        radius=TITLE_RADIUS,
        fill=TITLE_CARD_FILL,
    )
    y = TITLE_CARD_Y + TITLE_PAD_Y
    for line, w in zip(lines, widths):
        bbox = font.getbbox(line)
        d.text(((W - w) // 2 - bbox[0], y - bbox[1]), line, font=font, fill=TITLE_TEXT_FILL)
        y += line_h + TITLE_LINE_GAP
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    return card_w, TITLE_CARD_Y + card_h


def render_clip(
    edl: dict[str, Any], edit_dir: Path, work_dir: Path, output_path: Path
) -> None:
    fonts_dir = edit_dir / "fonts"
    staged_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=edit_dir,
            prefix=".batch_clips_",
            suffix=".json",
            delete=False,
        ) as staged:
            json.dump(edl, staged, indent=2, ensure_ascii=False)
            staged.write("\n")
            staged_path = Path(staged.name)

        command = [
            sys.executable,
            str(RENDER_HELPER),
            str(staged_path),
            "-o",
            str(output_path),
            "--work-dir",
            str(work_dir),
            "--build-subtitles",
            "--subtitle-case",
            "natural",
            "--subtitle-font-name",
            "Montserrat Black",
            "--subtitle-fonts-dir",
            str(fonts_dir),
            "--subtitle-no-bold",
            "--subtitle-font-size",
            "14",
            "--subtitle-margin-v",
            "105",
            "--subtitle-outline",
            "1",
        ]
        subprocess.run(command, check=True, stdin=subprocess.DEVNULL)
    finally:
        if staged_path is not None:
            staged_path.unlink(missing_ok=True)


def probe_video(path: Path) -> tuple[int, int, float]:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height:format=duration",
        "-of",
        "json",
        str(path),
    ]
    completed = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    data = json.loads(completed.stdout)
    streams = data.get("streams") or []
    if not streams:
        raise ValueError(f"no video stream: {path}")
    return (
        int(streams[0]["width"]),
        int(streams[0]["height"]),
        float(data["format"]["duration"]),
    )


def parse_srt_qc(path: Path) -> tuple[int, list[dict[str, Any]], list[int]]:
    raw = path.read_text(encoding="utf-8-sig")
    blocks = re.split(r"\n\s*\n", raw.strip()) if raw.strip() else []
    flagged: list[dict[str, Any]] = []
    empty: list[int] = []
    cue_count = 0
    for block in blocks:
        lines = block.splitlines()
        timing_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            continue
        cue_count += 1
        try:
            cue_index = int(lines[0].strip()) if timing_index > 0 else cue_count
        except ValueError:
            cue_index = cue_count
        text = " ".join(line.strip() for line in lines[timing_index + 1 :]).strip()
        if not text:
            empty.append(cue_index)
        if len(text) > MAX_CUE_CHARS:
            flagged.append({"index": cue_index, "chars": len(text), "text": text})
    return cue_count, flagged, empty


def reset_subtitle_qc_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for child in path.iterdir():
        if child.is_file() and (child.suffix.lower() == ".png" or child.name == "subtitle_qc.json"):
            child.unlink()


def run_subtitle_qc(output_path: Path, srt_path: Path, qc_dir: Path) -> int:
    reset_subtitle_qc_dir(qc_dir)
    command = [
        sys.executable,
        str(SUBTITLE_CHECK_HELPER),
        str(output_path),
        "--srt",
        str(srt_path),
        "--samples",
        "5",
        "--output-dir",
        str(qc_dir),
    ]
    subprocess.run(command, check=True, stdin=subprocess.DEVNULL)
    qc_report = json.loads((qc_dir / "subtitle_qc.json").read_text(encoding="utf-8"))
    return len(qc_report.get("samples") or [])


def verify_render(
    output_path: Path, srt_path: Path, qc_dir: Path, expected_duration: float
) -> dict[str, Any]:
    failures: list[str] = []
    width, height, actual_duration = probe_video(output_path)
    if (width, height) != (1080, 1920):
        failures.append(f"dimensions {width}x{height}, expected 1080x1920")
    duration_delta = abs(actual_duration - expected_duration)
    if duration_delta > 0.35:
        failures.append(
            f"duration {actual_duration:.3f}s differs from EDL by {duration_delta:.3f}s"
        )

    cue_count, flagged_cues, empty_cues = parse_srt_qc(srt_path)
    if cue_count == 0:
        failures.append("master.srt has no cues")
    if empty_cues:
        failures.append(f"empty subtitle cues: {', '.join(map(str, empty_cues))}")

    sample_count = run_subtitle_qc(output_path, srt_path, qc_dir)
    if sample_count < 5:
        failures.append(f"subtitle_check produced {sample_count} samples, expected at least 5")

    return {
        "width": width,
        "height": height,
        "actual_duration_s": round(actual_duration, 6),
        "duration_delta_s": round(duration_delta, 6),
        "cue_count": cue_count,
        "flagged_cues": flagged_cues,
        "empty_cues": empty_cues,
        "subtitle_samples": sample_count,
        "failures": failures,
    }


def existing_pass_result(work_dir: Path, output_path: Path) -> dict[str, Any] | None:
    result_path = work_dir / "qc_result.json"
    if not result_path.is_file() or not output_path.is_file():
        return None
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if result.get("qc") != "PASS":
        return None
    if not (work_dir / "master.srt").is_file():
        return None
    if not (work_dir / "subtitle_qc" / "subtitle_qc.json").is_file():
        return None
    try:
        width, height, actual_duration = probe_video(output_path)
    except Exception:
        return None
    if (width, height) != (1080, 1920):
        return None
    if abs(actual_duration - float(result["edl_duration_s"])) > 0.35:
        return None
    result["actual_duration_s"] = round(actual_duration, 6)
    return result


def failed_result(
    clip: dict[str, Any], output_path: Path, reason: str, attempts: int = 0
) -> dict[str, Any]:
    return {
        "id": str(clip["id"]),
        "slug": str(clip["slug"]),
        "snapped_start_s": None,
        "snapped_end_s": None,
        "edl_duration_s": None,
        "actual_duration_s": None,
        "cue_count": 0,
        "flagged_cues": [],
        "empty_cues": [],
        "attempts": attempts,
        "qc": "FAILED",
        "failure_reason": reason,
        "output_path": str(output_path),
    }


def process_clip(
    clip: dict[str, Any],
    words: list[dict[str, Any]],
    edit_dir: Path,
    resume: bool,
    source_name: str,
    source_path: Path,
) -> dict[str, Any]:
    clip_id = str(clip["id"])
    slug = str(clip["slug"])
    work_dir = edit_dir / "batch" / clip_id
    output_path = edit_dir / "clips_out" / f"{clip_id}_{slug}.mp4"
    work_dir.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if resume:
        existing = existing_pass_result(work_dir, output_path)
        if existing is not None:
            print(f"\n[{clip_id}] resume: existing PASS reused", flush=True)
            return existing

    try:
        edl, snapped_start, snapped_end = build_clip_edl(
            clip, words, source_name, source_path
        )
        title_lines = clip.get("title")
        if title_lines:
            canvas_wh = tuple(int(v) for v in CANVAS.split("x"))
            build_title_card(
                [str(l) for l in title_lines],
                edit_dir / "fonts" / TITLE_FONT_FILE,
                work_dir / "title.png",
                canvas_wh,
            )
            edl["overlays"] = [
                {
                    "file": str((work_dir / "title.png").resolve()),
                    "start_in_output": 0.0,
                    "duration": edl["total_duration_s"],
                }
            ]
        write_json(work_dir / "edl.json", edl)
    except Exception as exc:
        result = failed_result(clip, output_path, f"EDL preparation: {exc}")
        write_json(work_dir / "qc_result.json", result)
        print(f"\n[{clip_id}] FAILED: {result['failure_reason']}", flush=True)
        return result

    last_reason = "unknown failure"
    last_qc: dict[str, Any] | None = None
    attempts = 0
    for attempt in range(1, MAX_RENDER_ATTEMPTS + 1):
        attempts = attempt
        print(
            f"\n[{clip_id}] render attempt {attempt}/{MAX_RENDER_ATTEMPTS}: "
            f"{snapped_start:.3f}-{snapped_end:.3f}",
            flush=True,
        )
        try:
            render_clip(edl, edit_dir, work_dir, output_path)
            last_qc = verify_render(
                output_path,
                work_dir / "master.srt",
                work_dir / "subtitle_qc",
                float(edl["total_duration_s"]),
            )
            if not last_qc["failures"]:
                result = {
                    "id": clip_id,
                    "slug": slug,
                    "snapped_start_s": snapped_start,
                    "snapped_end_s": snapped_end,
                    "edl_duration_s": edl["total_duration_s"],
                    "actual_duration_s": last_qc["actual_duration_s"],
                    "duration_delta_s": last_qc["duration_delta_s"],
                    "width": last_qc["width"],
                    "height": last_qc["height"],
                    "cue_count": last_qc["cue_count"],
                    "flagged_cues": last_qc["flagged_cues"],
                    "empty_cues": last_qc["empty_cues"],
                    "subtitle_samples": last_qc["subtitle_samples"],
                    "attempts": attempts,
                    "qc": "PASS",
                    "failure_reason": "",
                    "output_path": str(output_path),
                }
                write_json(work_dir / "qc_result.json", result)
                print(
                    f"[{clip_id}] PASS: {last_qc['width']}x{last_qc['height']}, "
                    f"{last_qc['actual_duration_s']:.3f}s, "
                    f"{last_qc['cue_count']} cues",
                    flush=True,
                )
                return result
            last_reason = "; ".join(last_qc["failures"])
        except Exception as exc:
            last_reason = f"{type(exc).__name__}: {exc}"
        print(f"[{clip_id}] attempt {attempt} failed: {last_reason}", flush=True)

    result = {
        "id": clip_id,
        "slug": slug,
        "snapped_start_s": snapped_start,
        "snapped_end_s": snapped_end,
        "edl_duration_s": edl["total_duration_s"],
        "actual_duration_s": last_qc.get("actual_duration_s") if last_qc else None,
        "cue_count": last_qc.get("cue_count", 0) if last_qc else 0,
        "flagged_cues": last_qc.get("flagged_cues", []) if last_qc else [],
        "empty_cues": last_qc.get("empty_cues", []) if last_qc else [],
        "attempts": attempts,
        "qc": "FAILED",
        "failure_reason": last_reason,
        "output_path": str(output_path),
    }
    write_json(work_dir / "qc_result.json", result)
    print(f"[{clip_id}] FAILED after {attempts} attempts: {last_reason}", flush=True)
    return result


def _md_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def flagged_summary(result: dict[str, Any]) -> str:
    parts: list[str] = []
    flagged = result.get("flagged_cues") or []
    empty = result.get("empty_cues") or []
    if flagged:
        details = "; ".join(
            f"#{cue['index']} ({cue['chars']} chars): {cue['text']}" for cue in flagged
        )
        parts.append(f"{len(flagged)} over {MAX_CUE_CHARS}: {details}")
    if empty:
        parts.append(f"{len(empty)} empty: {', '.join('#' + str(index) for index in empty)}")
    return "; ".join(parts) if parts else "0"


def format_seconds(value: Any) -> str:
    return "—" if value is None else f"{float(value):.3f}"


def write_batch_report(path: Path, results: list[dict[str, Any]]) -> None:
    lines = [
        "# Batch Vertical Clips Report",
        "",
        "| ID | Slug | Snapped start (s) | Snapped end (s) | Duration (s) | Cue count | Wrapped/flagged cues | QC | Output path |",
        "|---|---|---:|---:|---:|---:|---|---|---|",
    ]
    for result in results:
        qc = str(result.get("qc") or "FAILED")
        if qc == "FAILED" and result.get("failure_reason"):
            qc = f"FAILED — {result['failure_reason']}"
        row = [
            result.get("id", ""),
            result.get("slug", ""),
            format_seconds(result.get("snapped_start_s")),
            format_seconds(result.get("snapped_end_s")),
            format_seconds(result.get("edl_duration_s")),
            result.get("cue_count", 0),
            flagged_summary(result),
            qc,
            result.get("output_path", ""),
        ]
        lines.append("| " + " | ".join(_md_cell(value) for value in row) + " |")

    passed = sum(result.get("qc") == "PASS" for result in results)
    lines.extend(
        [
            "",
            f"Batch result: {passed}/{len(results)} clips passed automated QC.",
            "",
            "## Engine changes",
            "",
            "- `helpers/render.py` — added isolated `--work-dir` run products and `-nostdin` on ffmpeg invocations while preserving EDL-relative inputs.",
            "- `helpers/batch_clips.py` — added transcript snapping, excluded-word splitting, final rendering, per-clip QC, retry, resume, and report generation.",
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path, help="Batch spec JSON path")
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="ID",
        help="Render only this clip id; repeat to select more than one.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Reuse clips whose persisted result and current artifacts still pass QC.",
    )
    args = parser.parse_args()

    spec_path = args.spec.resolve()
    if not spec_path.is_file():
        parser.error(f"spec not found: {spec_path}")
    edit_dir = spec_path.parent
    project_dir = edit_dir.parent
    if edit_dir.name != "edit":
        parser.error("batch spec must live directly under the media project's edit directory")
    try:
        source_name, source_path, clips = load_spec(spec_path)
    except ValueError as exc:
        parser.error(str(exc))
    if not source_path.is_file():
        parser.error(f"source not found: {source_path}")
    transcript_path = edit_dir / "transcripts" / f"{source_name}.json"
    fonts_dir = edit_dir / "fonts"
    if not transcript_path.is_file():
        parser.error(f"transcript not found: {transcript_path}")
    if not (fonts_dir / "Montserrat-Black.ttf").is_file():
        parser.error(f"font not found: {fonts_dir / 'Montserrat-Black.ttf'}")
    if not RENDER_HELPER.is_file() or not SUBTITLE_CHECK_HELPER.is_file():
        parser.error("render or subtitle QC helper is missing")
    if args.only:
        wanted = set(args.only)
        known = {str(clip["id"]) for clip in clips}
        unknown = wanted - known
        if unknown:
            parser.error(f"unknown --only id(s): {', '.join(sorted(unknown))}")
        clips = [clip for clip in clips if str(clip["id"]) in wanted]

    print(f"project: {project_dir}", flush=True)
    print(f"clips: {len(clips)}", flush=True)
    words = load_transcript_words(transcript_path)
    results: list[dict[str, Any]] = []
    for clip in clips:
        try:
            results.append(
                process_clip(
                    clip, words, edit_dir, resume=args.resume,
                    source_name=source_name, source_path=source_path,
                )
            )
        except Exception as exc:
            output_path = edit_dir / "clips_out" / f"{clip['id']}_{clip['slug']}.mp4"
            result = failed_result(clip, output_path, f"unhandled: {type(exc).__name__}: {exc}")
            write_json(edit_dir / "batch" / str(clip["id"]) / "qc_result.json", result)
            results.append(result)
            print(f"[{clip['id']}] FAILED: {result['failure_reason']}", flush=True)

    report_path = edit_dir / "clips_out" / "BATCH_REPORT.md"
    write_batch_report(report_path, results)
    passed = sum(result.get("qc") == "PASS" for result in results)
    print(f"\nreport: {report_path}", flush=True)
    print(f"batch complete: {passed}/{len(results)} passed", flush=True)
    if passed != len(results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
