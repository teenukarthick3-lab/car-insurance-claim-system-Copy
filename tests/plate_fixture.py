"""Test fixture: renders photo-like chassis plates (metal, rivets, engraved text, angle, noise).
Only used by tests; skipped automatically if the font isn't available on this computer."""
import os
import random

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageEnhance

_FONT_DIRS = ["/usr/share/fonts/truetype/dejavu", r"C:\Windows\Fonts"]
BOLD = next((os.path.join(d, f) for d in _FONT_DIRS for f in ("DejaVuSansCondensed-Bold.ttf", "arialbd.ttf")
             if os.path.exists(os.path.join(d, f))), None)
REG = next((os.path.join(d, f) for d in _FONT_DIRS for f in ("DejaVuSansCondensed.ttf", "arial.ttf")
            if os.path.exists(os.path.join(d, f))), None)


def plate(lines, seed=1, angle=-4, blur=0.8, noise=18, size=(1600, 1100), vin_gap=False):
    rnd = random.Random(seed)
    W, H = size
    # background: painted body metal
    bg = Image.new("RGB", size, (88, 92, 98))
    bg = Image.blend(bg, Image.effect_noise(size, 30).convert("RGB"), 0.25)
    # plate: brushed aluminium
    pw, ph = 1180, 620
    p = Image.new("RGB", (pw, ph), (196, 198, 200))
    streak = Image.effect_noise((pw, ph), 22).convert("L").resize((pw, 8)).resize((pw, ph))
    p = Image.blend(p, Image.merge("RGB", (streak,) * 3), 0.18)
    d = ImageDraw.Draw(p)
    d.rounded_rectangle([6, 6, pw - 7, ph - 7], 22, outline=(120, 122, 126), width=5)
    for cx, cy in [(40, 40), (pw - 40, 40), (40, ph - 40), (pw - 40, ph - 40)]:
        d.ellipse([cx - 16, cy - 16, cx + 16, cy + 16], fill=(150, 152, 155), outline=(90, 92, 95), width=3)
    y = 70
    for text, font, sizept, gap in lines:
        f = ImageFont.truetype(font, sizept)
        x = 90
        if text == "@VIN" and vin_gap:
            text = vin_gap
        d.text((x + 2, y + 2), text, font=f, fill=(235, 236, 238))      # engraving highlight
        d.text((x, y), text, font=f, fill=(38, 40, 44))
        y += sizept + gap
    p = p.rotate(angle, expand=True, resample=Image.BICUBIC, fillcolor=(0, 0, 0, 0))
    mask = Image.new("L", (pw, ph), 255).rotate(angle, expand=True)
    bg.paste(p, ((W - p.width) // 2, (H - p.height) // 2), mask)
    # lighting gradient + glare
    glare = Image.new("L", size, 0)
    ImageDraw.Draw(glare).ellipse([W * .55, -H * .2, W * 1.2, H * .5], fill=70)
    glare = glare.filter(ImageFilter.GaussianBlur(120))
    bg = Image.composite(Image.new("RGB", size, (255, 255, 255)), bg, glare)
    bg = Image.blend(bg, Image.effect_noise(size, noise).convert("RGB"), 0.10)
    bg = bg.filter(ImageFilter.GaussianBlur(blur))
    return ImageEnhance.Contrast(bg).enhance(0.95)


