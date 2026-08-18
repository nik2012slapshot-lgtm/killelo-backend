"""Erzeugt die Rang-Wappen als SVG (echte Pixelkanten, keine Bilddatei).

Die Schildform steht als Zeichenraster - da zaehlt jede Kante. Die Schwerter
werden dagegen gerechnet: Klinge, Parierstange, Griff und Knauf entlang einer
Diagonalen. Von Hand gemalt wurden daraus vorher zwei Striche, die wie ein X
aussahen statt wie Waffen.
"""
import io, os

# 24 breit - genug fuer Rand, Innenflaeche und Glanzkante
SHIELD = [
    "......oooooooooooo......",
    "....oohhhhhhhhhhhhoo....",
    "...ohhhhhhhhhhhhhhhho...",
    "..ohhhhhhhhhhhhhhhhhho..",
    ".ohhhhhmmmmmmmmmmhhhhho.",
    ".ohhhmmmmmmmmmmmmmmhhho.",
    "ohhhmmmmmmmmmmmmmmmmdhho",
    "ohhmmmmmmmmmmmmmmmmmmdho",
    "ohmmmmmmmmmmmmmmmmmmmmdo",
    "ohmmmmmmmmmmmmmmmmmmmmdo",
    "ohmmmmmmmmmmmmmmmmmmmmdo",
    "ohmmmmmmmmmmmmmmmmmmmddo",
    ".ommmmmmmmmmmmmmmmmmddo.",
    ".ommmmmmmmmmmmmmmmmddo..",
    "..ommmmmmmmmmmmmmmddo...",
    "...ommmmmmmmmmmmmddo....",
    "....ommmmmmmmmmmddo.....",
    ".....ommmmmmmmmddo......",
    "......ommmmmmmddo.......",
    ".......ommmmmddo........",
    "........ommmddo.........",
    ".........ommdo..........",
    "..........oddo..........",
    "...........oo...........",
]

CROWN = [
    "........o.....o.........",
    ".......oko...oko........",
    "......okko.ookko........",
    ".....okkkokokkkko.......",
    "......okkkkkkkkko.......",
    ".......ooooooooo........",
]

PX = 8


def leer(w, h):
    return [["." for _ in range(w)] for _ in range(h)]


def setz(g, x, y, ch):
    if 0 <= y < len(g) and 0 <= x < len(g[0]):
        g[y][x] = ch


def schwert(g, x0, y0, x1, y1):
    """Zeichnet ein Schwert vom Knauf (x0,y0) zur Spitze (x1,y1).

    Bewusst schmal: eine breite Klinge deckt das halbe Wappen zu und sieht
    dann aus wie ein aufgemaltes X statt wie eine Waffe.
    """
    schritte = max(abs(x1 - x0), abs(y1 - y0))
    quer = (1, 0) if abs(y1 - y0) > abs(x1 - x0) else (0, 1)
    for i in range(schritte + 1):
        t = i / schritte
        x = round(x0 + (x1 - x0) * t)
        y = round(y0 + (y1 - y0) * t)
        if t < 0.07:                      # Knauf
            for q in (-1, 0, 1):
                setz(g, x + q * quer[0], y + q * quer[1], "G")
        elif t < 0.20:                    # Griff
            setz(g, x, y, "G")
        elif t < 0.28:                    # Parierstange, quer zur Klinge
            for q in (-2, -1, 0, 1, 2):
                setz(g, x + q * quer[0], y + q * quer[1], "P")
        elif t < 0.93:                    # Klinge mit heller Schneide
            setz(g, x, y, "S")
            setz(g, x + quer[0], y + quer[1], "K")
        else:                             # Spitze
            setz(g, x, y, "K")


def rects(grid, colors, dy=0, dx=0):
    out = []
    for y, row in enumerate(grid):
        x = 0
        while x < len(row):
            ch = row[x]
            if ch == "." or ch not in colors:
                x += 1
                continue
            run = 1
            while x + run < len(row) and row[x + run] == ch:
                run += 1
            out.append('<rect x="%d" y="%d" width="%d" height="%d" fill="%s"/>'
                       % ((x + dx) * PX, (y + dy) * PX, run * PX, PX, colors[ch]))
            x += run
    return out


RANKS = [
    # name,      o,         h,         m,         d,         glut,      krone, stein, funken
    ("BRONZE",   "#3d1c0b", "#e89a5c", "#b5652c", "#6d3512", "#e07c34", False, False, 0),
    ("SILVER",   "#28303a", "#f4f8fb", "#b8c2cc", "#69757f", "#cfd9e2", False, False, 1),
    ("GOLD",     "#4a3606", "#fff0b0", "#ffc93c", "#94690c", "#ffd34d", False, False, 3),
    ("PLATINUM", "#093f3d", "#d6fffb", "#5ae6e0", "#177470", "#6ff0ea", False, True,  4),
    ("DIAMOND",  "#082murks", "#cfe8ff", "#4fa6ff", "#14508f", "#5fb0ff", False, True, 5),
    ("MASTER",   "#341552", "#f3dfff", "#c77dff", "#6f37a0", "#d08cff", True,  True,  6),
    ("CHAMPION", "#240750", "#e0c2ff", "#9b4dff", "#511a9e", "#a95dff", True,  True,  8),
    ("LEGEND",   "#520810", "#ffd2d7", "#ff4d5e", "#96101c", "#ff5f6f", True,  True,  11),
]
RANKS[4] = ("DIAMOND", "#082a52", "#cfe8ff", "#4fa6ff", "#14508f", "#5fb0ff", False, True, 5)


def build(name, o, h, m, d, glut, krone, stein, funken):
    W = len(SHIELD[0])
    oben = len(CROWN)          # immer gleich viel Kopfraum
    H = len(SHIELD) + oben + 5

    sw = leer(W, len(SHIELD))
    schwert(sw, 2, len(SHIELD) - 6, W - 3, 1)
    schwert(sw, W - 3, len(SHIELD) - 6, 2, 1)

    t = []
    t.append('<defs>'
             '<radialGradient id="gl" cx="50%%" cy="42%%" r="58%%">'
             '<stop offset="0%%" stop-color="%s" stop-opacity="%.2f"/>'
             '<stop offset="55%%" stop-color="%s" stop-opacity="%.2f"/>'
             '<stop offset="100%%" stop-color="%s" stop-opacity="0"/>'
             '</radialGradient></defs>'
             % (glut, 0.40 + funken * 0.035, glut, 0.16 + funken * 0.02, glut))
    t.append('<rect width="100%" height="100%" fill="url(#gl)"/>')

    if krone:
        t += rects(CROWN, {"o": o, "k": h})
    t += rects(SHIELD, {"o": o, "h": h, "m": m, "d": d}, dy=oben)

    if stein:
        cx, cy = W // 2 - 1, oben + 7
        t.append('<rect x="%d" y="%d" width="%d" height="%d" fill="%s"/>'
                 % (cx * PX, cy * PX, PX * 2, PX * 2, h))
        t.append('<rect x="%d" y="%d" width="%d" height="%d" fill="#ffffff" opacity=".9"/>'
                 % (cx * PX, cy * PX, PX, PX))

    t += rects(sw, {"S": "#c8d2dc", "K": "#ffffff", "P": h, "G": o}, dy=oben)

    for i in range(funken):
        seite = -1 if i % 2 == 0 else 1
        fx = W // 2 + seite * (W // 2 - 1 - (i % 4))
        fy = oben + 1 + (i * 3) % (len(SHIELD) - 6)
        t.append('<rect x="%d" y="%d" width="%d" height="%d" fill="%s" opacity=".9"/>'
                 % (fx * PX, fy * PX, PX, PX, glut))

    by = (len(SHIELD) + oben + 1) * PX
    bw = W * PX
    t.append('<rect x="%d" y="%d" width="%d" height="%d" fill="%s"/>'
             % (PX * 2, by, bw - PX * 4, PX * 3, o))
    t.append('<rect x="%d" y="%d" width="%d" height="%d" fill="none" stroke="%s" stroke-width="2"/>'
             % (PX * 2 + 3, by + 3, bw - PX * 4 - 6, PX * 3 - 6, m))
    t.append('<text x="%d" y="%d" font-family="monospace" font-size="%d" font-weight="700" '
             'fill="%s" text-anchor="middle" letter-spacing="1.5">%s</text>'
             % (bw // 2, by + PX * 2 + 3, int(PX * 1.6), h, name))

    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" '
            'shape-rendering="crispEdges" role="img" aria-label="%s">%s</svg>'
            % (bw, H * PX, name, "".join(t)))


ziel = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "static", "ranks")
os.makedirs(ziel, exist_ok=True)
for r in RANKS:
    io.open(os.path.join(ziel, r[0].lower() + ".svg"), "w", encoding="utf-8").write(build(*r))
    print("%-9s ok" % r[0])
