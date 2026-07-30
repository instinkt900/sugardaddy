"""Generate the notification badge icon from the app icon.

    python tools/make_badge_icon.py        # needs: pip install pillow

Android's status bar does not draw the badge you hand it. It keeps only the
**alpha channel**, paints the result white, and shrinks it to about 24dp. So the
badge has to be a silhouette on transparency: point it at an opaque icon and the
status bar shows a featureless white square. That is the whole reason this file
exists rather than reusing icon-192.png.

The app icon is a flat two-colour mark — an accent-blue rounded square with a
white droplet — so the silhouette is exactly "the white pixels". Extracting it
here keeps the badge in step with the icon by construction, instead of being a
hand-drawn lookalike that quietly drifts.

The droplet is also cropped out of its padding and scaled to fill the frame. The
installed-app icons can afford generous margins; a 24dp status-bar glyph cannot,
for the same reason the favicon drops its padding.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageChops

ICONS = Path(__file__).resolve().parent.parent / "sugardaddy" / "static" / "icons"
SOURCE = ICONS / "icon-512.png"
TARGET = ICONS / "icon-badge-96.png"

SIZE = 96          # 96px covers the densest screens at 24dp
MARGIN_PCT = 0.06  # a little breathing room so the tip isn't clipped
# The source has no antialiasing (three flat colours), so "is this pixel white?"
# is unambiguous; the threshold only guards against a future re-export with AA.
WHITE_MIN = 200


def main() -> int:
    src = Image.open(SOURCE).convert("RGBA")

    # Alpha = wherever the source is white (the droplet). Taking the darkest of
    # R/G/B means "white" needs all three channels high, so the blue field can't
    # sneak in; multiplying by the source alpha drops the transparent corners
    # outside the rounded square.
    r, g, b, a = src.split()
    darkest = ImageChops.darker(ImageChops.darker(r, g), b)
    mask = darkest.point(lambda v: 255 if v >= WHITE_MIN else 0)
    mask = ImageChops.multiply(mask, a.point(lambda v: 255 if v else 0))

    box = mask.getbbox()
    if box is None:
        raise SystemExit(f"no white droplet found in {SOURCE.name}")
    mask = mask.crop(box)

    # Square it off before scaling so the droplet keeps its proportions.
    side = max(mask.size)
    inner = round(side * (1 + MARGIN_PCT * 2))
    canvas = Image.new("L", (inner, inner), 0)
    canvas.paste(mask, ((inner - mask.width) // 2, (inner - mask.height) // 2))
    canvas = canvas.resize((SIZE, SIZE), Image.LANCZOS)

    # White RGB under the mask: Android repaints it anyway, but other surfaces
    # (desktop Chrome) render the pixels as given.
    badge = Image.merge("RGBA", (
        Image.new("L", canvas.size, 255),
        Image.new("L", canvas.size, 255),
        Image.new("L", canvas.size, 255),
        canvas,
    ))
    badge.save(TARGET)
    opaque = sum(canvas.histogram()[128:])
    print(f"wrote {TARGET.relative_to(ICONS.parent.parent.parent)} "
          f"({SIZE}x{SIZE}, {100 * opaque / (SIZE * SIZE):.0f}% opaque)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
