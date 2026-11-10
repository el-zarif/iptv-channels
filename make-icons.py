#!/usr/bin/env python3
"""Draw the app icon and write the platform formats.

Build-time tool only: the application itself does not import Pillow.

    python3 make-icons.py              # render the default style
    python3 make-icons.py --style solid
    python3 make-icons.py --style disc

Writes into ./icons/:
    icon.png         1024 master
    icon-256.png ... icon-16.png
    icon.ico         Windows, multi-size 16-256

Each size is drawn at that size, not downscaled from one master, so the
outline stays crisp and the antennae are dropped where they would turn into
noise (at or below 48 px).
"""
import os
import sys

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "icons")

BG_TOP = (27, 31, 39)
BG_BOTTOM = (14, 16, 20)
STROKE = (231, 233, 238)
CYAN = (56, 206, 255)
BLUE = (40, 104, 200)

STYLES = ("outline", "solid", "disc")
PNG_SIZES = (1024, 512, 256, 128, 64, 32, 16)


def rounded_background(size):
    grad = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / max(size - 1, 1)
        grad.putpixel((0, y), tuple(
            int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t) for i in range(3)))
    bg = grad.resize((size, size)).convert("RGBA")
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size - 1, size - 1), radius=int(size * 0.18), fill=255)
    bg.putalpha(mask)
    return bg


def fill_for(size):
    """Below 64 px the cyan->blue ramp turns into two muddy tones, so small
    sizes get the flat accent colour instead."""
    if size < 64:
        return Image.new("RGBA", (size, size), CYAN + (255,))
    return gradient_fill(size)


def play_mask(size, box):
    """White triangle on black, to be used as a paste mask."""
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).polygon(box, fill=255)
    return mask


def gradient_fill(size):
    """Left-to-right cyan -> blue, used inside a play mask."""
    fill = Image.new("RGBA", (size, size))
    for x in range(size):
        t = x / max(size - 1, 1)
        colour = tuple(int(CYAN[i] + (BLUE[i] - CYAN[i]) * t) for i in range(3))
        ImageDraw.Draw(fill).line((x, 0, x, size), fill=colour + (255,))
    return fill


def play_box(cx, cy, height):
    """Triangle centred optically: its centroid, not its bounding box, sits on
    cx. A play triangle measured by bbox looks shoved right. Width is 0.87 of
    height, the usual proportion for a play glyph."""
    width = height * 0.87
    half = height / 2.0
    left = cx - width / 3.0
    return [(left, cy - half), (left, cy + half), (cx + width * 2 / 3.0, cy)]


def draw_outline(size):
    icon = rounded_background(size)
    draw = ImageDraw.Draw(icon)
    stroke = max(2, int(round(size * 0.030)))
    left, right = size * 0.165, size * 0.835
    top, bottom = size * 0.335, size * 0.775

    if size > 48:                                   # antennae die below this
        cx = size / 2.0
        draw.line((cx, top, cx - size * 0.125, size * 0.175),
                  fill=STROKE, width=stroke)
        draw.line((cx, top, cx + size * 0.125, size * 0.175),
                  fill=STROKE, width=stroke)

    draw.rounded_rectangle((left, top, right, bottom),
                           radius=int(size * 0.05), outline=STROKE,
                           width=stroke)

    inner = (bottom - top) - 2 * stroke
    mask = play_mask(size, play_box((left + right) / 2.0,
                                    (top + bottom) / 2.0, inner * 0.78))
    icon.paste(fill_for(size), (0, 0), mask)
    return icon


def draw_solid(size):
    """Filled screen, dark inset, gradient play. Survives 16 px because the
    outer silhouette is one solid shape."""
    icon = rounded_background(size)
    draw = ImageDraw.Draw(icon)
    left, right = size * 0.145, size * 0.855
    top, bottom = size * 0.325, size * 0.775
    draw.rounded_rectangle((left, top, right, bottom),
                           radius=int(size * 0.06), fill=STROKE)
    inset = max(3, size * 0.04)
    draw.rounded_rectangle((left + inset, top + inset,
                            right - inset, bottom - inset),
                           radius=int(size * 0.035), fill=BG_BOTTOM)
    if size > 48:
        cx = size / 2.0
        draw.line((cx, top, cx - size * 0.125, size * 0.155),
                  fill=STROKE, width=max(2, int(size * 0.032)))
        draw.line((cx, top, cx + size * 0.125, size * 0.155),
                  fill=STROKE, width=max(2, int(size * 0.032)))

    inner = (bottom - top) - 2 * inset
    mask = play_mask(size, play_box((left + right) / 2.0,
                                    (top + bottom) / 2.0, inner * 0.74))
    icon.paste(fill_for(size), (0, 0), mask)
    return icon


def draw_disc(size):
    """Gradient disc with a knocked-out play triangle. No detail to lose, so
    it is the most legible of the three at 16 px."""
    icon = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    disc = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    fill = gradient_fill(size)
    ring = Image.new("L", (size, size), 0)
    ImageDraw.Draw(ring).ellipse((0, 0, size - 1, size - 1), fill=255)
    disc.paste(fill, (0, 0), ring)
    icon.alpha_composite(disc)

    mask = play_mask(size, play_box(size * 0.5, size * 0.5, size * 0.50))
    icon.paste((0, 0, 0, 0), (0, 0), mask)
    return icon


def draw_mini(size):
    """16 px: a TV outline leaves three pixels of screen and reads as noise,
    so the glyph reduces to a play triangle."""
    icon = rounded_background(size)
    mask = play_mask(size, play_box(size * 0.5, size * 0.5, size * 0.52))
    icon.paste(fill_for(size), (0, 0), mask)
    return icon


def render(style, size):
    if style != "disc" and size < 32:
        return draw_mini(size)
    draw = {"outline": draw_outline, "solid": draw_solid,
            "disc": draw_disc}[style]
    return draw(size)


def main(argv):
    style = "outline"
    if "--style" in argv:
        style = argv[argv.index("--style") + 1]
        if style not in STYLES:
            print("style must be one of: %s" % ", ".join(STYLES))
            return 2
    os.makedirs(OUT, exist_ok=True)

    master = render(style, 1024)
    master.save(os.path.join(OUT, "icon.png"))
    print("wrote %s" % os.path.join(OUT, "icon.png"))

    for size in PNG_SIZES[1:]:
        path = os.path.join(OUT, "icon-%d.png" % size)
        render(style, size).save(path)
        print("wrote %s" % path)

    ico = os.path.join(OUT, "icon.ico")
    ico_sizes = (16, 24, 32, 48, 64, 128, 256)
    frames = [render(style, s) for s in ico_sizes]
    frames[-1].save(ico, sizes=[(s, s) for s in ico_sizes],
                    append_images=frames[:-1])
    print("wrote %s" % ico)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
