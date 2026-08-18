"""Erzeugt eigene Block-Texturen im Minecraft-Stil.

Bewusst selbst gezeichnet und nicht aus dem Spiel entnommen: Mojangs Texturen
sind urheberrechtlich geschuetzt und duerfen nicht auf einer eigenen Webseite
weiterverbreitet werden. Der Look entsteht aus 16x16 Pixeln mit leicht
streuender Helligkeit - genau das macht die Vorlage im Spiel auch.
"""
import os, random
from PIL import Image

HIER = os.path.dirname(os.path.abspath(__file__))
ZIEL = os.path.join(HIER, "..", "static", "blocks")
os.makedirs(ZIEL, exist_ok=True)


def textur(name, grund, streuung, seed, adern=None):
    random.seed(seed)
    im = Image.new("RGB", (16, 16))
    px = im.load()
    for y in range(16):
        for x in range(16):
            d = random.randint(-streuung, streuung)
            px[x, y] = tuple(max(0, min(255, k + d)) for k in grund)
    # ein paar dunklere Flecken, sonst wirkt es wie Rauschen statt wie Gestein
    if adern:
        for _ in range(adern):
            fx, fy = random.randint(0, 15), random.randint(0, 15)
            for dx in range(random.randint(1, 3)):
                for dy in range(random.randint(1, 2)):
                    x, y = (fx + dx) % 16, (fy + dy) % 16
                    r, g, b = px[x, y]
                    px[x, y] = (max(0, r - 18), max(0, g - 18), max(0, b - 18))
    pfad = os.path.join(ZIEL, name + ".png")
    im.save(pfad, "PNG", optimize=True)
    print("%-10s %5d Bytes" % (name, os.path.getsize(pfad)))


textur("stone", (104, 104, 104), 11, 7, adern=6)
textur("deepslate", (58, 58, 64), 9, 11, adern=5)
textur("dirt", (110, 78, 52), 12, 3, adern=4)
