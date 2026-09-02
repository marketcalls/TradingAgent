"""Colour helper tests.

The table is the contract the tool layer relies on: every hex form comes back as a
lowercase "#rrggbb", every base name resolves, and the light/pale/shaded rule yields
the 0.15 opacity hint while dark yields none. The luminance section walks every
base colour through its shades rather than spot-checking one, because the mixing
is arithmetic on three channels and a sign error would show on some hue and not
another.

Run:  python backend/tests/test_colours.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.charts.colours import (  # noqa: E402
    BASE_COLOURS,
    LOW_OPACITY_HINT,
    SHADES,
    colour_to_hex,
    known_colour_names,
)

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str]] = []

HEX_SHAPE = re.compile(r"^#[0-9a-f]{6}$")


def check(name: str, condition: bool, detail: str = "") -> None:
    status = PASS if condition else FAIL
    results.append((name, status))
    print(f"  [{status}] {name}" + (f" - {detail}" if detail else ""))


def luminance(hex_colour: str) -> float:
    r, g, b = (int(hex_colour[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def failure(text: str) -> str | None:
    """The ValueError message colour_to_hex raises for text, or None if it resolves."""
    try:
        colour_to_hex(text)
    except ValueError as exc:
        return str(exc)
    return None


def test_hex_forms() -> None:
    print("\n--- hex forms ---")
    table = {
        "#ff0": "#ffff00",
        "#FF0000": "#ff0000",
        "ff0000": "#ff0000",
        "#AbCdEf": "#abcdef",
        "  #4f8cff  ": "#4f8cff",
    }
    for text, want in table.items():
        got = colour_to_hex(text)
        check(f"hex {text.strip()!r}", got == (want, None), f"got {got}")
    check("bare 3-digit is not a colour", failure("f00") is not None)


def test_rgb_forms() -> None:
    print("\n--- rgb forms ---")
    check("rgb commas", colour_to_hex("rgb(255, 200, 0)") == ("#ffc800", None))
    check("rgb spaces", colour_to_hex("rgb(255 200 0)") == ("#ffc800", None))
    check("RGB upper case", colour_to_hex("RGB(0,0,255)") == ("#0000ff", None))
    check("rgba alpha is the hint", colour_to_hex("rgba(255,200,0,0.3)") == ("#ffc800", 0.3))
    check("rgba alpha 1 gives no hint", colour_to_hex("rgba(255,200,0,1)") == ("#ffc800", None))
    check("rgba percent alpha", colour_to_hex("rgba(255 200 0 / 25%)") == ("#ffc800", 0.25))
    check("channel above 255 refused", "0..255" in (failure("rgb(300,0,0)") or ""))
    check("alpha above 1 refused", "alpha" in (failure("rgba(0,0,0,2)") or ""))


def test_base_names() -> None:
    print("\n--- base names ---")
    wrong = [n for n, h in BASE_COLOURS.items() if colour_to_hex(n) != (h, None)]
    check("every base name resolves to its own hex with no hint", not wrong, ", ".join(wrong))
    names = known_colour_names()
    check("about thirty names", len(names) >= 30, str(len(names)))
    check("names are sorted", names == sorted(names))
    check("grey and gray agree", colour_to_hex("grey") == colour_to_hex("gray"))
    check(
        "case does not matter",
        colour_to_hex("Yellow") == colour_to_hex("YELLOW") == ("#ffff00", None),
    )


def test_shade_rule() -> None:
    print("\n--- light, pale, dark, shaded ---")
    yellow = colour_to_hex("yellow")[0]
    light_hex, light_hint = colour_to_hex("light yellow")
    pale_hex, pale_hint = colour_to_hex("pale yellow")
    dark_hex, dark_hint = colour_to_hex("dark yellow")
    shaded_hex, shaded_hint = colour_to_hex("shaded yellow")
    check("light gives the low opacity hint", light_hint == LOW_OPACITY_HINT, str(light_hint))
    check("pale gives the low opacity hint", pale_hint == LOW_OPACITY_HINT, str(pale_hint))
    check("shaded gives the low opacity hint", shaded_hint == LOW_OPACITY_HINT, str(shaded_hint))
    check("dark gives no hint", dark_hint is None, str(dark_hint))
    check("shaded keeps the hue unchanged", shaded_hex == yellow, shaded_hex)
    check("light is lighter than the base", luminance(light_hex) > luminance(yellow), light_hex)
    check("pale is lighter than light", luminance(pale_hex) > luminance(light_hex), pale_hex)
    check("dark is darker than the base", luminance(dark_hex) < luminance(yellow), dark_hex)
    check("deep reads as dark", colour_to_hex("deep yellow") == colour_to_hex("dark yellow"))
    check(
        "the user's own phrase",
        colour_to_hex("light shaded yellow color") == (light_hex, LOW_OPACITY_HINT),
    )
    check(
        "translucent is a hint without a shade",
        colour_to_hex("translucent blue") == ("#0000ff", LOW_OPACITY_HINT),
    )
    light_red = colour_to_hex("light red")[0]
    check("lightening keeps red's hue (g == b)", light_red[3:5] == light_red[5:7], light_red)
    dark_red = colour_to_hex("dark red")[0]
    check("darkening keeps red's hue (g == b == 0)", dark_red[3:7] == "0000", dark_red)
    piled = colour_to_hex("pale pale pale yellow")[0]
    check("piled shade words stop short of white", piled != "#ffffff", piled)


def test_joined_and_punctuated() -> None:
    print("\n--- joined and punctuated ---")
    check("lightyellow == light yellow", colour_to_hex("lightyellow") == colour_to_hex("light yellow"))
    check("darkblue == dark blue", colour_to_hex("darkblue") == colour_to_hex("dark blue"))
    check("paleblue == pale blue", colour_to_hex("paleblue") == colour_to_hex("pale blue"))
    check("Dark-Blue == dark blue", colour_to_hex("Dark-Blue") == colour_to_hex("dark blue"))
    check("dark_blue == dark blue", colour_to_hex("dark_blue") == colour_to_hex("dark blue"))
    check("lighter is not light + er", colour_to_hex("lighter blue") == colour_to_hex("light blue"))
    check("skyblue is its own name", colour_to_hex("skyblue") == colour_to_hex("sky"))


def test_noise_words() -> None:
    print("\n--- filler words ---")
    check("fill with a red colour", colour_to_hex("fill with a red colour") == ("#ff0000", None))
    check("bright green is green", colour_to_hex("bright green") == colour_to_hex("green"))
    check("a shade of blue is plain blue", colour_to_hex("a shade of blue") == ("#0000ff", None))


def test_unknown() -> None:
    print("\n--- unknown text ---")
    message = failure("chartreuse") or ""
    check("unknown name is refused", bool(message))
    check("message names the unknown word", '"chartreuse"' in message, message[:80])
    check("message lists the known names", all(n in message for n in ("yellow", "blue", "gold")))
    check("message says how to pass hex", "#rrggbb" in message)
    check("empty text is refused", "no colour" in (failure("   ") or ""))
    check("a shade word alone is refused", "no colour named" in (failure("light") or ""))
    check("two colours are refused", "one colour at a time" in (failure("red and blue") or ""))
    check("two spellings of one colour are fine", colour_to_hex("grey gray") == ("#808080", None))


def test_luminance_order() -> None:
    print("\n--- every base through its shades ---")
    broken: list[str] = []
    shapeless: list[str] = []
    for name in known_colour_names():
        base = colour_to_hex(name)[0]
        light = colour_to_hex(f"light {name}")[0]
        pale = colour_to_hex(f"pale {name}")[0]
        dark = colour_to_hex(f"dark {name}")[0]
        shapeless.extend(h for h in (base, light, pale, dark) if not HEX_SHAPE.match(h))
        if not luminance(pale) >= luminance(light) >= luminance(base) >= luminance(dark):
            broken.append(name)
    check("pale >= light >= base >= dark for every name", not broken, ", ".join(broken))
    check("every result is lowercase #rrggbb", not shapeless, ", ".join(shapeless))
    check("dark black is black", colour_to_hex("dark black")[0] == "#000000")
    check("pale white is white", colour_to_hex("pale white")[0] == "#ffffff")
    check("shade weights sit strictly inside -1..1", all(-1 < w < 1 and w != 0 for w in SHADES.values()))


def main() -> int:
    test_hex_forms()
    test_rgb_forms()
    test_base_names()
    test_shade_rule()
    test_joined_and_punctuated()
    test_noise_words()
    test_unknown()
    test_luminance_order()

    n_pass = sum(1 for _, s in results if s == PASS)
    n_fail = sum(1 for _, s in results if s == FAIL)
    print("\n=== Summary ===")
    for name, status in results:
        if status == FAIL:
            print(f"  FAILED: {name}")
    print(f"  {n_pass} passed, {n_fail} failed")
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
