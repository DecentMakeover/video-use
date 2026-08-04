"""Compile a long-form removal cutlist into a render-ready EDL.

The cutlist is the reversible source of truth for a podcast-style edit:
an explicitly confirmed part order, removal ranges in source time (each
with a category and rationale), and optional chapter markers. This
helper owns the deterministic half of that contract — it validates
every cut edge against probed media, inverts approved cuts into keep
ranges, derives output timings, and emits an EDL v1 that render.py
consumes unchanged. Editorial judgment (what to cut and why) stays in
the workflow prompts; this tool only checks and transforms.

Cutlist schema (paths resolve relative to the cutlist's directory, the
same convention render.py uses for EDL paths; keep source keys equal to
each file's stem so `render.py --build-subtitles` finds transcripts):

    {
      "version": 1,
      "parts":    [{"source": "episode_part1", "file": "../episode_part1.mp4"}],
      "cuts":     [{"source": "episode_part1", "start": 870.0, "end": 889.0,
                    "category": "false_start", "reason": "...", "quote": "..."}],
      "chapters": [{"source": "episode_part1", "at": 120.0, "title": "Intro"}]
    }

Usage:
    python helpers/cutlist.py validate edit/cutlist.json
    python helpers/cutlist.py compile  edit/cutlist.json          # -> edit/edl.json + edit/cut_report.md
    python helpers/cutlist.py compile  edit/cutlist.json --min-keep 0.5
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

EPSILON = 1e-6
# ffprobe container durations can disagree with stream time by a few ms;
# allow cut/chapter edges to overshoot the probed duration by this much.
EDGE_TOLERANCE = 0.05
DEFAULT_MIN_KEEP = 1.0


class CutlistError(ValueError):
    """Raised by compile_cutlist when the cutlist fails validation."""

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("\n".join(self.errors))


def resolve_path(p: str, base: Path) -> Path:
    path = Path(p).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def fmt_time(seconds: float) -> str:
    ms = round(seconds * 1000)
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def schema_errors(cutlist) -> list[str]:
    """Structural checks that need no media on disk."""
    if not isinstance(cutlist, dict):
        return ["cutlist must be a JSON object"]

    errors: list[str] = []
    if cutlist.get("version") != 1:
        errors.append("version must be 1")

    parts = cutlist.get("parts")
    known_sources: set[str] = set()
    if not isinstance(parts, list) or not parts:
        errors.append("parts must be a non-empty list confirming the part order")
    else:
        for i, part in enumerate(parts):
            if not isinstance(part, dict):
                errors.append(f"parts[{i}] must be an object")
                continue
            source = part.get("source")
            file = part.get("file")
            if not isinstance(source, str) or not source:
                errors.append(f"parts[{i}] needs a non-empty 'source' name")
                continue
            if not isinstance(file, str) or not file:
                errors.append(f"parts[{i}] ({source}) needs a non-empty 'file' path")
            if source in known_sources:
                errors.append(f"duplicate part source name: {source}")
            known_sources.add(source)

    cuts = cutlist.get("cuts", [])
    per_source: dict[str, list[tuple[float, float]]] = {}
    if not isinstance(cuts, list):
        errors.append("cuts must be a list")
        cuts = []
    for i, cut in enumerate(cuts):
        if not isinstance(cut, dict):
            errors.append(f"cuts[{i}] must be an object")
            continue
        source = cut.get("source")
        start = cut.get("start")
        end = cut.get("end")
        if source not in known_sources:
            errors.append(f"cuts[{i}] references unknown source: {source!r}")
            continue
        if not _is_number(start) or not _is_number(end):
            errors.append(f"cuts[{i}] ({source}) start/end must be numbers")
            continue
        if start < 0:
            errors.append(f"cuts[{i}] ({source}) start must be >= 0, got {start}")
            continue
        if end <= start + EPSILON:
            errors.append(f"cuts[{i}] ({source}) end must be after start, got {start}-{end}")
            continue
        per_source.setdefault(source, []).append((float(start), float(end)))

    for source, ranges in per_source.items():
        ranges.sort()
        for (_, prev_end), (next_start, next_end) in zip(ranges, ranges[1:]):
            if next_start < prev_end - EPSILON:
                errors.append(
                    f"overlapping cuts on {source}: "
                    f"{fmt_time(next_start)}-{fmt_time(next_end)} starts before "
                    f"an earlier cut ends at {fmt_time(prev_end)}"
                )

    chapters = cutlist.get("chapters", [])
    if not isinstance(chapters, list):
        errors.append("chapters must be a list")
        chapters = []
    for i, chapter in enumerate(chapters):
        if not isinstance(chapter, dict):
            errors.append(f"chapters[{i}] must be an object")
            continue
        source = chapter.get("source")
        at = chapter.get("at")
        title = chapter.get("title")
        if source not in known_sources:
            errors.append(f"chapters[{i}] references unknown source: {source!r}")
            continue
        if not _is_number(at) or at < 0:
            errors.append(f"chapters[{i}] ({source}) 'at' must be a number >= 0")
            continue
        if not isinstance(title, str) or not title.strip():
            errors.append(f"chapters[{i}] ({source}) needs a non-empty 'title'")
        for cut_start, cut_end in per_source.get(source, []):
            if cut_start + EPSILON < at < cut_end - EPSILON:
                errors.append(
                    f"chapter {title!r} at {fmt_time(at)} on {source} falls inside the "
                    f"removed range {fmt_time(cut_start)}-{fmt_time(cut_end)}; "
                    "move the marker or the cut"
                )
                break

    return errors


def media_errors(cutlist: dict, durations: dict[str, float]) -> list[str]:
    """Checks that need each part's probed duration (seconds)."""
    errors: list[str] = []
    for part in cutlist.get("parts", []):
        source = part.get("source")
        duration = durations.get(source)
        if duration is None:
            errors.append(f"no probed duration for part: {source}")
        elif duration <= 0:
            errors.append(f"non-positive duration for part {source}: {duration}")

    for i, cut in enumerate(cutlist.get("cuts", [])):
        duration = durations.get(cut.get("source"))
        if duration is None or not _is_number(cut.get("end")):
            continue
        if cut["end"] > duration + EDGE_TOLERANCE:
            errors.append(
                f"cuts[{i}] ({cut['source']}) ends at {fmt_time(cut['end'])}, "
                f"past the source duration {fmt_time(duration)}"
            )

    for chapter in cutlist.get("chapters", []):
        duration = durations.get(chapter.get("source"))
        if duration is None or not _is_number(chapter.get("at")):
            continue
        if chapter["at"] > duration + EDGE_TOLERANCE:
            errors.append(
                f"chapter {chapter.get('title')!r} at {fmt_time(chapter['at'])} is past "
                f"the source duration {fmt_time(duration)} of {chapter['source']}"
            )

    return errors


def merge_cuts(cuts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Sort cuts and merge touching/adjacent ranges. Assumes no overlaps."""
    merged: list[tuple[float, float]] = []
    for start, end in sorted(cuts):
        if merged and start <= merged[-1][1] + EPSILON:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def invert_cuts(duration: float, cuts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Complement of the (validated) cut ranges within [0, duration]."""
    keeps: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in merge_cuts(cuts):
        start = max(0.0, min(start, duration))
        end = max(0.0, min(end, duration))
        if start - cursor > EPSILON:
            keeps.append((cursor, start))
        cursor = max(cursor, end)
    if duration - cursor > EPSILON:
        keeps.append((cursor, duration))
    return keeps


def _transcript_words(transcripts_dir: Path, part: dict) -> list[dict] | None:
    """Load cached Scribe words for a part, or None if no transcript exists.

    render.py looks transcripts up by EDL source key; transcribe.py caches
    by video filename stem — try both.
    """
    for stem in (part["source"], Path(part["file"]).stem):
        path = transcripts_dir / f"{stem}.json"
        if path.exists():
            try:
                transcript = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
            return [
                w
                for w in transcript.get("words", [])
                if w.get("type") == "word"
                and w.get("start") is not None
                and w.get("end") is not None
            ]
    return None


def word_boundary_warnings(
    cutlist: dict, words_by_source: dict[str, list[dict]]
) -> list[str]:
    """Flag cut edges that land strictly inside a transcribed word."""
    warnings: list[str] = []
    for cut in cutlist.get("cuts", []):
        words = words_by_source.get(cut.get("source")) or []
        for edge_name in ("start", "end"):
            t = cut.get(edge_name)
            if not _is_number(t):
                continue
            for w in words:
                if w["start"] + EPSILON < t < w["end"] - EPSILON:
                    warnings.append(
                        f"cut {edge_name} {fmt_time(t)} on {cut['source']} lands mid-word "
                        f"({w.get('text', '').strip()!r} spans "
                        f"{fmt_time(w['start'])}-{fmt_time(w['end'])}); snap to a word boundary"
                    )
                    break
    return warnings


def compile_cutlist(
    cutlist: dict,
    durations: dict[str, float],
    min_keep: float = DEFAULT_MIN_KEEP,
    words_by_source: dict[str, list[dict]] | None = None,
) -> dict:
    """Validate and compile a cutlist. Raises CutlistError on invalid input.

    Returns a dict with:
      edl        — render.py-compatible {"sources": ..., "ranges": ...}
      segments   — kept ranges with derived output_in/output_out
      chapters   — chapter markers mapped to output time
      parts      — per-part duration/kept/removed/cut-count summary
      totals     — input/output/removed seconds + removed_by_category
      warnings   — evidence for the agent (short keeps, mid-word edges, ...)
    """
    errors = schema_errors(cutlist) + media_errors(cutlist, durations)
    if errors:
        raise CutlistError(errors)

    warnings: list[str] = []
    cuts_by_source: dict[str, list[tuple[float, float]]] = {}
    for cut in cutlist.get("cuts", []):
        cuts_by_source.setdefault(cut["source"], []).append(
            (float(cut["start"]), float(cut["end"]))
        )

    segments: list[dict] = []
    parts_summary: list[dict] = []
    offset = 0.0
    for part in cutlist["parts"]:
        source = part["source"]
        if source != Path(part["file"]).stem:
            warnings.append(
                f"source key {source!r} differs from file stem "
                f"{Path(part['file']).stem!r}; render.py --build-subtitles looks up "
                "transcripts by source key"
            )
        duration = durations[source]
        kept = 0.0
        for start, end in invert_cuts(duration, cuts_by_source.get(source, [])):
            length = end - start
            segments.append(
                {
                    "source": source,
                    "source_in": start,
                    "source_out": end,
                    "output_in": offset,
                    "output_out": offset + length,
                }
            )
            if length < min_keep - EPSILON:
                warnings.append(
                    f"kept segment {fmt_time(start)}-{fmt_time(end)} on {source} is only "
                    f"{length:.2f}s (min-keep {min_keep:.2f}s); merge the surrounding cuts "
                    "or confirm it is intentional"
                )
            offset += length
            kept += length
        parts_summary.append(
            {
                "source": source,
                "file": part["file"],
                "duration": duration,
                "kept": kept,
                "removed": duration - kept,
                "cut_count": len(cuts_by_source.get(source, [])),
            }
        )

    chapters_out: list[dict] = []
    for chapter in cutlist.get("chapters", []):
        at = float(chapter["at"])
        mapped = None
        for seg in segments:
            if seg["source"] != chapter["source"]:
                continue
            if seg["source_in"] - EDGE_TOLERANCE <= at <= seg["source_out"] + EDGE_TOLERANCE:
                clamped = min(max(at, seg["source_in"]), seg["source_out"])
                mapped = seg["output_in"] + (clamped - seg["source_in"])
                break
        if mapped is None:
            # Validated chapters only miss when every containing range was removed.
            raise CutlistError(
                [
                    f"chapter {chapter['title']!r} at {fmt_time(at)} on {chapter['source']} "
                    "has no kept segment to land in; move the marker or restore content"
                ]
            )
        chapters_out.append(
            {
                "title": chapter["title"],
                "source": chapter["source"],
                "source_at": at,
                "output_at": mapped,
            }
        )
    chapters_out.sort(key=lambda c: c["output_at"])

    if words_by_source is None:
        words_by_source = {}
    warnings.extend(word_boundary_warnings(cutlist, words_by_source))

    removed_by_category: dict[str, float] = {}
    for cut in cutlist.get("cuts", []):
        duration = durations[cut["source"]]
        length = min(float(cut["end"]), duration) - max(float(cut["start"]), 0.0)
        category = cut.get("category") or "uncategorized"
        removed_by_category[category] = removed_by_category.get(category, 0.0) + length

    files_by_source = {p["source"]: p["file"] for p in cutlist["parts"]}
    used_sources = {seg["source"] for seg in segments}
    edl = {
        "sources": {s: files_by_source[s] for s in files_by_source if s in used_sources},
        "ranges": [
            {"source": seg["source"], "start": seg["source_in"], "end": seg["source_out"]}
            for seg in segments
        ],
    }

    total_input = sum(p["duration"] for p in parts_summary)
    total_output = sum(p["kept"] for p in parts_summary)
    return {
        "edl": edl,
        "segments": segments,
        "chapters": chapters_out,
        "parts": parts_summary,
        "totals": {
            "input": total_input,
            "output": total_output,
            "removed": total_input - total_output,
            "removed_by_category": removed_by_category,
        },
        "warnings": warnings,
    }


def build_report_md(cutlist_name: str, result: dict) -> str:
    totals = result["totals"]
    removed_pct = 100.0 * totals["removed"] / totals["input"] if totals["input"] else 0.0

    lines = [
        "# Cut report",
        "",
        f"Derived from `{cutlist_name}`. The cutlist is the reversible source of "
        "truth — edit it and recompile rather than editing `edl.json` by hand.",
        "",
        "## Part map",
        "",
        "| # | Source | File | Duration | Kept | Removed | Cuts |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for i, part in enumerate(result["parts"], start=1):
        lines.append(
            f"| {i} | {part['source']} | `{part['file']}` | {fmt_time(part['duration'])} "
            f"| {fmt_time(part['kept'])} | {fmt_time(part['removed'])} | {part['cut_count']} |"
        )

    lines += [
        "",
        "## Totals",
        "",
        f"- Input: {fmt_time(totals['input'])}",
        f"- Output: {fmt_time(totals['output'])}",
        f"- Removed: {fmt_time(totals['removed'])} ({removed_pct:.1f}%)",
    ]
    for category, seconds in sorted(totals["removed_by_category"].items()):
        lines.append(f"  - {category}: {fmt_time(seconds)}")

    lines += [
        "",
        "## Output timeline",
        "",
        "| Output | Source | Source range |",
        "| --- | --- | --- |",
    ]
    for seg in result["segments"]:
        lines.append(
            f"| {fmt_time(seg['output_in'])}-{fmt_time(seg['output_out'])} "
            f"| {seg['source']} | {fmt_time(seg['source_in'])}-{fmt_time(seg['source_out'])} |"
        )

    lines += ["", "## Chapters", ""]
    if result["chapters"]:
        lines += ["| Output time | Title | Source reference |", "| --- | --- | --- |"]
        for chapter in result["chapters"]:
            lines.append(
                f"| {fmt_time(chapter['output_at'])} | {chapter['title']} "
                f"| {chapter['source']} @ {fmt_time(chapter['source_at'])} |"
            )
    else:
        lines.append("None.")

    lines += ["", "## Warnings", ""]
    if result["warnings"]:
        lines += [f"- {w}" for w in result["warnings"]]
    else:
        lines.append("None.")
    lines.append("")
    return "\n".join(lines)


def probe_duration(path: Path) -> float:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(out.stdout.strip())


def _load_and_validate(cutlist_path: Path) -> tuple[dict, dict[str, float]]:
    """Load the cutlist, fail fast on schema errors, then probe durations."""
    try:
        cutlist = json.loads(cutlist_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"invalid JSON in {cutlist_path}: {exc}")

    errors = schema_errors(cutlist)
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        sys.exit(1)

    durations: dict[str, float] = {}
    for part in cutlist["parts"]:
        source_path = resolve_path(part["file"], cutlist_path.parent)
        if not source_path.exists():
            print(f"error: source file not found: {source_path}", file=sys.stderr)
            sys.exit(1)
        try:
            durations[part["source"]] = probe_duration(source_path)
        except (subprocess.CalledProcessError, ValueError) as exc:
            sys.exit(f"ffprobe failed for {source_path}: {exc}")
    return cutlist, durations


def _collect_words(cutlist: dict, cutlist_path: Path) -> dict[str, list[dict]]:
    transcripts_dir = cutlist_path.parent / "transcripts"
    words: dict[str, list[dict]] = {}
    if transcripts_dir.is_dir():
        for part in cutlist["parts"]:
            part_words = _transcript_words(transcripts_dir, part)
            if part_words is not None:
                words[part["source"]] = part_words
    return words


def _run(cutlist_path: Path, min_keep: float) -> dict:
    cutlist, durations = _load_and_validate(cutlist_path)
    try:
        return compile_cutlist(
            cutlist,
            durations,
            min_keep=min_keep,
            words_by_source=_collect_words(cutlist, cutlist_path),
        )
    except CutlistError as exc:
        for error in exc.errors:
            print(f"error: {error}", file=sys.stderr)
        sys.exit(1)


def _print_summary(result: dict) -> None:
    totals = result["totals"]
    print(
        f"{len(result['parts'])} part(s), "
        f"{sum(p['cut_count'] for p in result['parts'])} cut(s): "
        f"{fmt_time(totals['input'])} -> {fmt_time(totals['output'])} "
        f"(removed {fmt_time(totals['removed'])})"
    )
    for warning in result["warnings"]:
        print(f"warning: {warning}")


def _same_dir_or_exit(out_path: Path, cutlist_path: Path, flag: str) -> None:
    if out_path.resolve().parent != cutlist_path.resolve().parent:
        sys.exit(
            f"{flag} must stay in the cutlist's directory so the EDL's "
            "relative source paths keep resolving"
        )


def main() -> None:
    ap = argparse.ArgumentParser(description="Validate/compile a removal cutlist into an EDL")
    sub = ap.add_subparsers(dest="command", required=True)

    val = sub.add_parser("validate", help="check the cutlist without writing anything")
    val.add_argument("cutlist", type=Path)
    val.add_argument("--min-keep", type=float, default=DEFAULT_MIN_KEEP)

    comp = sub.add_parser("compile", help="validate, then write edl.json + cut_report.md")
    comp.add_argument("cutlist", type=Path)
    comp.add_argument("-o", "--output", type=Path, default=None, help="EDL path (default: <cutlist dir>/edl.json)")
    comp.add_argument("--report", type=Path, default=None, help="report path (default: <cutlist dir>/cut_report.md)")
    comp.add_argument("--min-keep", type=float, default=DEFAULT_MIN_KEEP)

    args = ap.parse_args()
    cutlist_path = args.cutlist.resolve()
    if not cutlist_path.exists():
        sys.exit(f"cutlist not found: {cutlist_path}")

    result = _run(cutlist_path, args.min_keep)

    if args.command == "validate":
        _print_summary(result)
        print("cutlist is valid")
        return

    edl_path = args.output or cutlist_path.parent / "edl.json"
    report_path = args.report or cutlist_path.parent / "cut_report.md"
    _same_dir_or_exit(edl_path, cutlist_path, "-o/--output")
    _same_dir_or_exit(report_path, cutlist_path, "--report")

    edl_path.write_text(json.dumps(result["edl"], indent=2) + "\n", encoding="utf-8")
    report_path.write_text(build_report_md(cutlist_path.name, result), encoding="utf-8")
    _print_summary(result)
    print(f"wrote {edl_path}")
    print(f"wrote {report_path}")


if __name__ == "__main__":
    main()
