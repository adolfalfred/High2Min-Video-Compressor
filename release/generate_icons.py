"""Generate PNG, Windows ICO, and macOS ICNS assets for the High2Min icon.

Install Pillow in a disposable build environment before running this script.
The committed SVG is the editable vector master; this script reproduces its
geometry as antialiased raster assets for native application packaging.
"""

from __future__ import annotations

from pathlib import Path

try:
    from PIL import Image, ImageDraw
except ImportError as exc:  # pragma: no cover - developer-only helper
    raise SystemExit("Pillow is required: python -m pip install Pillow") from exc


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ASSET_ROOT = PROJECT_ROOT / "src" / "adt_video_publisher" / "assets"
SIZE = 1024


def _gradient(start: tuple[int, int, int], end: tuple[int, int, int]) -> Image.Image:
    image = Image.new("RGBA", (SIZE, SIZE))
    pixels = image.load()
    for y in range(SIZE):
        for x in range(SIZE):
            amount = (x + y) / (2 * (SIZE - 1))
            pixels[x, y] = tuple(
                round(first + (last - first) * amount)
                for first, last in zip(start, end, strict=True)
            ) + (255,)
    return image


def _rounded_gradient(
    canvas: Image.Image,
    box: tuple[int, int, int, int],
    radius: int,
    start: tuple[int, int, int],
    end: tuple[int, int, int],
) -> None:
    mask = Image.new("L", (SIZE, SIZE))
    ImageDraw.Draw(mask).rounded_rectangle(box, radius=radius, fill=255)
    canvas.alpha_composite(Image.composite(_gradient(start, end), Image.new("RGBA", canvas.size), mask))


def build_icon() -> Image.Image:
    canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    _rounded_gradient(canvas, (32, 32, 992, 992), 224, (7, 26, 51), (11, 65, 97))
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle(
        (67, 67, 957, 957), radius=192, outline=(117, 242, 209, 46), width=10
    )
    draw.rounded_rectangle(
        (274, 296, 750, 728), radius=108, fill=(11, 33, 58), outline=(237, 250, 255), width=30
    )
    draw.rounded_rectangle(
        (328, 350, 696, 674), radius=66, fill=(16, 47, 75), outline=(73, 220, 202, 108), width=8
    )
    draw.line(
        [(174, 388), (286, 512), (174, 636)],
        fill=(45, 226, 180),
        width=58,
        joint="curve",
    )
    draw.line(
        [(850, 388), (738, 512), (850, 636)],
        fill=(49, 183, 255),
        width=58,
        joint="curve",
    )
    draw.polygon([(457, 398), (654, 512), (457, 626)], fill=(48, 208, 215))
    draw.ellipse((488, 816, 536, 864), fill=(117, 242, 209, 220))
    return canvas


def main() -> None:
    ASSET_ROOT.mkdir(parents=True, exist_ok=True)
    image = build_icon()
    png = ASSET_ROOT / "high2min-video-compressor.png"
    ico = ASSET_ROOT / "high2min-video-compressor.ico"
    icns = ASSET_ROOT / "high2min-video-compressor.icns"
    image.save(png, format="PNG", optimize=True)
    image.save(ico, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    image.save(icns, format="ICNS", append_images=[image])
    print(f"Generated {png.name}, {ico.name}, and {icns.name}")


if __name__ == "__main__":
    main()
