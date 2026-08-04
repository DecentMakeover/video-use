"""Render a video from an EDL.

Implements the HEURISTICS render pipeline in the correct order:

  1. Per-segment extract with color grade + 30ms audio fades baked in
  2. Lossless -c copy concat into base.mp4
  3. If overlays or subtitles: single filter graph that overlays animations
     (with PTS shift so frame 0 lands at the overlay window start)
     and applies `subtitles` filter LAST → final.mp4

Optionally builds a master SRT from the per-source transcripts + EDL
output-timeline offsets. Subtitle size and placement are aspect-aware starting
points and can be overridden per render; they still require full-resolution
visual QC via helpers/subtitle_check.py.

Usage:
    python helpers/render.py <edl.json> -o final.mp4
    python helpers/render.py <edl.json> -o preview.mp4 --preview
    python helpers/render.py <edl.json> -o final.mp4 --build-subtitles
    python helpers/render.py <edl.json> -o preview.mp4 --build-subtitles \
      --subtitle-case natural --subtitle-font-size 10 --subtitle-margin-v 90
    python helpers/render.py <edl.json> -o final.mp4 --no-subtitles
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

try:
    from grade import get_preset, auto_grade_for_clip  # same directory
except Exception:
    def get_preset(name: str) -> str:
        return ""

    def auto_grade_for_clip(video, start=0.0, duration=None, verbose=False):  # type: ignore
        return "eq=contrast=1.03:saturation=0.98", {}


# -------- Subtitle style -----------------------------------------------------
#
# libass scales SRT force_style values against a 288-line reference canvas.
# A FontSize of 18 therefore becomes roughly 120 px on a 1080x1920 render.
# That is reasonable for some landscape videos but oversized for portrait
# interviews. These defaults are only starting points; subtitle_check.py must
# be used to inspect full-resolution frames before a final render is approved.

PORTRAIT_SUBTITLE_DEFAULTS = {
    "font_size": 10.0,
    "margin_v": 90,
    "outline": 1.5,
}

LANDSCAPE_SUBTITLE_DEFAULTS = {
    "font_size": 18.0,
    "margin_v": 35,
    "outline": 2.0,
}

# -------- Helpers ------------------------------------------------------------


def run(cmd: list[str], quiet: bool = False) -> None:
    if not quiet:
        print(f"  $ {' '.join(str(c) for c in cmd[:6])}{' …' if len(cmd) > 6 else ''}")
    subprocess.run(cmd, check=True)


def video_dimensions(video: Path) -> tuple[int, int]:
    """Return displayed width/height for an already-rendered video."""
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
    width, height = map(int, out.stdout.strip().split(",")[:2])
    return width, height


# -------- Target-canvas reframing --------------------------------------------
#
# An EDL may declare a delivery canvas ("canvas": "1080x1920") that differs
# from the source aspect (e.g. vertical social clips from a landscape
# interview). Each range is cropped to the canvas aspect in source pixels,
# then scaled to the canvas. Per-range "crop" ("w:h:x:y", source pixels)
# positions the window on the subject; without it the crop is centered.


def parse_canvas(value: str | None) -> tuple[int, int] | None:
    """Parse an EDL canvas spec like "1080x1920" into (width, height)."""
    if not value:
        return None
    m = re.fullmatch(r"(\d+)\s*[xX]\s*(\d+)", str(value).strip())
    if not m:
        raise ValueError(f"invalid canvas spec: {value!r} (expected WIDTHxHEIGHT)")
    return int(m.group(1)), int(m.group(2))


def displayed_dimensions(video: Path) -> tuple[int, int]:
    """Displayed width/height of a source, accounting for rotation metadata."""
    w, h = video_dimensions(video)
    if abs(_stream_rotation(video)) % 180 == 90:
        w, h = h, w
    return w, h


def centered_crop(src_w: int, src_h: int, canvas: tuple[int, int]) -> str:
    """Largest centered crop window matching the canvas aspect, as "w:h:x:y"."""
    cw, ch = canvas
    target_ar = cw / ch
    if src_w / src_h > target_ar:
        crop_h = src_h
        crop_w = min(src_w, int(round(src_h * target_ar)))
    else:
        crop_w = src_w
        crop_h = min(src_h, int(round(src_w / target_ar)))
    crop_w -= crop_w % 2
    crop_h -= crop_h % 2
    x = (src_w - crop_w) // 2
    y = (src_h - crop_h) // 2
    return f"{crop_w}:{crop_h}:{x}:{y}"


def validate_crop(crop: str, src_w: int, src_h: int, range_index: int) -> str:
    """Validate a per-range crop spec "w:h:x:y" against source bounds."""
    parts = str(crop).split(":")
    if len(parts) != 4:
        raise ValueError(
            f"range {range_index}: invalid crop {crop!r} (expected w:h:x:y)"
        )
    try:
        w, h, x, y = (int(p) for p in parts)
    except ValueError:
        raise ValueError(
            f"range {range_index}: crop {crop!r} must be four integers"
        ) from None
    if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > src_w or y + h > src_h:
        raise ValueError(
            f"range {range_index}: crop {crop!r} outside source {src_w}x{src_h}"
        )
    return f"{w}:{h}:{x}:{y}"


def build_subtitle_force_style(
    video: Path,
    font_size: float | None = None,
    margin_v: int | None = None,
    alignment: int = 2,
    outline: float | None = None,
    raw_style: str | None = None,
    font_name: str | None = None,
    bold: bool = True,
) -> str:
    """Build an ASS force_style with aspect-aware defaults.

    Portrait defaults are deliberately smaller than the historical
    bold-overlay values and start above the busiest platform UI region. Every
    result still requires full-resolution subtitle QC.

    Set bold=False for single-weight display fonts (Anton, Archivo Black, …)
    where libass faux-bold would distort the glyphs.
    """
    if raw_style:
        return raw_style

    width, height = video_dimensions(video)
    defaults = (
        PORTRAIT_SUBTITLE_DEFAULTS
        if height > width
        else LANDSCAPE_SUBTITLE_DEFAULTS
    )
    chosen_font_size = font_size if font_size is not None else defaults["font_size"]
    chosen_margin_v = margin_v if margin_v is not None else defaults["margin_v"]
    chosen_outline = outline if outline is not None else defaults["outline"]
    chosen_font_name = font_name or "Helvetica"

    return (
        f"FontName={chosen_font_name},FontSize={chosen_font_size:g},Bold={1 if bold else 0},"
        "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H00000000,"
        f"BorderStyle=1,Outline={chosen_outline:g},Shadow=0,"
        f"Alignment={alignment},MarginV={chosen_margin_v}"
    )


def resolve_grade_filter(grade_field: str | None) -> str:
    """The EDL's 'grade' field can be a preset name, a raw ffmpeg filter, or 'auto'.

    Returns the filter string to embed into the per-segment -vf chain.
    For 'auto', returns the sentinel "__AUTO__" which is resolved per-segment.
    """
    if not grade_field:
        return ""
    if grade_field == "auto":
        return "__AUTO__"
    # Preset names are short identifiers, filter strings contain '=' or ','.
    if re.fullmatch(r"[a-zA-Z0-9_\-]+", grade_field):
        try:
            return get_preset(grade_field)
        except KeyError:
            print(f"warning: unknown preset '{grade_field}', using as raw filter")
            return grade_field
    return grade_field


def resolve_path(maybe_path: str, base: Path) -> Path:
    """Resolve a path that may be absolute or relative to `base`."""
    p = Path(maybe_path)
    if p.is_absolute():
        return p
    return (base / p).resolve()


# -------- HDR → SDR tone mapping (HLG / PQ sources) --------------------------
#
# iPhone defaults to HLG HDR in Rec.2020 (and many mirrorless cameras ship PQ).
# If the source is HDR and we only downconvert bit depth (yuv420p10le → yuv420p)
# without tone-mapping, the output is 8-bit but still carries HLG/PQ transfer
# metadata. Players that honor the metadata (screen recorders, most social
# upload re-encodes) interpret 8-bit values in an HDR container and the result
# looks oversaturated / blown out. QuickTime on macOS can hide this locally —
# screen recording and uploaded renders cannot.
#
# Fix: detect HDR via color_transfer and prepend a zscale+tonemap chain to the
# vf graph so the output is clean Rec.709 SDR.

HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}  # PQ (HDR10) and HLG

TONEMAP_CHAIN = (
    "zscale=t=linear:npl=100,"
    "format=gbrpf32le,"
    "zscale=p=bt709,"
    "tonemap=tonemap=hable:desat=0,"
    "zscale=t=bt709:m=bt709:r=tv,"
    "format=yuv420p"
)


def is_hdr_source(video: Path) -> bool:
    """Return True if the source uses a PQ or HLG transfer function."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=color_transfer",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip() in HDR_TRANSFERS
    except subprocess.CalledProcessError:
        return False


def _stream_rotation(video: Path) -> int:
    """Return the display-matrix rotation in degrees (0/±90/180/270), 0 if none.

    Phones shoot in a fixed sensor orientation and record a rotation display
    matrix (iPhone: side_data 'rotation'; legacy: stream tag 'rotate'). ffmpeg
    auto-applies it on decode, so the *displayed* frame can be portrait even
    when the coded frame is landscape.
    """
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream_side_data=rotation:stream_tags=rotate",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
            capture_output=True, text=True, check=True,
        )
        for line in out.stdout.splitlines():
            line = line.strip()
            if not line or line == "N/A":
                continue
            try:
                return int(round(float(line)))
            except ValueError:
                continue
    except subprocess.CalledProcessError:
        pass
    return 0


def is_portrait_source(video: Path) -> bool:
    """Return True if the *displayed* frame (after rotation) is portrait.

    Reads coded width/height and swaps them when a ±90°/270° display-matrix
    rotation is present, matching what ffmpeg feeds the filter chain on decode.
    """
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "csv=p=0", str(video)],
            capture_output=True, text=True, check=True,
        )
        w, h = map(int, out.stdout.strip().split(",")[:2])
        if abs(_stream_rotation(video)) % 180 == 90:
            w, h = h, w
        return h > w
    except Exception:
        return False


# -------- Per-segment extraction (Rule 2 + Rule 3) --------------------------


def extract_segment(
    source: Path,
    seg_start: float,
    duration: float,
    grade_filter: str,
    out_path: Path,
    preview: bool = False,
    draft: bool = False,
    crop_filter: str = "",
    canvas: tuple[int, int] | None = None,
) -> None:
    """Extract a cut range as its own MP4 with grade + 30ms audio fades baked in.

    `-ss` before `-i` for fast accurate seeking. Scale to 1080p from 4K.
    Portrait sources (height > width) are scaled by height to preserve orientation.
    With a target canvas, the range is cropped to the canvas aspect (crop_filter,
    source pixels) and scaled to the exact canvas size instead.

    Quality ladder:
      - final (default): 1080p libx264 fast CRF 20
      - preview:         1080p libx264 medium CRF 22 (evaluable for QC)
      - draft:           720p libx264 ultrafast CRF 28 (cut-point check only)
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if canvas:
        cw, ch = canvas
        if draft:
            cw, ch = max(2, cw // 2), max(2, ch // 2)
            cw -= cw % 2
            ch -= ch % 2
        scale = f"scale={cw}:{ch}"
    else:
        portrait = is_portrait_source(source)
        if draft:
            scale = "scale=-2:1280" if portrait else "scale=1280:-2"
        else:
            scale = "scale=-2:1920" if portrait else "scale=1920:-2"

    vf_parts: list[str] = []
    if is_hdr_source(source):
        vf_parts.append(TONEMAP_CHAIN)
    if crop_filter:
        vf_parts.append(f"crop={crop_filter}")
    vf_parts.append(scale)
    if grade_filter:
        vf_parts.append(grade_filter)
    vf = ",".join(vf_parts)

    # 30ms audio fades at both edges (Rule 3) — prevent pops
    fade_out_start = max(0.0, duration - 0.03)
    af = f"afade=t=in:st=0:d=0.03,afade=t=out:st={fade_out_start:.3f}:d=0.03"

    if draft:
        preset, crf = "ultrafast", "28"
    elif preview:
        preset, crf = "medium", "22"
    else:
        preset, crf = "fast", "20"

    cmd = [
        "ffmpeg", "-nostdin", "-y",
        "-ss", f"{seg_start:.3f}",
        "-i", str(source),
        "-t", f"{duration:.3f}",
        "-vf", vf,
        "-af", af,
        "-c:v", "libx264", "-preset", preset, "-crf", crf,
        "-pix_fmt", "yuv420p", "-r", "24",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def extract_all_segments(
    edl: dict,
    edit_dir: Path,
    preview: bool,
    draft: bool = False,
    work_dir: Path | None = None,
) -> list[Path]:
    """Extract every EDL range into the selected work directory.

    Source paths remain relative to ``edit_dir`` (the EDL parent). When
    ``work_dir`` is omitted, intermediates retain their historical locations
    under ``edit_dir``.

    Returns the ordered list of segment paths.

    If the EDL `grade` is "auto", analyze each segment range with
    `auto_grade_for_clip` and apply a per-segment subtle correction.
    Otherwise, apply the same preset/raw filter to every segment.
    """
    resolved = resolve_grade_filter(edl.get("grade"))
    is_auto = resolved == "__AUTO__"
    canvas = parse_canvas(edl.get("canvas"))
    products_dir = work_dir if work_dir is not None else edit_dir
    clips_dir = products_dir / (
        "clips_draft" if draft else ("clips_preview" if preview else "clips_graded")
    )
    clips_dir.mkdir(parents=True, exist_ok=True)

    ranges = edl["ranges"]
    sources = edl["sources"]
    src_dims: dict[str, tuple[int, int]] = {}

    seg_paths: list[Path] = []
    print(f"extracting {len(ranges)} segment(s) → {clips_dir.name}/")
    if canvas:
        print(f"  (target canvas: {canvas[0]}x{canvas[1]})")
    if is_auto:
        print("  (auto-grade per segment: analyzing each range)")
    for i, r in enumerate(ranges):
        src_name = r["source"]
        src_path = resolve_path(sources[src_name], edit_dir)
        start = float(r["start"])
        end = float(r["end"])
        duration = end - start
        out_path = clips_dir / f"seg_{i:02d}_{src_name}.mp4"

        crop_filter = ""
        if canvas:
            if src_name not in src_dims:
                src_dims[src_name] = displayed_dimensions(src_path)
            src_w, src_h = src_dims[src_name]
            if r.get("crop"):
                crop_filter = validate_crop(r["crop"], src_w, src_h, i)
            else:
                crop_filter = centered_crop(src_w, src_h, canvas)
        elif r.get("crop"):
            raise ValueError(
                f"range {i}: 'crop' requires an EDL-level 'canvas' to scale into"
            )

        if is_auto:
            seg_filter, _stats = auto_grade_for_clip(src_path, start=start, duration=duration, verbose=False)
        else:
            seg_filter = resolved

        note = r.get("beat") or r.get("note") or ""
        print(f"  [{i:02d}] {src_name}  {start:7.2f}-{end:7.2f}  ({duration:5.2f}s)  {note}")
        if crop_filter:
            print(f"        crop: {crop_filter}")
        if is_auto:
            print(f"        grade: {seg_filter or '(none)'}")
        extract_segment(
            src_path, start, duration, seg_filter, out_path,
            preview=preview, draft=draft,
            crop_filter=crop_filter, canvas=canvas,
        )
        seg_paths.append(out_path)

    return seg_paths


# -------- Lossless concat ----------------------------------------------------


def parse_transition(value: Any) -> tuple[str, float] | None:
    """Parse an EDL `transition` spec into (xfade_name, duration_seconds).

    Accepts {"type": "dissolve"|"fade"|<any xfade name>, "duration": 0.35}.
    The default ``mode: overlap`` shortens output by one duration per join.
    ``mode: bridge`` is handled by the caller and inserts the transition after
    a complete first moment instead.
    """
    if not value:
        return None
    if not isinstance(value, dict):
        raise ValueError('EDL "transition" must be an object')
    kind = str(value.get("type", "dissolve")).strip()
    dur = float(value.get("duration", 0.35))
    if dur <= 0:
        raise ValueError("transition duration must be > 0")
    # "dissolve" is our friendly alias for xfade's crossfade.
    xfade_name = "fade" if kind in {"dissolve", "crossfade", "fade"} else kind
    return xfade_name, dur


def concat_with_transitions(
    segment_paths: list[Path],
    out_path: Path,
    transition: tuple[str, float],
    preview: bool = False,
) -> None:
    """Join segments with a crossfade (video xfade + audio acrossfade).

    Unlike the lossless path this re-encodes, which a blend inherently
    requires. Offsets are cumulative: each transition pulls every later
    segment earlier by `duration`, so offset_k = sum(dur[0..k]) - (k+1)*dur.
    """
    xfade_name, dur = transition
    durations = [video_duration(p) for p in segment_paths]
    if any(d <= dur + 0.05 for d in durations):
        raise ValueError(
            f"a segment is too short for a {dur:.2f}s transition: {durations}"
        )

    inputs: list[str] = []
    for p in segment_paths:
        inputs += ["-i", str(p)]

    parts: list[str] = []
    v_prev, a_prev = "[0:v]", "[0:a]"
    running = durations[0]
    for i in range(1, len(segment_paths)):
        offset = running - dur
        v_out = f"[v{i}]" if i < len(segment_paths) - 1 else "[vout]"
        a_out = f"[a{i}]" if i < len(segment_paths) - 1 else "[aout]"
        parts.append(
            f"{v_prev}[{i}:v]xfade=transition={xfade_name}:"
            f"duration={dur:.3f}:offset={offset:.3f}{v_out}"
        )
        parts.append(
            f"{a_prev}[{i}:a]acrossfade=d={dur:.3f}:c1=tri:c2=tri{a_out}"
        )
        v_prev, a_prev = v_out, a_out
        running += durations[i] - dur

    crf = "22" if preview else "20"
    cmd = [
        "ffmpeg", "-nostdin", "-y",
        *inputs,
        "-filter_complex", ";".join(parts),
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "medium", "-crf", crf,
        "-pix_fmt", "yuv420p", "-r", "24",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        str(out_path),
    ]
    print(f"concat with {xfade_name} {dur:.2f}s → {out_path.name}")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def concat_with_bridge_transition(
    segment_paths: list[Path],
    out_path: Path,
    transition: tuple[str, float],
    preview: bool = False,
) -> None:
    """Insert a transition between two complete moments.

    Normal xfade overlaps the final ``duration`` seconds of the first moment,
    which is appropriate for montage edits but can obscure the last words of a
    spoken cold open. Bridge mode preserves both moments in full and inserts a
    silent transition made from their boundary frames between them.
    """
    if len(segment_paths) != 2:
        raise ValueError("bridge transitions currently require exactly two groups")

    xfade_name, dur = transition
    durations = [video_duration(p) for p in segment_paths]
    if any(d <= 0.15 for d in durations):
        raise ValueError(f"a segment is too short for a bridge transition: {durations}")

    handle = min(0.05, durations[0] / 2, durations[1] / 2)
    fade = min(0.08, durations[0] / 3, durations[1] / 3)
    first_fade_start = max(0.0, durations[0] - fade)
    parts = [
        "[0:v]split=2[v0main][v0tail]",
        "[1:v]split=2[v1main][v1head]",
        "[v0main]setpts=PTS-STARTPTS[v0]",
        "[v1main]setpts=PTS-STARTPTS[v1]",
        (
            f"[v0tail]trim=start={max(0.0, durations[0] - handle):.6f}:"
            f"end={durations[0]:.6f},setpts=PTS-STARTPTS,"
            f"tpad=stop_mode=clone:stop_duration={dur:.3f},fps=24,format=yuv420p[vt0]"
        ),
        (
            f"[v1head]trim=start=0:end={handle:.6f},setpts=PTS-STARTPTS,"
            f"tpad=stop_mode=clone:stop_duration={dur:.3f},fps=24,format=yuv420p[vt1]"
        ),
        (
            f"[vt0][vt1]xfade=transition={xfade_name}:duration={dur:.3f}:offset=0,"
            f"trim=duration={dur:.3f},setpts=PTS-STARTPTS[vbridge]"
        ),
        "[v0][vbridge][v1]concat=n=3:v=1:a=0[vout]",
        (
            "[0:a]aformat=sample_rates=48000:channel_layouts=stereo,"
            f"afade=t=out:st={first_fade_start:.6f}:d={fade:.3f},"
            "asetpts=PTS-STARTPTS[a0]"
        ),
        f"anullsrc=r=48000:cl=stereo:d={dur:.3f}[asil]",
        (
            "[1:a]aformat=sample_rates=48000:channel_layouts=stereo,"
            f"afade=t=in:st=0:d={fade:.3f},asetpts=PTS-STARTPTS[a1]"
        ),
        "[a0][asil][a1]concat=n=3:v=0:a=1[aout]",
    ]

    crf = "22" if preview else "20"
    cmd = [
        "ffmpeg", "-nostdin", "-y",
        "-i", str(segment_paths[0]), "-i", str(segment_paths[1]),
        "-filter_complex", ";".join(parts),
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "medium", "-crf", crf,
        "-pix_fmt", "yuv420p", "-r", "24",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        str(out_path),
    ]
    print(f"concat with inserted {xfade_name} {dur:.2f}s → {out_path.name}")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def concat_segments(segment_paths: list[Path], out_path: Path, edit_dir: Path) -> None:
    """Lossless concat via the concat demuxer. No re-encode."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    concat_list = edit_dir / "_concat.txt"
    concat_list.write_text("".join(f"file '{p.resolve()}'\n" for p in segment_paths))

    cmd = [
        "ffmpeg", "-nostdin", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(concat_list),
        "-c", "copy",
        "-movflags", "+faststart",
        str(out_path),
    ]
    print(f"concat → {out_path.name}")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    concat_list.unlink(missing_ok=True)


# -------- Master SRT (Rule 5) ------------------------------------------------


PUNCT_BREAK = set(".,!?;:")

# Words that read badly as the *last* word of a caption cue ("PAYING A").
# When a cue would close on one of these, the word is deferred to the next cue.
NO_TAIL_WORDS = {
    "a", "an", "the", "of", "to", "for", "and", "or", "but", "nor",
    "with", "at", "by", "from", "in", "on", "per", "than", "vs",
}

# Verbatim ASR keeps hesitations; captions usually shouldn't. Standalone
# fillers and cut-off stutter fragments ("I-", "hi-") can be stripped from
# cues with --subtitle-strip-fillers (audio is untouched).
FILLER_TOKENS = {"uh", "um", "ah", "er", "erm"}


def is_filler_word(text: str) -> bool:
    t = text.strip().lower().strip("".join(PUNCT_BREAK) + '"')
    if t in FILLER_TOKENS:
        return True
    # Cut-off stutter fragments ("I-", "hi-") — alphabetic only, so numeric
    # run-ons like "35%-" are never treated as fillers.
    return 1 < len(t) <= 4 and t.endswith("-") and t[:-1].isalpha()


def _word_text(w: dict) -> str:
    return re.sub(r"\s+", " ", (w.get("text") or "")).strip()


def _ends_in_punct(text: str) -> bool:
    return bool(text) and text[-1] in PUNCT_BREAK


def _is_number_word(text: str) -> bool:
    stripped = text.rstrip("".join(PUNCT_BREAK))
    return bool(re.fullmatch(r"[\d][\d,.]*", stripped))


def chunk_caption_words(words: list[dict], max_words: int = 2) -> list[list[dict]]:
    """Group transcript words into caption cues that read naturally.

    Base rule: up to max_words per cue, always break after punctuation.
    Reading-order fixes on top:
      - a cue never ends on a NO_TAIL_WORDS word ("PAYING A"): the cue keeps
        extending through function words (up to max_words + 2) so grammatical
        units stay together ("SORT OF A MINDSET", "OUT OF JAIL")
      - break *before* a number that starts a quantity phrase, so the number
        stays with its unit ("SHOPKEEPER 100 / RUPEES" → "A SHOPKEEPER" /
        "100 RUPEES"); a number that *ends* a phrase ("rupees 100.") is left
        attached to the word it follows
    """
    hard_cap = max_words + 2
    chunks: list[list[dict]] = []
    current: list[dict] = []
    for idx, w in enumerate(words):
        text = _word_text(w)
        if not text:
            continue

        # Break before a phrase-starting number: current cue closes first.
        if current and _is_number_word(text) and not _ends_in_punct(text):
            nxt = next(
                (_word_text(n) for n in words[idx + 1:] if _word_text(n)), ""
            )
            prev_text = _word_text(current[-1])
            if nxt and not _ends_in_punct(prev_text):
                chunks.append(current)
                current = []

        current.append(w)

        if _ends_in_punct(text):
            chunks.append(current)
            current = []
        elif len(current) >= max_words:
            # Extend through a function-word tail so the cue closes on a
            # content word — but never past the hard cap.
            if text.lower() in NO_TAIL_WORDS and len(current) < hard_cap:
                continue
            chunks.append(current)
            current = []
    if current:
        chunks.append(current)
    return chunks


def _srt_timestamp(seconds: float) -> str:
    total_ms = int(round(seconds * 1000))
    h, rem = divmod(total_ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _words_in_range(transcript: dict, t_start: float, t_end: float) -> list[dict]:
    out: list[dict] = []
    for w in transcript.get("words", []):
        if w.get("type") != "word":
            continue
        ws = w.get("start")
        we = w.get("end")
        if ws is None or we is None:
            continue
        if we <= t_start or ws >= t_end:
            continue
        out.append(w)
    return out


def _transition_after(edl: dict, current: dict, amount: float) -> float:
    """Transition time after ``current``, or 0 for ranges within one group."""
    if amount <= 0:
        return 0.0
    ranges = edl["ranges"]
    try:
        i = ranges.index(current)
    except ValueError:
        return amount
    if i + 1 >= len(ranges):
        return 0.0
    g_now = ranges[i].get("group", i)
    g_next = ranges[i + 1].get("group", i + 1)
    return amount if g_now != g_next else 0.0


def _overlap_after(edl: dict, current: dict, overlap: float) -> float:
    """Crossfade overlap following ``current`` (zero inside a group)."""
    return _transition_after(edl, current, overlap)


def _gap_after(edl: dict, current: dict, gap: float) -> float:
    """Inserted bridge duration following ``current`` (zero inside a group)."""
    return _transition_after(edl, current, gap)


def build_master_srt(
    edl: dict,
    edit_dir: Path,
    out_path: Path,
    text_case: str = "upper",
    max_words: int = 2,
    strip_fillers: bool = False,
    transition_overlap: float = 0.0,
    transition_gap: float = 0.0,
) -> None:
    """Build an output-timeline SRT from per-source transcripts.

    - up-to-max_words chunks via chunk_caption_words (punctuation breaks,
      no function-word tails, numbers kept with their units)
    - UPPERCASE text by default; natural preserves the Scribe transcript case
    - Output times computed as word.start - segment_start + segment_offset
    """
    transcripts_dir = edit_dir / "transcripts"
    sources = edl["sources"]

    entries: list[tuple[float, float, str]] = []
    seg_offset = 0.0

    for r in edl["ranges"]:
        src_name = r["source"]
        seg_start = float(r["start"])
        seg_end = float(r["end"])
        seg_duration = seg_end - seg_start

        tr_path = transcripts_dir / f"{src_name}.json"
        if not tr_path.exists():
            print(f"  no transcript for {src_name}, skipping captions for this segment")
            seg_offset += (
                seg_duration
                - _overlap_after(edl, r, transition_overlap)
                + _gap_after(edl, r, transition_gap)
            )
            continue

        transcript = json.loads(tr_path.read_text())
        words_in_seg = _words_in_range(transcript, seg_start, seg_end)
        if strip_fillers:
            words_in_seg = [
                w for w in words_in_seg if not is_filler_word(_word_text(w))
            ]
        # Caption-only exclusions ("caption_skip": [[t0, t1], ...] in source
        # seconds) — e.g. sub-second cross-talk that overlaps the primary
        # speaker and can't be cut from the audio.
        skip_windows = edl.get("caption_skip") or []
        if skip_windows:
            def _skipped(w: dict) -> bool:
                mid = (float(w.get("start", 0)) + float(w.get("end", 0))) / 2
                return any(a <= mid <= b for a, b in skip_windows)
            words_in_seg = [w for w in words_in_seg if not _skipped(w)]

        chunks = chunk_caption_words(words_in_seg, max_words=max_words)

        for chunk in chunks:
            local_start = max(seg_start, chunk[0].get("start", seg_start))
            local_end = min(seg_end, chunk[-1].get("end", seg_end))
            out_start = max(0.0, local_start - seg_start) + seg_offset
            out_end = max(0.0, local_end - seg_start) + seg_offset
            if out_end <= out_start:
                out_end = out_start + 0.4
            text = " ".join((w.get("text") or "").strip() for w in chunk)
            text = re.sub(r"\s+", " ", text).strip()
            # Strip pause punctuation that looks awkward on short chunks.
            text = text.rstrip(",;:")
            if text_case == "upper":
                text = text.upper()
            elif text_case != "natural":
                raise ValueError(f"unsupported subtitle case: {text_case}")
            entries.append((out_start, out_end, text))

        seg_offset += (
            seg_duration
            - _overlap_after(edl, r, transition_overlap)
            + _gap_after(edl, r, transition_gap)
        )

    # Sort and write as SRT
    entries.sort(key=lambda e: e[0])
    lines: list[str] = []
    for i, (a, b, t) in enumerate(entries, start=1):
        lines.append(str(i))
        lines.append(f"{_srt_timestamp(a)} --> {_srt_timestamp(b)}")
        lines.append(t)
        lines.append("")
    out_path.write_text("\n".join(lines))
    print(f"master SRT → {out_path.name} ({len(entries)} cues)")


# -------- Loudness normalization (social-ready audio) -----------------------


# Social-media standard: -14 LUFS integrated, -1 dBTP peak, LRA 11 LU.
# Matches YouTube / Instagram / TikTok / X / LinkedIn normalization targets.
LOUDNORM_I = -14.0
LOUDNORM_TP = -1.0
LOUDNORM_LRA = 11.0


def measure_loudness(video_path: Path) -> dict[str, str] | None:
    """Run ffmpeg loudnorm first pass and parse the JSON measurement.

    Returns a dict with measured_i, measured_tp, measured_lra, measured_thresh,
    target_offset, or None if measurement failed.
    """
    filter_str = (
        f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}:print_format=json"
    )
    cmd = [
        "ffmpeg", "-nostdin", "-y", "-hide_banner", "-nostats",
        "-i", str(video_path),
        "-af", filter_str,
        "-vn", "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    # loudnorm prints the JSON to stderr at the end of the run
    stderr = proc.stderr

    # Find the JSON block — loudnorm output contains a `{ ... }` block
    start = stderr.rfind("{")
    end = stderr.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(stderr[start : end + 1])
    except json.JSONDecodeError:
        return None
    needed = {"input_i", "input_tp", "input_lra", "input_thresh", "target_offset"}
    if not needed.issubset(data.keys()):
        return None
    return data


def apply_loudnorm_two_pass(
    input_path: Path,
    output_path: Path,
    preview: bool = False,
) -> bool:
    """Run two-pass loudnorm on input_path, write normalized copy to output_path.

    Returns True on success, False if measurement failed (caller should fall
    back to copying the input unchanged).

    In preview mode, skips the measurement pass and uses a one-pass approximation
    for speed. Final mode always does the proper two-pass.
    """
    if preview:
        # One-pass approximation — faster, slightly less accurate.
        filter_str = f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}"
        cmd = [
            "ffmpeg", "-nostdin", "-y", "-hide_banner", "-nostats",
            "-i", str(input_path),
            "-c:v", "copy",
            "-af", filter_str,
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-movflags", "+faststart",
            str(output_path),
        ]
        print(f"  loudnorm (1-pass preview) → {output_path.name}")
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        return True

    # Full two-pass
    print(f"  loudnorm pass 1: measuring {input_path.name}")
    measurement = measure_loudness(input_path)
    if measurement is None:
        print("  loudnorm measurement failed — falling back to 1-pass")
        return apply_loudnorm_two_pass(input_path, output_path, preview=True)

    print(f"    measured: I={measurement['input_i']} LUFS  "
          f"TP={measurement['input_tp']}  LRA={measurement['input_lra']}")

    filter_str = (
        f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}"
        f":measured_I={measurement['input_i']}"
        f":measured_TP={measurement['input_tp']}"
        f":measured_LRA={measurement['input_lra']}"
        f":measured_thresh={measurement['input_thresh']}"
        f":offset={measurement['target_offset']}"
        f":linear=true"
    )
    cmd = [
        "ffmpeg", "-nostdin", "-y", "-hide_banner", "-nostats",
        "-i", str(input_path),
        "-c:v", "copy",
        "-af", filter_str,
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        str(output_path),
    ]
    print(f"  loudnorm pass 2: normalizing → {output_path.name}")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return True


# -------- Final compositing (Rule 1 + Rule 4) -------------------------------


STILL_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


def probe_video_codec(video: Path) -> str:
    """Codec name of a video's first video stream (for decoder selection)."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name",
         "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def video_duration(video: Path) -> float:
    """Container duration of a rendered video, in seconds."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def build_final_composite(
    base_path: Path,
    overlays: list[dict],
    subtitles_path: Path | None,
    out_path: Path,
    edit_dir: Path,
    subtitle_force_style: str | None = None,
    fonts_dir: Path | None = None,
) -> None:
    """Final pass: base → overlays (PTS-shifted) → subtitles LAST → out.

    If there are no overlays and no subtitles, just copy base to out.
    """
    has_overlays = bool(overlays)
    has_subs = subtitles_path is not None and subtitles_path.exists()

    if not has_overlays and not has_subs:
        # Nothing to do — just rename/copy base to final name
        run(
            ["ffmpeg", "-nostdin", "-y", "-i", str(base_path), "-c", "copy", str(out_path)],
            quiet=True,
        )
        return

    base_duration = video_duration(base_path)
    inputs: list[str] = ["-i", str(base_path)]
    for ov in overlays:
        ov_path = resolve_path(ov["file"], edit_dir)
        if ov_path.suffix.lower() == ".webm":
            # VP8/VP9 alpha lives in a side data plane that ffmpeg's default
            # decoders drop — the overlay would then composite as opaque
            # black over the base. libvpx/libvpx-vp9 decode it properly.
            codec = probe_video_codec(ov_path)
            decoder = "libvpx-vp9" if codec == "vp9" else "libvpx"
            inputs += ["-c:v", decoder, "-i", str(ov_path)]
        elif ov_path.suffix.lower() in STILL_IMAGE_EXTS:
            # Loop a still (e.g. a title card PNG) so it covers its overlay
            # window; cut the looped stream to exactly the base duration so
            # it can neither extend the composite past the base video nor
            # (via repeatlast) starve a window that outruns it.
            inputs += ["-loop", "1", "-framerate", "24",
                       "-t", f"{base_duration:.3f}", "-i", str(ov_path)]
        else:
            inputs += ["-i", str(ov_path)]

    filter_parts: list[str] = []
    # PTS-shift every overlay so its frame 0 lands at start_in_output
    for idx, ov in enumerate(overlays, start=1):
        t = float(ov["start_in_output"])
        filter_parts.append(f"[{idx}:v]setpts=PTS-STARTPTS+{t}/TB[a{idx}]")

    # Chain overlays on top of base
    current = "[0:v]"
    for idx, ov in enumerate(overlays, start=1):
        t = float(ov["start_in_output"])
        dur = float(ov["duration"])
        end = t + dur
        next_label = f"[v{idx}]"
        filter_parts.append(
            f"{current}[a{idx}]overlay=enable='between(t,{t:.3f},{end:.3f})'{next_label}"
        )
        current = next_label

    # Subtitles LAST — Rule 1
    if has_subs:
        subs_abs = str(subtitles_path.resolve()).replace(":", r"\:").replace("'", r"\'")
        resolved_style = subtitle_force_style or build_subtitle_force_style(base_path)
        style = resolved_style.replace("'", r"\'")
        fonts_arg = ""
        if fonts_dir is not None:
            fonts_abs = str(fonts_dir.resolve()).replace(":", r"\:").replace("'", r"\'")
            fonts_arg = f":fontsdir='{fonts_abs}'"
        filter_parts.append(
            f"{current}subtitles='{subs_abs}':force_style='{style}'{fonts_arg}[outv]"
        )
        out_label = "[outv]"
    else:
        # Rename the last overlay output to [outv] for consistency
        if has_overlays:
            filter_parts.append(f"{current}null[outv]")
            out_label = "[outv]"
        else:
            out_label = "[0:v]"

    filter_complex = ";".join(filter_parts)

    cmd = [
        "ffmpeg", "-nostdin", "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", out_label,
        "-map", "0:a",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(out_path),
    ]
    print(f"compositing → {out_path.name}")
    print(f"  overlays: {len(overlays)}, subtitles: {'yes' if has_subs else 'no'}")
    if has_subs:
        print(f"  subtitle style: {subtitle_force_style or build_subtitle_force_style(base_path)}")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


# -------- Main ---------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description="Render a video from an EDL")
    ap.add_argument("edl", type=Path, help="Path to edl.json")
    ap.add_argument("-o", "--output", type=Path, required=True, help="Output video path")
    ap.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help=(
            "Directory for render intermediates and built master.srt. "
            "Defaults to the EDL parent."
        ),
    )
    ap.add_argument(
        "--preview",
        action="store_true",
        help="Preview mode: 1080p, medium, CRF 22 — evaluable for QC, faster than final.",
    )
    ap.add_argument(
        "--draft",
        action="store_true",
        help="Draft mode: 720p, ultrafast, CRF 28 — cut-point verification only.",
    )
    ap.add_argument(
        "--build-subtitles",
        action="store_true",
        help="Build master.srt from transcripts + EDL offsets before compositing",
    )
    ap.add_argument(
        "--subtitle-case",
        choices=("upper", "natural"),
        default="upper",
        help="Caption case when building subtitles (default: upper).",
    )
    ap.add_argument(
        "--subtitle-max-words",
        type=int,
        default=2,
        help="Max words per caption cue when building subtitles (default: 2).",
    )
    ap.add_argument(
        "--subtitle-strip-fillers",
        action="store_true",
        help="Drop hesitation fillers (uh/um/ah) and stutter fragments from "
        "built captions. Audio is unchanged.",
    )
    ap.add_argument(
        "--no-subtitles",
        action="store_true",
        help="Skip subtitles even if the EDL references one",
    )
    ap.add_argument(
        "--subtitle-font-size",
        type=float,
        default=None,
        help="ASS font size override. Portrait auto-default: 10; landscape: 18.",
    )
    ap.add_argument(
        "--subtitle-margin-v",
        type=int,
        default=None,
        help="ASS vertical margin override. Higher moves bottom-aligned text upward.",
    )
    ap.add_argument(
        "--subtitle-alignment",
        type=int,
        choices=range(1, 10),
        default=2,
        help="ASS numpad alignment (default: 2, bottom-center).",
    )
    ap.add_argument(
        "--subtitle-outline",
        type=float,
        default=None,
        help="ASS outline thickness override. Portrait auto-default: 1.5.",
    )
    ap.add_argument(
        "--subtitle-force-style",
        type=str,
        default=None,
        help="Raw ASS force_style override; replaces all generated subtitle styling.",
    )
    ap.add_argument(
        "--subtitle-font-name",
        type=str,
        default=None,
        help="Caption font family name (default: Helvetica).",
    )
    ap.add_argument(
        "--subtitle-fonts-dir",
        type=Path,
        default=None,
        help="Extra directory of font files for libass (subtitles fontsdir).",
    )
    ap.add_argument(
        "--subtitle-no-bold",
        action="store_true",
        help="Do not force Bold=1 — use for single-weight display fonts "
        "(Anton, Archivo Black, …) where faux-bold distorts glyphs.",
    )
    ap.add_argument(
        "--no-loudnorm",
        action="store_true",
        help="Skip audio loudness normalization. Default is on (-14 LUFS, -1 dBTP, LRA 11).",
    )
    args = ap.parse_args()

    if args.subtitle_font_size is not None and args.subtitle_font_size <= 0:
        ap.error("--subtitle-font-size must be > 0")
    if args.subtitle_max_words < 1:
        ap.error("--subtitle-max-words must be >= 1")
    if args.subtitle_margin_v is not None and args.subtitle_margin_v < 0:
        ap.error("--subtitle-margin-v must be >= 0")
    if args.subtitle_outline is not None and args.subtitle_outline < 0:
        ap.error("--subtitle-outline must be >= 0")

    edl_path = args.edl.resolve()
    if not edl_path.exists():
        sys.exit(f"edl not found: {edl_path}")

    edl = json.loads(edl_path.read_text())
    edit_dir = edl_path.parent
    work_dir = args.work_dir.resolve() if args.work_dir is not None else edit_dir
    work_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.output.resolve()

    # 1. Extract per-segment (auto-grade per range if EDL grade is "auto")
    segment_paths = extract_all_segments(
        edl,
        edit_dir,
        preview=args.preview,
        draft=args.draft,
        work_dir=work_dir,
    )

    # 2. Concat → base
    if args.draft:
        base_name = "base_draft.mp4"
    elif args.preview:
        base_name = "base_preview.mp4"
    else:
        base_name = "base.mp4"
    base_path = work_dir / base_name
    transition_spec = edl.get("transition") or {}
    transition = parse_transition(transition_spec)
    transition_mode = (
        str(transition_spec.get("mode", "overlap")).strip().lower()
        if isinstance(transition_spec, dict)
        else "overlap"
    )
    if transition_mode not in {"overlap", "bridge"}:
        raise ValueError(f"unsupported transition mode: {transition_mode}")
    groups = [r.get("group", i) for i, r in enumerate(edl["ranges"])]
    n_groups = len(dict.fromkeys(groups))
    if transition and n_groups > 1:
        # Ranges sharing a group are one continuous moment: hard-cut them
        # together first (silence trims must stay invisible), then crossfade
        # only the joins between moments.
        group_files: list[Path] = []
        for gi, gid in enumerate(dict.fromkeys(groups)):
            members = [p for p, g in zip(segment_paths, groups) if g == gid]
            if len(members) == 1:
                group_files.append(members[0])
            else:
                gpath = work_dir / f"_group_{gi:02d}.mp4"
                concat_segments(members, gpath, work_dir)
                group_files.append(gpath)
        if transition_mode == "bridge":
            concat_with_bridge_transition(
                group_files, base_path, transition, preview=args.preview
            )
        else:
            concat_with_transitions(
                group_files, base_path, transition, preview=args.preview
            )
    elif transition and len(segment_paths) > 1:
        concat_with_transitions(
            segment_paths, base_path, transition, preview=args.preview
        )
    else:
        concat_segments(segment_paths, base_path, work_dir)

    # 3. Subtitles: build if requested, resolve final path
    subs_path: Path | None = None
    if not args.no_subtitles:
        if args.build_subtitles:
            subs_path = work_dir / "master.srt"
            build_master_srt(
                edl,
                edit_dir,
                subs_path,
                text_case=args.subtitle_case,
                max_words=args.subtitle_max_words,
                strip_fillers=args.subtitle_strip_fillers,
                transition_overlap=(
                    transition[1]
                    if transition and transition_mode == "overlap"
                    else 0.0
                ),
                transition_gap=(
                    transition[1]
                    if transition and transition_mode == "bridge"
                    else 0.0
                ),
            )
        elif edl.get("subtitles"):
            subs_path = resolve_path(edl["subtitles"], edit_dir)
            if not subs_path.exists():
                print(f"warning: subtitles path in EDL does not exist: {subs_path}")
                subs_path = None

    subtitle_force_style = build_subtitle_force_style(
        base_path,
        font_size=args.subtitle_font_size,
        margin_v=args.subtitle_margin_v,
        alignment=args.subtitle_alignment,
        outline=args.subtitle_outline,
        raw_style=args.subtitle_force_style,
        font_name=args.subtitle_font_name,
        bold=not args.subtitle_no_bold,
    )
    if args.subtitle_fonts_dir is not None and not args.subtitle_fonts_dir.is_dir():
        sys.exit(f"--subtitle-fonts-dir is not a directory: {args.subtitle_fonts_dir}")

    # 4. Composite (overlays + subtitles LAST) → intermediate (pre-loudnorm) path
    overlays = edl.get("overlays") or []
    if args.no_loudnorm:
        # Composite directly to final output
        build_final_composite(
            base_path, overlays, subs_path, out_path, edit_dir, subtitle_force_style,
            fonts_dir=args.subtitle_fonts_dir,
        )
    else:
        # Composite to a temp file, then run loudnorm → final output
        tmp_composite = out_path.with_suffix(".prenorm.mp4")
        build_final_composite(
            base_path, overlays, subs_path, tmp_composite, edit_dir, subtitle_force_style,
            fonts_dir=args.subtitle_fonts_dir,
        )
        print("loudness normalization → social-ready (-14 LUFS / -1 dBTP / LRA 11)")
        apply_loudnorm_two_pass(tmp_composite, out_path, preview=args.draft)
        tmp_composite.unlink(missing_ok=True)

    size_mb = out_path.stat().st_size / (1024 * 1024)
    print(f"\ndone: {out_path} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
