"""
make_icon.py - draws the NYX app logo and saves it as nyx.ico / nyx.png.

The logo: a night-sky rounded square with a glowing orb (Nyx's "standing by"
colours, cyan -> violet) and a crescent moon cut out of it - Nyx being the
goddess of the night.

Run:  python tools\make_icon.py      (needs Pillow + numpy)
"""
import os

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter

S = 1024                      # draw big, scale down for each icon size
HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(os.path.dirname(HERE), "assets")


def hex_rgb(h):
    h = h.lstrip("#")
    return np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)], dtype=np.float32)


def radial(size, cx, cy, radius):
    """0 at (cx, cy) -> 1 at `radius`, clipped."""
    y, x = np.mgrid[0:size, 0:size].astype(np.float32)
    return np.clip(np.hypot(x - cx, y - cy) / radius, 0, 1)


def lerp_colours(t, stops):
    """t: array 0..1, stops: [(pos, '#hex'), ...] -> HxWx3 float array."""
    out = np.zeros(t.shape + (3,), dtype=np.float32)
    for (p0, c0), (p1, c1) in zip(stops, stops[1:]):
        m = (t >= p0) & (t <= p1)
        k = ((t - p0) / max(p1 - p0, 1e-6))[..., None]
        out[m] = (hex_rgb(c0) * (1 - k) + hex_rgb(c1) * k)[m]
    return out


def to_img(rgb, alpha=None):
    rgb = np.clip(rgb, 0, 255).astype(np.uint8)
    img = Image.fromarray(rgb, "RGB").convert("RGBA")
    if alpha is not None:
        img.putalpha(alpha)
    return img


def draw():
    # --- background: rounded square, deep night gradient ------------------
    t = radial(S, S * 0.5, S * 0.38, S * 0.85)
    bg = to_img(lerp_colours(t, [(0, "#1e1b4b"), (0.55, "#0f1229"), (1, "#05070f")]))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([24, 24, S - 24, S - 24], radius=220, fill=255)
    bg.putalpha(mask)

    # --- a few stars ------------------------------------------------------
    stars = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(stars)
    rng = np.random.default_rng(7)
    for _ in range(38):
        x, y = rng.uniform(110, S - 110, 2)
        if np.hypot(x - S / 2, y - S / 2) < 360:        # keep the orb area clear
            continue
        r = rng.uniform(2.5, 6.5)
        a = int(rng.uniform(90, 230))
        d.ellipse([x - r, y - r, x + r, y + r], fill=(220, 235, 255, a))
    stars = stars.filter(ImageFilter.GaussianBlur(1.2))

    # --- outer glow ---------------------------------------------------------
    cx = cy = S / 2
    R = 300
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([cx - R - 40, cy - R - 40, cx + R + 40, cy + R + 40],
                                 fill=(56, 189, 248, 170))
    glow = glow.filter(ImageFilter.GaussianBlur(70))

    # --- the orb: cyan -> violet sphere with a soft highlight ---------------
    t = radial(S, cx - R * 0.35, cy - R * 0.4, R * 1.75)
    orb_rgb = lerp_colours(t, [(0, "#e0f7ff"), (0.18, "#67e8f9"), (0.5, "#38bdf8"),
                               (0.8, "#7c6cf0"), (1, "#5b21b6")])
    orb_mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(orb_mask).ellipse([cx - R, cy - R, cx + R, cy + R], fill=255)

    # crescent: bite a circle out of the orb, offset up-right
    bite = Image.new("L", (S, S), 0)
    bx, by, br = cx + R * 0.42, cy - R * 0.30, R * 0.86
    ImageDraw.Draw(bite).ellipse([bx - br, by - br, bx + br, by + br], fill=255)
    crescent_mask = ImageChops.subtract(orb_mask, bite)
    crescent = to_img(orb_rgb, crescent_mask.filter(ImageFilter.GaussianBlur(1.5)))

    # faint "night side" of the moon so the full orb shape still reads
    dark = to_img(lerp_colours(radial(S, cx, cy, R), [(0, "#312e81"), (1, "#1e1b4b")]),
                  ImageChops.multiply(orb_mask, bite).point(lambda v: v * 0.55))

    # thin bright rim around the whole orb (the app's ring)
    rim = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(rim).ellipse([cx - R - 34, cy - R - 34, cx + R + 34, cy + R + 34],
                                outline=(125, 211, 252, 200), width=10)
    rim = rim.filter(ImageFilter.GaussianBlur(1.5))

    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    for layer in (bg, stars, glow, rim, dark, crescent):
        img = Image.alpha_composite(img, layer)
    # clip everything to the rounded square
    final = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    final.paste(img, mask=mask)
    return final


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    img = draw()
    img.resize((512, 512), Image.LANCZOS).save(os.path.join(OUT_DIR, "nyx.png"))
    sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (24, 24), (16, 16)]
    img.save(os.path.join(OUT_DIR, "nyx.ico"), sizes=sizes)
    print("wrote", os.path.join(OUT_DIR, "nyx.ico"))


if __name__ == "__main__":
    main()
