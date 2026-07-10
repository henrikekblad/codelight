#!/usr/bin/env python3
"""Render a codelight agent SVG logo to the ESP8266 48x48 bitmap format.

The screen client cannot render SVG, so AgentSpec.logo_bitmap stores a 48x48
1-bit bitmap as base64. This helper keeps that conversion repeatable.

Dependencies for SVG input:
    python3 -m pip install cairosvg pillow

Usage:
    python3 companion/tools/logo_bitmap.py path/to/logo.svg
"""
from __future__ import annotations

import argparse
import base64
import io
import sys
from pathlib import Path


WIDTH = 48
HEIGHT = 48
BYTE_COUNT = WIDTH * HEIGHT // 8


def pack_bitmap(pixels: list[bool]) -> bytes:
    """Pack 48x48 booleans into MSB-first bytes used by the screen firmware."""
    if len(pixels) != WIDTH * HEIGHT:
        raise ValueError(f"expected {WIDTH * HEIGHT} pixels, got {len(pixels)}")
    out = bytearray()
    for offset in range(0, len(pixels), 8):
        value = 0
        for bit, enabled in enumerate(pixels[offset:offset + 8]):
            if enabled:
                value |= 1 << (7 - bit)
        out.append(value)
    if len(out) != BYTE_COUNT:
        raise ValueError(f"expected {BYTE_COUNT} bytes, got {len(out)}")
    return bytes(out)


def svg_to_pixels(path: Path, threshold: int) -> list[bool]:
    try:
        import cairosvg  # type: ignore
        from PIL import Image  # type: ignore
    except ImportError as exc:
        raise SystemExit(
            "SVG rendering requires cairosvg and pillow. Install with:\n"
            "  python3 -m pip install cairosvg pillow"
        ) from exc

    svg = path.read_text(encoding="utf-8").replace("currentColor", "#000000")
    png = cairosvg.svg2png(
        bytestring=svg.encode("utf-8"),
        output_width=WIDTH,
        output_height=HEIGHT,
    )
    image = Image.open(io.BytesIO(png)).convert("RGBA")
    pixels: list[bool] = []
    for red, green, blue, alpha in image.getdata():
        luminance = (red * 299 + green * 587 + blue * 114) // 1000
        pixels.append(alpha >= threshold and luminance < 255)
    return pixels


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate AgentSpec.logo_bitmap from an SVG logo.",
    )
    parser.add_argument("svg", type=Path, help="SVG file to rasterize")
    parser.add_argument(
        "--threshold",
        type=int,
        default=32,
        help="alpha threshold for a set pixel, 0-255 (default: 32)",
    )
    args = parser.parse_args()

    if not 0 <= args.threshold <= 255:
        parser.error("--threshold must be between 0 and 255")
    if args.svg.suffix.lower() != ".svg":
        parser.error("input must be an .svg file")

    bitmap = pack_bitmap(svg_to_pixels(args.svg, args.threshold))
    print(base64.b64encode(bitmap).decode("ascii"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
