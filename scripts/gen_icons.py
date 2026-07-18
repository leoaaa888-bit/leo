"""Generate multi-size PNG/ICO icons from the brand mark geometry."""
from PIL import Image, ImageDraw
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "static" / "icons"
OUT.mkdir(parents=True, exist_ok=True)

BG = (14, 16, 20, 255)
GREEN = (61, 220, 132, 255)
GREEN_LIGHT = (110, 240, 164, 255)
GREEN_DARK = (43, 184, 106, 255)
SCREEN_TOP = (26, 36, 48, 255)
SCREEN_BOT = (11, 16, 22, 255)


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(4))


def rounded_rect(draw, box, radius, fill):
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def make_icon(size, maskable=False, rounded_tile=True):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0) if not maskable else BG)
    draw = ImageDraw.Draw(img)

    if not maskable and rounded_tile:
        r = max(2, int(size * 0.22))
        rounded_rect(draw, (0, 0, size - 1, size - 1), r, BG)
    elif not maskable:
        draw.rectangle((0, 0, size, size), fill=BG)

    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    cx = cy = size // 2
    gr = int(size * (0.38 if maskable else 0.33))
    gdraw.ellipse((cx - gr, cy - gr, cx + gr, cy + gr), fill=(61, 220, 132, 28))
    img = Image.alpha_composite(img, glow)
    draw = ImageDraw.Draw(img)

    if maskable:
        pw, ph = int(size * 0.31), int(size * 0.625)
    else:
        pw, ph = int(size * 0.344), int(size * 0.656)
    px = (size - pw) // 2
    py = (size - ph) // 2
    pr = max(2, int(pw * 0.205))

    body = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    bdraw = ImageDraw.Draw(body)
    for y in range(py, py + ph):
        t = (y - py) / max(1, ph - 1)
        color = lerp((74, 232, 146, 255), GREEN_DARK, t)
        bdraw.line([(px, y), (px + pw - 1, y)], fill=color)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (px, py, px + pw - 1, py + ph - 1), radius=pr, fill=255
    )
    body.putalpha(mask)
    img = Image.alpha_composite(img, body)
    draw = ImageDraw.Draw(img)

    inset_x = max(1, int(pw * 0.09))
    inset_top = max(1, int(ph * 0.07))
    inset_bot = max(1, int(ph * 0.095))
    sx0 = px + inset_x
    sy0 = py + inset_top
    sx1 = px + pw - inset_x
    sy1 = py + ph - inset_bot
    sr = max(2, int(pw * 0.1))

    screen = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(screen)
    for y in range(sy0, max(sy0 + 1, sy1)):
        t = (y - sy0) / max(1, sy1 - sy0 - 1)
        color = lerp(SCREEN_TOP, SCREEN_BOT, t)
        sdraw.line([(sx0, y), (sx1 - 1, y)], fill=color)
    smask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(smask).rounded_rectangle(
        (sx0, sy0, sx1 - 1, sy1 - 1), radius=sr, fill=255
    )
    screen.putalpha(smask)
    img = Image.alpha_composite(img, screen)
    draw = ImageDraw.Draw(img)

    iw = max(2, int(pw * 0.32))
    ih = max(2, int(ph * 0.042))
    ix = (size - iw) // 2
    iy = sy0 + max(1, int(ph * 0.045))
    rounded_rect(draw, (ix, iy, ix + iw, iy + ih), ih // 2, (14, 16, 20, 220))

    sw = max(2, int(pw * 0.08))
    gap = max(1, int(pw * 0.055))
    total_w = 4 * sw + 3 * gap
    bx0 = (size - total_w) // 2
    base_y = sy0 + int((sy1 - sy0) * 0.72)
    colors = [GREEN, GREEN, GREEN, GREEN_LIGHT]
    for i in range(4):
        bh = max(2, int((sy1 - sy0) * (0.18 + i * 0.08)))
        bx = bx0 + i * (sw + gap)
        by = base_y - bh
        rounded_rect(draw, (bx, by, bx + sw, base_y), max(1, sw // 3), colors[i])

    hw = max(2, int(pw * 0.32))
    hh = max(2, int(ph * 0.024))
    hx = (size - hw) // 2
    hy = py + ph - max(2, int(ph * 0.055))
    rounded_rect(draw, (hx, hy, hx + hw, hy + hh), hh // 2, (7, 20, 12, 120))

    return img


def save_png(img, name):
    path = OUT / name
    img.save(path, "PNG", optimize=True)
    print("wrote", path.name, img.size)


def main():
    for s, name in [
        (16, "icon-16.png"),
        (32, "icon-32.png"),
        (180, "apple-touch-icon.png"),
        (192, "icon-192.png"),
        (512, "icon-512.png"),
    ]:
        save_png(make_icon(s, maskable=False, rounded_tile=True), name)

    save_png(make_icon(512, maskable=True, rounded_tile=False), "icon-maskable-512.png")

    ico_sizes = [16, 32, 48]
    frames = [make_icon(s, maskable=False, rounded_tile=True) for s in ico_sizes]
    frames[0].save(
        OUT / "favicon.ico",
        format="ICO",
        sizes=[(s, s) for s in ico_sizes],
        append_images=frames[1:],
    )
    print("wrote favicon.ico")
    print("done")


if __name__ == "__main__":
    main()
