# -*- coding: utf-8 -*-
"""Convert generated PNG icon -> multi-size .ico + web favicon png."""
from PIL import Image

SRC = r"D:\Download\workworkwork\python_playground\Message-cli\assets\Flat_minimalist_desktop_app_ic_2026-09-10T04-07-04.png"
ICO = r"D:\Download\workworkwork\python_playground\Message-cli\assets\mail-app.ico"
FAV = r"D:\Download\workworkwork\python_playground\Message-cli\web\icon.png"

img = Image.open(SRC).convert("RGB")
w, h = img.size  # 1024x1024

# 1) cover the AI watermark (bottom-right) with the paper color sampled nearby
paper = img.getpixel((500, 55))
px = img.load()
for y in range(930, 1024):
    for x in range(860, 1020):
        px[x, y] = paper

# 2) crop away outer white margins -> keep the rounded-square tile
from PIL import ImageChops
bg = Image.new("RGB", img.size, (255, 255, 255))
bbox = ImageChops.difference(img, bg).getbbox()
tile = img.crop(bbox)
# make it exactly square (it should already be)
s = min(tile.size)
tile = tile.crop(((tile.width - s) // 2, (tile.height - s) // 2,
                  (tile.width + s) // 2, (tile.height + s) // 2))

# 3) rounded transparent corners, then export favicon png + multi-size ico
from PIL import ImageDraw

def rounded(im, radius_frac=0.22):
    s = im.size[0]
    mask = Image.new("L", (s, s), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * radius_frac), fill=255)
    out = im.convert("RGBA")
    out.putalpha(mask)
    return out

base = rounded(tile.resize((512, 512), Image.LANCZOS))
base.resize((256, 256), Image.LANCZOS).save(FAV, "PNG")

base.resize((256, 256), Image.LANCZOS).save(
    ICO, format="ICO",
    sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
)
print("tile bbox:", bbox, "-> ico + favicon written (rounded corners)")
