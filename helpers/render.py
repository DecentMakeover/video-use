"""Render a video from an EDL.

Implements the HEURISTICS render pipeline in the correct order:

  1. Build one cumulative rational frame/sample schedule for the whole EDL
  2. Extract video-only and lossless PCM segments with exact counts and reset PTS
  3. Concatenate video and audio independently, then mux into base.mov
  4. If overlays or subtitles: single filter graph that overlays animations
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
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
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


def build_subtitle_force_style(
    video: Path,
    font_size: float | None = None,
    margin_v: int | None = None,
    alignment: int = 2,
    outline: float | None = None,
    raw_style: str | None = None,
) -> str:
    """Build an ASS force_style with aspect-aware defaults.

    Portrait defaults are deliberately smaller than the historical
    bold-overlay values and start above the busiest platform UI region. Every
    result still requires full-resolution subtitle QC.
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

    return (
        f"FontName=Helvetica,FontSize={chosen_font_size:g},Bold=1,"
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


# -------- Canonical output schedule -----------------------------------------


AUDIO_RATE = 48_000
AAC_FRAME_SAMPLES = 1_024


@dataclass(frozen=True)
class SegmentSchedule:
    index: int
    start_frames: int
    end_frames: int
    start_samples: int
    end_samples: int

    @property
    def frame_count(self) -> int:
        return self.end_frames - self.start_frames

    @property
    def sample_count(self) -> int:
        return self.end_samples - self.start_samples


@dataclass(frozen=True)
class RenderSchedule:
    fps: Fraction
    sample_rate: int
    segments: tuple[SegmentSchedule, ...]

    @property
    def total_frames(self) -> int:
        return self.segments[-1].end_frames if self.segments else 0

    @property
    def total_samples(self) -> int:
        return self.segments[-1].end_samples if self.segments else 0


def _as_fraction(value: object) -> Fraction:
    """Convert JSON-style numbers without introducing binary float error."""
    if isinstance(value, Fraction):
        return value
    if isinstance(value, Decimal):
        return Fraction(value)
    return Fraction(str(value))


def _round_fraction(value: Fraction) -> int:
    """Round a non-negative rational half up, deterministically."""
    if value < 0:
        raise ValueError(f"timeline values must be non-negative (got {value})")
    return (2 * value.numerator + value.denominator) // (2 * value.denominator)


def build_render_schedule(
    ranges: list[dict], fps: str | Fraction, sample_rate: int = AUDIO_RATE
) -> RenderSchedule:
    """Allocate exact counts from cumulative boundaries, never per-range rounding."""
    rate = _as_fraction(fps)
    if rate <= 0 or sample_rate <= 0:
        raise ValueError("output frame and sample rates must be positive")

    cumulative = Fraction(0)
    previous_frames = 0
    previous_samples = 0
    result: list[SegmentSchedule] = []
    for index, item in enumerate(ranges):
        duration = _as_fraction(item["end"]) - _as_fraction(item["start"])
        if duration <= 0:
            raise ValueError(f"range {index} has non-positive duration {duration}")
        cumulative += duration
        end_frames = _round_fraction(cumulative * rate)
        end_samples = _round_fraction(cumulative * sample_rate)
        segment = SegmentSchedule(
            index=index,
            start_frames=previous_frames,
            end_frames=end_frames,
            start_samples=previous_samples,
            end_samples=end_samples,
        )
        if segment.frame_count <= 0:
            raise ValueError(
                f"range {index} is too short to allocate one frame at {rate} fps"
            )
        if segment.sample_count <= 0:
            raise ValueError(f"range {index} is too short to allocate one audio sample")
        result.append(segment)
        previous_frames = end_frames
        previous_samples = end_samples
    return RenderSchedule(rate, sample_rate, tuple(result))


def _fps_text(fps: Fraction) -> str:
    return str(fps.numerator) if fps.denominator == 1 else f"{fps.numerator}/{fps.denominator}"


# -------- Per-segment extraction (Rule 2 + Rule 3) --------------------------


def source_fps(video: Path) -> str:
    """Probed r_frame_rate of the first video stream (e.g. '30/1')."""
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=r_frame_rate",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(video),
        ],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip().splitlines()[0]


def resolve_output_fps(edl: dict, edit_dir: Path, requested: str) -> Fraction:
    """Resolve one homogeneous output rate before constructing the schedule."""
    if requested != "source":
        return _as_fraction(requested)
    selected_sources = {item["source"] for item in edl["ranges"]}
    rates = {
        _as_fraction(source_fps(resolve_path(edl["sources"][name], edit_dir)))
        for name in selected_sources
    }
    if len(rates) != 1:
        formatted = ", ".join(sorted(_fps_text(rate) for rate in rates))
        raise ValueError(
            "--fps source requires all selected sources to share one rate; "
            f"found {formatted}"
        )
    return rates.pop()


@dataclass(frozen=True)
class SegmentPaths:
    video: Path
    audio: Path


def extract_segment(
    source: Path,
    seg_start: float,
    duration: float,
    grade_filter: str,
    out_path: Path,
    preview: bool = False,
    draft: bool = False,
    fps: str = "24",
    crf: int | None = None,
    fade_in: float = 0.0,
    fade_out: float = 0.0,
    long_edge: int = 1920,
    frame_count: int | None = None,
    sample_count: int | None = None,
    audio_out_path: Path | None = None,
) -> SegmentPaths:
    """Extract exact-count H.264 video and lossless PCM audio intermediates.

    `-ss` before `-i` for fast accurate seeking. Scales the long edge to
    `long_edge` px (default 1920 → 1080p landscape; 3840 → 4K). Sources smaller
    than the target are upscaled; portrait sources scale by height to preserve
    orientation.

    `fade_in`/`fade_out` (seconds) fade the segment from/to black for
    montage-style transitions (cold opens, highlight reels). The audio fade
    widens to match the visual fade, never below the 30ms de-pop floor.

    Quality ladder:
      - final (default): 1080p libx264 fast CRF 20
      - preview:         1080p libx264 medium CRF 22 (evaluable for QC)
      - draft:           720p libx264 ultrafast CRF 28 (cut-point check only)
    """
    if fade_in < 0 or fade_out < 0:
        raise ValueError(
            f"segment fades must be >= 0 (got fade_in={fade_in:g}, fade_out={fade_out:g})"
        )
    if fade_in + fade_out > duration + 1e-6:
        raise ValueError(
            f"segment fades ({fade_in:g}s + {fade_out:g}s) exceed the {duration:.3f}s segment"
        )
    rate = _as_fraction(fps)
    if frame_count is None:
        frame_count = _round_fraction(_as_fraction(duration) * rate)
    if sample_count is None:
        sample_count = _round_fraction(_as_fraction(duration) * AUDIO_RATE)
    if frame_count <= 0 or sample_count <= 0:
        raise ValueError("segment must allocate at least one frame and one sample")
    audio_path = audio_out_path or out_path.with_suffix(".pcm.wav")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    audio_path.parent.mkdir(parents=True, exist_ok=True)

    video_duration = Fraction(frame_count, 1) / rate
    audio_duration = Fraction(sample_count, AUDIO_RATE)
    video_seconds = float(video_duration)
    audio_seconds = float(audio_duration)

    portrait = is_portrait_source(source)
    if draft:
        draft_edge = min(long_edge, 1280)
        scale = f"scale=-2:{draft_edge}" if portrait else f"scale={draft_edge}:-2"
    else:
        scale = f"scale=-2:{long_edge}" if portrait else f"scale={long_edge}:-2"

    fps_expr = _fps_text(rate)
    vf_parts: list[str] = [
        "setpts=PTS-STARTPTS",
        f"fps=fps={fps_expr}:round=near",
        "tpad=stop_mode=clone:stop=-1",
        f"trim=end_frame={frame_count}",
        f"setpts=N/({fps_expr}*TB)",
    ]
    if is_hdr_source(source):
        vf_parts.append(TONEMAP_CHAIN)
    vf_parts.append(scale)
    if grade_filter:
        vf_parts.append(grade_filter)
    if fade_in > 0:
        vf_parts.append(f"fade=t=in:st=0:d={fade_in:.3f}")
    if fade_out > 0:
        vf_parts.append(
            f"fade=t=out:st={max(0.0, video_seconds - fade_out):.6f}:d={fade_out:.6f}"
        )
    vf = ",".join(vf_parts)

    # Audio remains PCM. Exact sample padding/trimming and a second PTS reset
    # make every segment independently start at sample zero without AAC priming.
    afade_in = max(0.03, fade_in)
    afade_out = max(0.03, fade_out)
    fade_out_start = max(0.0, audio_seconds - afade_out)
    af = ",".join(
        [
            "asetpts=PTS-STARTPTS",
            f"aresample={AUDIO_RATE}:async=0:first_pts=0",
            f"apad=whole_len={sample_count}",
            f"atrim=end_sample={sample_count}",
            f"asetpts=N/{AUDIO_RATE}/TB",
            f"afade=t=in:st=0:d={afade_in:.6f}",
            f"afade=t=out:st={fade_out_start:.6f}:d={afade_out:.6f}",
        ]
    )

    if draft:
        preset, default_crf = "ultrafast", "28"
    elif preview:
        preset, default_crf = "medium", "22"
    else:
        preset, default_crf = "fast", "20"
    crf_value = str(crf) if crf is not None else default_crf

    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{seg_start:.9f}",
        "-i", str(source),
        "-map", "0:v:0", "-an",
        "-vf", vf,
        "-frames:v", str(frame_count),
        "-c:v", "libx264", "-preset", preset, "-crf", crf_value,
        "-pix_fmt", "yuv420p", "-fps_mode", "passthrough",
        "-movflags", "+faststart",
        str(out_path),
        "-map", "0:a:0", "-vn",
        "-af", af,
        "-c:a", "pcm_s24le", "-ar", str(AUDIO_RATE),
        str(audio_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return SegmentPaths(out_path, audio_path)


def extract_all_segments(
    edl: dict,
    edit_dir: Path,
    preview: bool,
    draft: bool = False,
    fps: str | Fraction = "24",
    crf: int | None = None,
    long_edge: int = 1920,
    schedule: RenderSchedule | None = None,
) -> list[SegmentPaths]:
    """Extract every EDL range to separate exact-count video and PCM files.
    Returns the ordered video/audio path pairs.

    If the EDL `grade` is "auto", analyze each segment range with
    `auto_grade_for_clip` and apply a per-segment subtle correction.
    Otherwise, apply the same preset/raw filter to every segment.
    """
    resolved = resolve_grade_filter(edl.get("grade"))
    is_auto = resolved == "__AUTO__"
    clips_dir = edit_dir / (
        "clips_draft" if draft else ("clips_preview" if preview else "clips_graded")
    )
    clips_dir.mkdir(parents=True, exist_ok=True)

    ranges = edl["ranges"]
    sources = edl["sources"]

    if schedule is None:
        if fps == "source":
            resolved_rate = resolve_output_fps(edl, edit_dir, "source")
        else:
            resolved_rate = _as_fraction(fps)
        schedule = build_render_schedule(ranges, resolved_rate)
    if len(schedule.segments) != len(ranges):
        raise ValueError("render schedule/range count mismatch")

    seg_paths: list[SegmentPaths] = []
    print(
        f"extracting {len(ranges)} segment(s) → {clips_dir.name}/ "
        f"({schedule.total_frames} frames, {schedule.total_samples} samples)"
    )
    if is_auto:
        print("  (auto-grade per segment: analyzing each range)")
    for i, (r, allocation) in enumerate(zip(ranges, schedule.segments)):
        src_name = r["source"]
        src_path = resolve_path(sources[src_name], edit_dir)
        start = float(r["start"])
        end = float(r["end"])
        duration = end - start
        video_path = clips_dir / f"seg_{i:04d}_{src_name}.video.mp4"
        audio_path = clips_dir / f"seg_{i:04d}_{src_name}.audio.wav"

        if is_auto:
            seg_filter, _stats = auto_grade_for_clip(src_path, start=start, duration=duration, verbose=False)
        else:
            seg_filter = resolved

        fade_in = float(r.get("fade_in") or 0.0)
        fade_out = float(r.get("fade_out") or 0.0)

        note = r.get("beat") or r.get("note") or ""
        fade_note = ""
        if fade_in > 0 or fade_out > 0:
            fade_note = f"  fade {fade_in:g}s/{fade_out:g}s"
        print(f"  [{i:02d}] {src_name}  {start:7.2f}-{end:7.2f}  ({duration:5.2f}s)  {note}{fade_note}")
        if is_auto:
            print(f"        grade: {seg_filter or '(none)'}")
        paths = extract_segment(
            src_path, start, duration, seg_filter, video_path,
            preview=preview, draft=draft, fps=_fps_text(schedule.fps), crf=crf,
            fade_in=fade_in, fade_out=fade_out, long_edge=long_edge,
            frame_count=allocation.frame_count,
            sample_count=allocation.sample_count,
            audio_out_path=audio_path,
        )
        seg_paths.append(paths)

    return seg_paths


# -------- Lossless concat ----------------------------------------------------


def _write_concat_list(path: Path, inputs: list[Path]) -> None:
    # ffconcat single-quoted paths escape an apostrophe as '\''.
    def quote(item: Path) -> str:
        escaped = str(item.resolve()).replace("'", "'\\''")
        return f"file '{escaped}'\n"

    path.write_text("".join(quote(item) for item in inputs))


def concat_segments(
    segment_paths: list[SegmentPaths], out_path: Path, edit_dir: Path
) -> None:
    """Concat video and PCM independently, then losslessly mux them.

    Keeping the tracks separate is intentional: the concat demuxer advances a
    multiplexed file by its longest stream, which can add a sub-frame gap at
    every segment when independently quantized video/audio durations differ.
    """
    if not segment_paths:
        raise ValueError("cannot concatenate an empty segment list")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    video_list = edit_dir / "_concat_video.txt"
    audio_list = edit_dir / "_concat_audio.txt"
    video_concat = edit_dir / "_video_concat.mp4"
    audio_concat = edit_dir / "_audio_concat.wav"
    _write_concat_list(video_list, [item.video for item in segment_paths])
    _write_concat_list(audio_list, [item.audio for item in segment_paths])

    commands = [
        [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(video_list),
            "-map", "0:v:0", "-c:v", "copy", "-an", "-movflags", "+faststart",
            str(video_concat),
        ],
        [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(audio_list),
            "-map", "0:a:0", "-c:a", "copy", "-vn", "-rf64", "auto",
            str(audio_concat),
        ],
        [
            "ffmpeg", "-y", "-i", str(video_concat), "-i", str(audio_concat),
            "-map", "0:v:0", "-map", "1:a:0", "-c", "copy",
            "-movflags", "+faststart", str(out_path),
        ],
    ]
    print(f"concat video + PCM → {out_path.name}")
    try:
        for command in commands:
            subprocess.run(
                command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
            )
    finally:
        for temporary in (video_list, audio_list, video_concat, audio_concat):
            temporary.unlink(missing_ok=True)


# -------- Master SRT (Rule 5) ------------------------------------------------


PUNCT_BREAK = set(".,!?;:")


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


def build_master_srt(
    edl: dict,
    edit_dir: Path,
    out_path: Path,
    text_case: str = "upper",
    schedule: RenderSchedule | None = None,
    fps: str | Fraction = "24",
) -> None:
    """Build an output-timeline SRT from per-source transcripts.

    - 2-word chunks (break on any punctuation in between)
    - UPPERCASE text by default; natural preserves the Scribe transcript case
    - Output times computed as word.start - segment_start + segment_offset
    """
    transcripts_dir = edit_dir / "transcripts"
    sources = edl["sources"]

    entries: list[tuple[float, float, str]] = []
    if schedule is None:
        schedule = build_render_schedule(edl["ranges"], fps)

    for index, r in enumerate(edl["ranges"]):
        src_name = r["source"]
        seg_start = float(r["start"])
        seg_end = float(r["end"])
        allocation = schedule.segments[index]
        seg_offset = float(Fraction(allocation.start_frames, 1) / schedule.fps)
        seg_duration = float(Fraction(allocation.frame_count, 1) / schedule.fps)

        tr_path = transcripts_dir / f"{src_name}.json"
        if not tr_path.exists():
            print(f"  no transcript for {src_name}, skipping captions for this segment")
            continue

        transcript = json.loads(tr_path.read_text())
        words_in_seg = _words_in_range(transcript, seg_start, seg_end)

        # Group into 2-word chunks, break on punctuation
        chunks: list[list[dict]] = []
        current: list[dict] = []
        for w in words_in_seg:
            text = (w.get("text") or "").strip()
            if not text:
                continue
            current.append(w)
            # Break if the current text ends in punctuation or we hit 2 words
            ends_in_punct = bool(text) and text[-1] in PUNCT_BREAK
            if len(current) >= 2 or ends_in_punct:
                chunks.append(current)
                current = []
        if current:
            chunks.append(current)

        for chunk in chunks:
            local_start = max(seg_start, chunk[0].get("start", seg_start))
            local_end = min(seg_end, chunk[-1].get("end", seg_end))
            out_start = min(seg_duration, max(0.0, local_start - seg_start)) + seg_offset
            out_end = min(seg_duration, max(0.0, local_end - seg_start)) + seg_offset
            if out_end <= out_start:
                # The word only touched the cut boundary and has no canonical
                # on-screen duration in this segment; do not leak it past the cut.
                continue
            text = " ".join((w.get("text") or "").strip() for w in chunk)
            text = re.sub(r"\s+", " ", text).strip()
            # Strip pause punctuation that looks awkward on short chunks.
            text = text.rstrip(",;:")
            if text_case == "upper":
                text = text.upper()
            elif text_case != "natural":
                raise ValueError(f"unsupported subtitle case: {text_case}")
            entries.append((out_start, out_end, text))

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
        "ffmpeg", "-y", "-hide_banner", "-nostats",
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
            "ffmpeg", "-y", "-hide_banner", "-nostats",
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
        "ffmpeg", "-y", "-hide_banner", "-nostats",
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


def build_final_composite(
    base_path: Path,
    overlays: list[dict],
    subtitles_path: Path | None,
    out_path: Path,
    edit_dir: Path,
    subtitle_force_style: str | None = None,
    fps: str | Fraction = "24",
) -> None:
    """Final pass: base → overlays (PTS-shifted) → subtitles LAST → out.

    If there are no overlays and no subtitles, just copy base to out.
    """
    has_overlays = bool(overlays)
    has_subs = subtitles_path is not None and subtitles_path.exists()

    if not has_overlays and not has_subs:
        # Nothing to do — just rename/copy base to final name
        run(["ffmpeg", "-y", "-i", str(base_path), "-c", "copy", str(out_path)], quiet=True)
        return

    rate = _as_fraction(fps)

    def snap_to_frame(seconds: object) -> float:
        frame = _round_fraction(_as_fraction(seconds) * rate)
        return float(Fraction(frame, 1) / rate)

    inputs: list[str] = ["-i", str(base_path)]
    for ov in overlays:
        ov_path = resolve_path(ov["file"], edit_dir)
        inputs += ["-i", str(ov_path)]

    filter_parts: list[str] = []
    # PTS-shift every overlay so its frame 0 lands at start_in_output
    for idx, ov in enumerate(overlays, start=1):
        t = snap_to_frame(ov["start_in_output"])
        filter_parts.append(f"[{idx}:v]setpts=PTS-STARTPTS+{t:.9f}/TB[a{idx}]")

    # Chain overlays on top of base
    current = "[0:v]"
    for idx, ov in enumerate(overlays, start=1):
        t = snap_to_frame(ov["start_in_output"])
        end = snap_to_frame(_as_fraction(ov["start_in_output"]) + _as_fraction(ov["duration"]))
        next_label = f"[v{idx}]"
        filter_parts.append(
            f"{current}[a{idx}]overlay="
            f"enable='gte(t,{t:.9f})*lt(t,{end:.9f})'{next_label}"
        )
        current = next_label

    # Subtitles LAST — Rule 1
    if has_subs:
        subs_abs = str(subtitles_path.resolve()).replace(":", r"\:").replace("'", r"\'")
        resolved_style = subtitle_force_style or build_subtitle_force_style(base_path)
        style = resolved_style.replace("'", r"\'")
        filter_parts.append(
            f"{current}subtitles='{subs_abs}':force_style='{style}'[outv]"
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
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", out_label,
        "-map", "0:a",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-fps_mode", "cfr", "-r", _fps_text(rate),
        "-movflags", "+faststart",
        str(out_path),
    ]
    print(f"compositing → {out_path.name}")
    print(f"  overlays: {len(overlays)}, subtitles: {'yes' if has_subs else 'no'}")
    if has_subs:
        print(f"  subtitle style: {subtitle_force_style or build_subtitle_force_style(base_path)}")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def encode_aac_once(input_path: Path, output_path: Path) -> None:
    """Copy final video and perform the pipeline's only AAC encode."""
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-nostats",
        "-i", str(input_path),
        "-map", "0:v:0", "-map", "0:a:0",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", str(AUDIO_RATE),
        "-movflags", "+faststart", str(output_path),
    ]
    print(f"  final AAC encode → {output_path.name}")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


# -------- Main ---------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description="Render a video from an EDL")
    ap.add_argument("edl", type=Path, help="Path to edl.json")
    ap.add_argument("-o", "--output", type=Path, required=True, help="Output video path")
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
        "--fps",
        default="24",
        help="Output frame rate: a number/fraction, or 'source' to keep each "
        "source's native rate (default: 24, the current engine contract). "
        "All EDL sources must share one rate when using 'source' — the concat "
        "step assumes homogeneous segments.",
    )
    ap.add_argument(
        "--crf",
        type=int,
        default=None,
        help="Override libx264 CRF for segment extraction (default: 20 final / "
        "22 preview / 28 draft). Lower = higher quality; 17-18 is visually "
        "lossless for delivery masters.",
    )
    ap.add_argument(
        "--long-edge",
        type=int,
        default=1920,
        help="Output long-edge resolution in px (default 1920 → 1080p; "
        "3840 → 4K, 2560 → 1440p). Sources smaller than the target are "
        "upscaled, so mixed-resolution edits deliver at one canvas.",
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
        "--no-loudnorm",
        action="store_true",
        help="Skip audio loudness normalization. Default is on (-14 LUFS, -1 dBTP, LRA 11).",
    )
    args = ap.parse_args()

    if args.subtitle_font_size is not None and args.subtitle_font_size <= 0:
        ap.error("--subtitle-font-size must be > 0")
    if args.subtitle_margin_v is not None and args.subtitle_margin_v < 0:
        ap.error("--subtitle-margin-v must be >= 0")
    if args.subtitle_outline is not None and args.subtitle_outline < 0:
        ap.error("--subtitle-outline must be >= 0")

    edl_path = args.edl.resolve()
    if not edl_path.exists():
        sys.exit(f"edl not found: {edl_path}")

    edl = json.loads(edl_path.read_text(), parse_float=Decimal)
    edit_dir = edl_path.parent
    out_path = args.output.resolve()
    output_fps = resolve_output_fps(edl, edit_dir, args.fps)
    schedule = build_render_schedule(edl["ranges"], output_fps)
    print(
        f"canonical schedule: {_fps_text(output_fps)} fps, "
        f"{schedule.total_frames} frames, {schedule.total_samples} samples"
    )

    # 1. Extract per-segment (auto-grade per range if EDL grade is "auto")
    segment_paths = extract_all_segments(
        edl, edit_dir, preview=args.preview, draft=args.draft,
        fps=output_fps, crf=args.crf, long_edge=args.long_edge, schedule=schedule,
    )

    # 2. Concat → base
    if args.draft:
        base_name = "base_draft.mov"
    elif args.preview:
        base_name = "base_preview.mov"
    else:
        base_name = "base.mov"
    base_path = edit_dir / base_name
    concat_segments(segment_paths, base_path, edit_dir)

    # 3. Subtitles: build if requested, resolve final path
    subs_path: Path | None = None
    if not args.no_subtitles:
        if args.build_subtitles:
            subs_path = edit_dir / "master.srt"
            build_master_srt(
                edl,
                edit_dir,
                subs_path,
                text_case=args.subtitle_case,
                schedule=schedule,
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
    )

    # 4. Composite with PCM retained, then perform exactly one final AAC encode.
    overlays = edl.get("overlays") or []
    tmp_composite = out_path.with_suffix(".prenorm.mov")
    build_final_composite(
        base_path, overlays, subs_path, tmp_composite, edit_dir, subtitle_force_style,
        fps=output_fps,
    )
    if args.no_loudnorm:
        encode_aac_once(tmp_composite, out_path)
    else:
        print("loudness normalization → social-ready (-14 LUFS / -1 dBTP / LRA 11)")
        apply_loudnorm_two_pass(tmp_composite, out_path, preview=args.draft)
    tmp_composite.unlink(missing_ok=True)

    size_mb = out_path.stat().st_size / (1024 * 1024)
    print(f"\ndone: {out_path} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
