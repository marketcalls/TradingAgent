"""Colour words to "#rrggbb" for the chart tools.

The model writes what the user said. "draw the bollinger bands and fill with light
shaded yellow color" has to land on a hex string in the settings dict the chart's
indicator runtime reads, because that runtime takes colours in one form only.

Measured against openalgo-charts 1.9.2 in frontend/node_modules:
  - Every colour input on every registered indicator is type "color" with a
    "#rrggbb" default (bollinger basisColor "#f5a623" and bandColor "#4f8cff",
    keltner-channel fillColor "#2196f3"). The settings panel binds those to a
    colour picker, so "#rrggbb" is the one form known to be accepted everywhere.
  - Opacity is not part of a colour there. A band fill's alpha is the descriptor's
    fills[].opacity (0.05 on keltner-channel, envelope, donchian, ma-channel and
    standard-error-bands, 0.08 on the bollinger override, 0.1 to 0.15 on the
    oscillator bands), and a plot's alpha is the generated "<plot>:opacity" style
    input on a 0..100 scale. So "light shaded yellow" carries two facts, a hue and
    a wish for a faint wash, and colour_to_hex returns both: the hex, and an
    opacity hint on the 0..1 scale that the tool layer maps onto whichever key it
    is setting.

Pure Python, no dependencies. Unknown text raises ValueError naming the known
colours; the tool layer turns that into a RetryAgentRun so the model can rephrase.
"""

from __future__ import annotations

import re

__all__ = [
    "BASE_COLOURS",
    "LOW_OPACITY_HINT",
    "SHADES",
    "colour_to_hex",
    "known_colour_names",
]

# The opacity a "light", "pale" or "shaded" colour asks for, on the 0..1 scale.
LOW_OPACITY_HINT = 0.15

# Base hues. CSS values where CSS has the name, so "gold" is the gold every
# stylesheet means; the few words CSS lacks (amber, mint, peach, rose, sky) take
# their common web values. grey and gray are both here because both get typed.
BASE_COLOURS: dict[str, str] = {
    "amber": "#ffbf00",
    "aqua": "#00ffff",
    "beige": "#f5f5dc",
    "black": "#000000",
    "blue": "#0000ff",
    "brown": "#a52a2a",
    "coral": "#ff7f50",
    "crimson": "#dc143c",
    "cyan": "#00ffff",
    "gold": "#ffd700",
    "gray": "#808080",
    "green": "#008000",
    "grey": "#808080",
    "indigo": "#4b0082",
    "khaki": "#f0e68c",
    "lavender": "#e6e6fa",
    "lime": "#00ff00",
    "magenta": "#ff00ff",
    "maroon": "#800000",
    "mint": "#98ff98",
    "navy": "#000080",
    "olive": "#808000",
    "orange": "#ffa500",
    "orchid": "#da70d6",
    "peach": "#ffdab9",
    "pink": "#ffc0cb",
    "purple": "#800080",
    "red": "#ff0000",
    "rose": "#ff007f",
    "salmon": "#fa8072",
    "silver": "#c0c0c0",
    "sky": "#87ceeb",
    "skyblue": "#87ceeb",
    "steelblue": "#4682b4",
    "tan": "#d2b48c",
    "teal": "#008080",
    "tomato": "#ff6347",
    "turquoise": "#40e0d0",
    "violet": "#ee82ee",
    "white": "#ffffff",
    "yellow": "#ffff00",
}

# Shade words: a mix toward white (positive) or black (negative) by that fraction,
# so "light blue" and "dark blue" stay on blue's hue instead of jumping to the CSS
# lightblue and darkblue, which are different hues.
SHADES: dict[str, float] = {
    "light": 0.45,
    "lighter": 0.45,
    "pale": 0.65,
    "paler": 0.65,
    "dark": -0.4,
    "darker": -0.4,
    "deep": -0.4,
}

# Words asking for a faint wash. "light" and "pale" are in both tables on purpose:
# they lighten the hue AND lower the opacity, which is what "light shaded yellow"
# means on a chart.
FAINT_WORDS = frozenset(
    {
        "light",
        "lighter",
        "pale",
        "paler",
        "shaded",
        "shading",
        "faint",
        "translucent",
        "transparent",
    }
)

# Words the model carries along from the user's sentence that name no hue.
NOISE_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "the",
        "in",
        "of",
        "with",
        "very",
        "bright",
        "solid",
        "color",
        "colour",
        "colored",
        "coloured",
        "colors",
        "colours",
        "fill",
        "filled",
        "shade",
        "tint",
        "tinted",
        "tone",
    }
)

# Shade words are the only prefixes a joined word can carry: "lightyellow",
# "darkblue", "paleblue". Longest first so "lighter" is not read as "light" + "er".
_JOINED_PREFIXES = sorted(SHADES, key=len, reverse=True)

# Clamp on the summed shade so "pale pale pale yellow" cannot mix all the way to white.
_SHADE_LIMIT = 0.9

_HEX_RE = re.compile(r"^(#[0-9a-f]{3}|#?[0-9a-f]{6})$")
_RGB_RE = re.compile(
    r"^rgba?\(\s*"
    r"(\d+(?:\.\d+)?)\s*[,\s]\s*(\d+(?:\.\d+)?)\s*[,\s]\s*(\d+(?:\.\d+)?)"
    r"\s*(?:[,/]\s*(\d*\.?\d+)\s*(%?)\s*)?\)$"
)
_SPLIT_RE = re.compile(r"[\s,_/-]+")


def known_colour_names() -> list[str]:
    """The colour words this module resolves, sorted, for error messages and docs.

    Returns:
        The base names only. Shade words (light, pale, dark) combine with any of them.
    """
    return sorted(BASE_COLOURS)


def colour_to_hex(text: str) -> tuple[str, float | None]:
    """Resolve a colour phrase to "#rrggbb" and an optional opacity hint.

    Args:
        text: A hex colour ("#ff0", "#ffd700", "ffd700"), an rgb() or rgba() call,
            or words: one known colour name with optional shade words ("light",
            "pale", "dark", "deep") and faint words ("shaded", "translucent"),
            joined or not ("light yellow", "lightyellow", "dark-blue"). Filler
            such as "color" or "fill" is ignored. Case does not matter.

    Returns:
        A pair: the lowercase "#rrggbb" string, and an opacity hint on the 0..1
        scale or None. The hint is LOW_OPACITY_HINT (0.15) when the phrase says
        "light", "pale" or "shaded", or the alpha channel of an rgba() call when
        one below 1 is given. "dark" changes the shade and gives no hint.

    Raises:
        ValueError: The text names no known colour, names more than one, or has
            an rgb channel outside 0..255. The message lists the known names.
    """
    raw = text.strip().lower()
    if not raw:
        raise ValueError("no colour given. " + _help())
    hex_match = _HEX_RE.match(raw)
    if hex_match:
        return _expand_hex(hex_match.group(1)), None
    rgb_match = _RGB_RE.match(raw)
    if rgb_match:
        return _from_rgb(rgb_match)
    return _from_words(raw, text)


def _help() -> str:
    """The tail of every error: what the caller could have said instead."""
    return (
        f"Known names: {', '.join(known_colour_names())}. "
        "Shade one with light, pale or dark, or pass #rrggbb or rgb(r, g, b)."
    )


def _expand_hex(token: str) -> str:
    """Normalise "#rgb", "#rrggbb" or "rrggbb" to lowercase "#rrggbb"."""
    digits = token.lstrip("#")
    if len(digits) == 3:
        digits = "".join(ch * 2 for ch in digits)
    return "#" + digits


def _from_rgb(match: re.Match[str]) -> tuple[str, float | None]:
    """Read an rgb() or rgba() call. An alpha below 1 becomes the opacity hint."""
    channels = [float(match.group(i)) for i in (1, 2, 3)]
    if any(c < 0 or c > 255 for c in channels):
        raise ValueError(f"rgb channels must be 0..255 in {match.group(0)}")
    hint: float | None = None
    alpha_text, percent = match.group(4), match.group(5)
    if alpha_text is not None:
        alpha = float(alpha_text) / (100.0 if percent else 1.0)
        if alpha < 0.0 or alpha > 1.0:
            raise ValueError(f"alpha must be 0..1 in {match.group(0)}")
        hint = alpha if alpha < 1.0 else None
    return _join([round(c) for c in channels]), hint


def _from_words(raw: str, text: str) -> tuple[str, float | None]:
    """Read a phrase: one base colour, any shade words, any faint words."""
    bases: list[str] = []
    shade = 0.0
    faint = False
    for word in _SPLIT_RE.split(raw):
        if not word or word in NOISE_WORDS:
            continue
        if word in FAINT_WORDS:
            faint = True
        if word in SHADES:
            shade += SHADES[word]
            continue
        if word in FAINT_WORDS:
            continue
        if word in BASE_COLOURS:
            bases.append(word)
            continue
        joined = _split_joined(word)
        if joined is None:
            raise ValueError(f'unknown colour word "{word}" in "{text}". ' + _help())
        prefix, base = joined
        if prefix in FAINT_WORDS:
            faint = True
        shade += SHADES[prefix]
        bases.append(base)
    if not bases:
        raise ValueError(f'no colour named in "{text}". ' + _help())
    if len({BASE_COLOURS[b] for b in bases}) > 1:
        raise ValueError(f'one colour at a time: "{text}" names {", ".join(bases)}.')
    shade = max(-_SHADE_LIMIT, min(_SHADE_LIMIT, shade))
    return _mix(BASE_COLOURS[bases[0]], shade), (LOW_OPACITY_HINT if faint else None)


def _split_joined(word: str) -> tuple[str, str] | None:
    """Split "lightyellow" into ("light", "yellow"); None when it is not that shape."""
    for prefix in _JOINED_PREFIXES:
        rest = word[len(prefix) :]
        if word.startswith(prefix) and rest in BASE_COLOURS:
            return prefix, rest
    return None


def _mix(hex_colour: str, amount: float) -> str:
    """Move a colour toward white (amount > 0) or black (amount < 0) by abs(amount)."""
    if amount == 0.0:
        return hex_colour
    target = 255 if amount > 0 else 0
    weight = abs(amount)
    channels = [int(hex_colour[i : i + 2], 16) for i in (1, 3, 5)]
    return _join([round(c + (target - c) * weight) for c in channels])


def _join(channels: list[int]) -> str:
    """Three 0..255 ints to lowercase "#rrggbb"."""
    return "#" + "".join(f"{c:02x}" for c in channels)
