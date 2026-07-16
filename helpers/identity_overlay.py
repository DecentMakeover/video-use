"""Generate a prominent, reusable portrait-video identity card.

The helper renders a full-canvas transparent PNG and an optional qtrle/ARGB
QuickTime overlay. Keeping the overlay full-canvas lets render.py composite it
at (0, 0) while the card itself stays inside a measured portrait safe zone.

Example:
    python helpers/identity_overlay.py \
      --name "Harshita" --company "Saks Global" \
      --output edit/animations/harshita_identity.mov --duration 5
"""

from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


WIDTH = 1080
HEIGHT = 1920
DEFAULT_FONT = Path("/System/Library/Fonts/Supplemental/DIN Alternate Bold.ttf")

SAFE_LEFT = 96
SAFE_TOP = 210
SAFE_RIGHT = 96
MAX_CARD_WIDTH = WIDTH - SAFE_LEFT - SAFE_RIGHT

NAME_FONT_SIZE = 64
COMPANY_FONT_SIZE = 52
MIN_NAME_FONT_SIZE = 52
MIN_COMPANY_FONT_SIZE = 40

NAME_CARD_MIN_WIDTH = 720
COMPANY_CARD_MIN_WIDTH = 660
NAME_CARD_HEIGHT = 108
COMPANY_CARD_HEIGHT = 92
CARD_GAP = 8
HORIZONTAL_PADDING = 30
ACCENT_WIDTH = 8

NAME_BACKGROUND = "#0B4DA2"
NAME_FOREGROUND = "#FFFFFF"
COMPANY_BACKGROUND = "#F8FAFC"
COMPANY_FOREGROUND = "#101828"
ACCENT_COLOR = "#E66A3C"


@dataclass(frozen=True)
class IdentityLayout:
    name_card: tuple[int, int, int, int]
    company_card: tuple[int, int, int, int]
    name_text: tuple[int, int]
    company_text: tuple[int, int]
    name_font_size: int
    company_font_size: int
    name_text_width: int
    company_text_width: int


def _font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    if not path.is_file():
        raise FileNotFoundError(f"font not found: {path}")
    return ImageFont.truetype(str(path), size)


def _text_metrics(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
) -> tuple[int, int, tuple[int, int, int, int]]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1], bbox


def _fit_font(
    draw: ImageDraw.ImageDraw,
    text: str,
    font_path: Path,
    preferred_size: int,
    minimum_size: int,
    max_text_width: int,
) -> tuple[ImageFont.FreeTypeFont, int, int, int, tuple[int, int, int, int]]:
    for size in range(preferred_size, minimum_size - 1, -1):
        font = _font(font_path, size)
        width, height, bbox = _text_metrics(draw, text, font)
        if width <= max_text_width:
            return font, size, width, height, bbox
    raise ValueError(
        f"text is too long for the identity safe zone at {minimum_size}px: {text!r}"
    )


def calculate_layout(
    name: str,
    company: str,
    draw: ImageDraw.ImageDraw,
    font_path: Path = DEFAULT_FONT,
) -> tuple[IdentityLayout, ImageFont.FreeTypeFont, ImageFont.FreeTypeFont]:
    """Measure a two-row identity card inside the 1080x1920 top safe zone."""
    name = name.strip()
    company = company.strip()
    if not name or not company:
        raise ValueError("name and company must both be non-empty")

    max_name_text_width = MAX_CARD_WIDTH - (2 * HORIZONTAL_PADDING) - ACCENT_WIDTH
    max_company_text_width = MAX_CARD_WIDTH - (2 * HORIZONTAL_PADDING)
    name_font, name_size, name_width, name_height, name_bbox = _fit_font(
        draw,
        name,
        font_path,
        NAME_FONT_SIZE,
        MIN_NAME_FONT_SIZE,
        max_name_text_width,
    )
    company_font, company_size, company_width, company_height, company_bbox = _fit_font(
        draw,
        company,
        font_path,
        COMPANY_FONT_SIZE,
        MIN_COMPANY_FONT_SIZE,
        max_company_text_width,
    )

    name_card_width = min(
        MAX_CARD_WIDTH,
        max(NAME_CARD_MIN_WIDTH, name_width + (2 * HORIZONTAL_PADDING) + ACCENT_WIDTH),
    )
    company_card_width = min(
        MAX_CARD_WIDTH,
        max(COMPANY_CARD_MIN_WIDTH, company_width + (2 * HORIZONTAL_PADDING)),
    )

    name_card = (
        SAFE_LEFT,
        SAFE_TOP,
        SAFE_LEFT + name_card_width,
        SAFE_TOP + NAME_CARD_HEIGHT,
    )
    company_top = name_card[3] + CARD_GAP
    company_card = (
        SAFE_LEFT,
        company_top,
        SAFE_LEFT + company_card_width,
        company_top + COMPANY_CARD_HEIGHT,
    )

    name_text = (
        SAFE_LEFT + HORIZONTAL_PADDING + ACCENT_WIDTH,
        SAFE_TOP + ((NAME_CARD_HEIGHT - name_height) // 2) - name_bbox[1],
    )
    company_text = (
        SAFE_LEFT + HORIZONTAL_PADDING,
        company_top + ((COMPANY_CARD_HEIGHT - company_height) // 2) - company_bbox[1],
    )

    layout = IdentityLayout(
        name_card=name_card,
        company_card=company_card,
        name_text=name_text,
        company_text=company_text,
        name_font_size=name_size,
        company_font_size=company_size,
        name_text_width=name_width,
        company_text_width=company_width,
    )
    return layout, name_font, company_font


def render_identity_png(
    name: str,
    company: str,
    output: Path,
    *,
    font_path: Path = DEFAULT_FONT,
    name_background: str = NAME_BACKGROUND,
    name_foreground: str = NAME_FOREGROUND,
    company_background: str = COMPANY_BACKGROUND,
    company_foreground: str = COMPANY_FOREGROUND,
    accent_color: str = ACCENT_COLOR,
) -> IdentityLayout:
    """Render the approved stacked identity treatment to a transparent PNG."""
    image = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    layout, name_font, company_font = calculate_layout(name, company, draw, font_path)

    for card in (layout.name_card, layout.company_card):
        shadow = (card[0], card[1] + 6, card[2], card[3] + 6)
        draw.rounded_rectangle(shadow, radius=18, fill=(0, 0, 0, 64))

    draw.rounded_rectangle(layout.name_card, radius=18, fill=name_background)
    accent = (
        layout.name_card[0],
        layout.name_card[1],
        layout.name_card[0] + ACCENT_WIDTH,
        layout.name_card[3],
    )
    draw.rounded_rectangle(accent, radius=4, fill=accent_color)
    draw.rounded_rectangle(layout.company_card, radius=16, fill=company_background)

    draw.text(layout.name_text, name.strip(), font=name_font, fill=name_foreground)
    draw.text(
        layout.company_text,
        company.strip(),
        font=company_font,
        fill=company_foreground,
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output)
    return layout


def render_identity_movie(
    png: Path,
    output: Path,
    *,
    duration: float,
    fps: int = 24,
    fade_duration: float = 0.2,
) -> None:
    """Render a transparent qtrle movie with gentle alpha fades."""
    if duration <= 0:
        raise ValueError("duration must be > 0")
    if fade_duration < 0 or fade_duration * 2 >= duration:
        raise ValueError("fade duration must be non-negative and shorter than half the clip")
    fade_out_start = duration - fade_duration
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-loop",
            "1",
            "-framerate",
            str(fps),
            "-i",
            str(png),
            "-t",
            f"{duration:.3f}",
            "-vf",
            (
                "format=rgba,"
                f"fade=t=in:st=0:d={fade_duration:.3f}:alpha=1,"
                f"fade=t=out:st={fade_out_start:.3f}:d={fade_duration:.3f}:alpha=1"
            ),
            "-c:v",
            "qtrle",
            "-pix_fmt",
            "argb",
            str(output),
        ],
        check=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a portrait identity-card overlay")
    parser.add_argument("--name", required=True)
    parser.add_argument("--company", required=True)
    parser.add_argument("--output", type=Path, required=True, help="Output .mov path")
    parser.add_argument("--png-output", type=Path, default=None)
    parser.add_argument("--duration", type=float, default=5.0)
    parser.add_argument("--fps", type=int, default=24)
    parser.add_argument("--font", type=Path, default=DEFAULT_FONT)
    parser.add_argument("--name-background", default=NAME_BACKGROUND)
    parser.add_argument("--name-foreground", default=NAME_FOREGROUND)
    parser.add_argument("--company-background", default=COMPANY_BACKGROUND)
    parser.add_argument("--company-foreground", default=COMPANY_FOREGROUND)
    parser.add_argument("--accent-color", default=ACCENT_COLOR)
    args = parser.parse_args()

    output = args.output.resolve()
    png = (
        args.png_output.resolve()
        if args.png_output
        else output.with_suffix(".png")
    )
    layout = render_identity_png(
        args.name,
        args.company,
        png,
        font_path=args.font,
        name_background=args.name_background,
        name_foreground=args.name_foreground,
        company_background=args.company_background,
        company_foreground=args.company_foreground,
        accent_color=args.accent_color,
    )
    render_identity_movie(png, output, duration=args.duration, fps=args.fps)
    print(json.dumps({"png": str(png), "movie": str(output), **asdict(layout)}, indent=2))


if __name__ == "__main__":
    main()
