from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
HELPERS = ROOT / "helpers"
sys.path.insert(0, str(HELPERS))

import identity_overlay  # noqa: E402


def _linearized(channel: int) -> float:
    value = channel / 255.0
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def _contrast_ratio(foreground: str, background: str) -> float:
    def luminance(color: str) -> float:
        red, green, blue = Image.new("RGB", (1, 1), color).getpixel((0, 0))
        return (
            0.2126 * _linearized(red)
            + 0.7152 * _linearized(green)
            + 0.0722 * _linearized(blue)
        )

    light, dark = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


class IdentityOverlayTests(unittest.TestCase):
    def test_layout_is_prominent_and_inside_portrait_safe_zone(self) -> None:
        image = Image.new("RGBA", (identity_overlay.WIDTH, identity_overlay.HEIGHT))
        layout, _, _ = identity_overlay.calculate_layout(
            "Harshita", "Saks Global", ImageDraw.Draw(image)
        )

        self.assertEqual(layout.name_card[0], 96)
        self.assertEqual(layout.name_card[1], 210)
        self.assertGreaterEqual(layout.name_card[2] - layout.name_card[0], 720)
        self.assertGreaterEqual(layout.company_card[2] - layout.company_card[0], 660)
        self.assertLessEqual(layout.name_card[2], 984)
        self.assertLessEqual(layout.company_card[2], 984)
        self.assertGreater(layout.company_card[1], layout.name_card[3])
        self.assertLessEqual(layout.company_card[3], 440)
        self.assertEqual(layout.name_font_size, 64)
        self.assertEqual(layout.company_font_size, 52)

    def test_longer_identity_shrinks_to_fit_without_clipping(self) -> None:
        image = Image.new("RGBA", (identity_overlay.WIDTH, identity_overlay.HEIGHT))
        layout, _, _ = identity_overlay.calculate_layout(
            "Christopher Alexander",
            "Publicis Sapient International",
            ImageDraw.Draw(image),
        )

        name_available = (
            layout.name_card[2]
            - layout.name_text[0]
            - identity_overlay.HORIZONTAL_PADDING
        )
        company_available = (
            layout.company_card[2]
            - layout.company_text[0]
            - identity_overlay.HORIZONTAL_PADDING
        )
        self.assertLessEqual(layout.name_text_width, name_available)
        self.assertLessEqual(layout.company_text_width, company_available)
        self.assertGreaterEqual(layout.name_font_size, identity_overlay.MIN_NAME_FONT_SIZE)
        self.assertGreaterEqual(
            layout.company_font_size, identity_overlay.MIN_COMPANY_FONT_SIZE
        )

    def test_render_uses_opaque_high_contrast_cards(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "identity.png"
            layout = identity_overlay.render_identity_png(
                "Harshita", "Saks Global", output
            )
            image = Image.open(output).convert("RGBA")

        name_sample = image.getpixel((layout.name_card[2] - 20, layout.name_card[1] + 20))
        company_sample = image.getpixel(
            (layout.company_card[2] - 20, layout.company_card[1] + 20)
        )
        self.assertEqual(image.size, (1080, 1920))
        self.assertGreaterEqual(name_sample[3], 240)
        self.assertGreaterEqual(company_sample[3], 240)
        self.assertGreaterEqual(
            _contrast_ratio(
                identity_overlay.NAME_FOREGROUND,
                identity_overlay.NAME_BACKGROUND,
            ),
            4.5,
        )
        self.assertGreaterEqual(
            _contrast_ratio(
                identity_overlay.COMPANY_FOREGROUND,
                identity_overlay.COMPANY_BACKGROUND,
            ),
            4.5,
        )


if __name__ == "__main__":
    unittest.main()
