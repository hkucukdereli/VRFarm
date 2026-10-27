"""
controller/palette.py

Rig identity colours. A fixed palette of NINE colours, generated from evenly spaced hues (every
40 degrees) at one lightness and saturation, so they are distinct from each other and legible on
the dark UI without anyone hand-picking them. The Network tab offers the palette in a drop-down;
the chosen colour is stored in the rig YAML as `color: '#rrggbb'` (the resolved hex, not an
index, so a later change to the generator never recolours an existing rig), and the shell
paints it on the rig's tabs in Setup and Experiment.
"""
from __future__ import annotations
import colorsys
import random
import re

N = 9
# Hue names in generation order (0°, 40°, 80° … 320°) — labels for the drop-down only.
_NAMES = ["red", "orange", "lime", "green", "teal", "sky", "blue", "violet", "pink"]
_HEX_RE = re.compile(r"^#?([0-9a-f]{6})$")


def _hex(r: float, g: float, b: float) -> str:
    return "#%02x%02x%02x" % tuple(max(0, min(255, round(c * 255))) for c in (r, g, b))


def generate(n: int = N, lightness: float = 0.60, saturation: float = 0.65) -> list[dict]:
    """n evenly spaced hues -> [{"id", "name", "hex"}]. One HLS lightness/saturation for all of
    them keeps every colour at the same visual weight, which is what makes the tabs of different
    rigs read as one set."""
    out = []
    for i in range(n):
        r, g, b = colorsys.hls_to_rgb(i / n, lightness, saturation)
        out.append({"id": i,
                    "name": _NAMES[i] if i < len(_NAMES) else f"hue{round(360 * i / n)}",
                    "hex": _hex(r, g, b)})
    return out


PALETTE: list[dict] = generate()
HEXES: list[str] = [c["hex"] for c in PALETTE]


def normalize(value) -> str | None:
    """A palette index (0-8, int or digit string) or a '#rrggbb' colour -> lowercase '#rrggbb';
    anything else (None, '', junk) -> None. Any valid hex is accepted, not only palette members,
    so a YAML edited by hand keeps working."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int) or (isinstance(value, str) and value.strip().isdigit()):
        i = int(value)
        return HEXES[i] if 0 <= i < len(HEXES) else None
    m = _HEX_RE.match(str(value).strip().lower())
    return f"#{m.group(1)}" if m else None


def pick_unused(used, rng=random) -> str:
    """A random palette colour not in `used` (any palette colour once all nine are taken) —
    what Add rig assigns when no colour was chosen."""
    taken = {normalize(u) for u in used}
    free = [h for h in HEXES if h not in taken] or list(HEXES)
    return rng.choice(free)


def text_on(hex_color: str) -> str:
    """'#000' or '#fff', whichever reads better on the colour (WCAG relative luminance)."""
    h = (normalize(hex_color) or "#000000")[1:]

    def lin(c: int) -> float:
        c /= 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (lin(int(h[i:i + 2], 16)) for i in (0, 2, 4))
    return "#000" if (0.2126 * r + 0.7152 * g + 0.0722 * b) > 0.4 else "#fff"
