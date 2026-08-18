"""Schneidet die acht Wappen aus dem Quellbild und macht den Hintergrund frei.

Das Bild ist ein 4x2-Raster. Die Zuordnung erfolgt ueber die Beschriftung im
Bild, nicht ueber die Position - im Quellbild steht Diamond in der oberen und
Platinum in der unteren Reihe, in der Mod ist es andersherum.
"""
import os
from PIL import Image

HIER = os.path.dirname(os.path.abspath(__file__))
QUELLE = os.path.join(HIER, "..", "static", "ranks", "source.png.webp")
ZIEL = os.path.join(HIER, "..", "static", "ranks")

# Zeile fuer Zeile so, wie es im Bild steht
RASTER = [
    ["bronze", "silver", "gold", "diamond"],
    ["platinum", "master", "champion", "legend"],
]

SCHWELLE = 26   # alles darunter gilt als Hintergrund


def freistellen(bild):
    """Schwarzen Hintergrund transparent machen - sonst klebt ein schwarzer
    Kasten auf der Seite. Der Schein am Rand bleibt teilweise erhalten, indem
    dunkle Pixel nicht hart, sondern nach Helligkeit ausgeblendet werden."""
    bild = bild.convert("RGBA")
    px = bild.load()
    b, h = bild.size
    for y in range(h):
        for x in range(b):
            r, g, bl, a = px[x, y]
            hell = max(r, g, bl)
            if hell <= SCHWELLE:
                px[x, y] = (r, g, bl, 0)
            elif hell < SCHWELLE * 3:
                # weicher Uebergang, damit der Schein nicht abrupt abreisst
                px[x, y] = (r, g, bl, int(255 * (hell - SCHWELLE) / (SCHWELLE * 2)))
    return bild


def zuschneiden(bild):
    """Auf den sichtbaren Inhalt zuschneiden, mit etwas Rand."""
    rand = bild.getbbox()
    if not rand:
        return bild
    x0, y0, x1, y1 = rand
    luft = 6
    return bild.crop((max(0, x0 - luft), max(0, y0 - luft),
                      min(bild.size[0], x1 + luft), min(bild.size[1], y1 + luft)))


quelle = Image.open(QUELLE)
B, H = quelle.size
fw, fh = B // 4, H // 2

for zeile, namen in enumerate(RASTER):
    for spalte, name in enumerate(namen):
        # Sicherheitsrand: die Wappen im Quellbild reichen teils in das
        # Nachbarfeld hinein - ohne den Abstand klebt ein Streifen des
        # Nachbarn am Rand (bei Master war es ein rotes Stueck von Champion).
        luft = int(fw * 0.075)
        feld = quelle.crop((spalte * fw + luft, zeile * fh + luft,
                            (spalte + 1) * fw - luft, (zeile + 1) * fh - luft))
        feld = zuschneiden(freistellen(feld))
        pfad = os.path.join(ZIEL, name + ".png")
        feld.save(pfad, "PNG", optimize=True)
        print("%-9s %4dx%-4d  %6d Bytes" % (name, feld.size[0], feld.size[1],
                                            os.path.getsize(pfad)))
