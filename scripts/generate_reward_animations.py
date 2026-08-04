from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "app" / "static" / "animations" / "rewards"
TEAL = [0.192, 0.859, 0.773, 1]
BLUE = [0.125, 0.596, 0.827, 1]
GOLD = [0.965, 0.714, 0.196, 1]
CREAM = [0.984, 0.941, 0.824, 1]
DARK = [0.035, 0.071, 0.067, 1]
PINK = [0.918, 0.302, 0.502, 1]


def prop(value):
    return {"a": 0, "k": value}


def keyframes(first, middle, *, times=(0, 45, 90)):
    dimensions = len(first) if isinstance(first, list) else 1
    ease_in = {"x": [0.667] * dimensions, "y": [1] * dimensions}
    ease_out = {"x": [0.333] * dimensions, "y": [0] * dimensions}
    return {
        "a": 1,
        "k": [
            {"t": times[0], "s": first, "e": middle, "i": ease_in, "o": ease_out},
            {"t": times[1], "s": middle, "e": first, "i": ease_in, "o": ease_out},
            {"t": times[2], "s": first},
        ],
    }


def transform(position=(256, 256, 0), *, anchor=(0, 0, 0), scale=None, rotation=None, opacity=100):
    return {
        "o": prop(opacity),
        "r": rotation or prop(0),
        "p": prop(list(position)),
        "a": prop(list(anchor)),
        "s": scale or prop([100, 100, 100]),
    }


def ellipse(position, size, color, *, stroke=None, stroke_width=0):
    items = [
        {"ty": "el", "p": prop(list(position)), "s": prop(list(size)), "nm": "Ellipse"},
        {"ty": "fl", "c": prop(color), "o": prop(100), "r": 1, "nm": "Fill"},
    ]
    if stroke:
        items.append({"ty": "st", "c": prop(stroke), "o": prop(100), "w": prop(stroke_width), "lc": 2, "lj": 2, "nm": "Stroke"})
    items.append({"ty": "tr", "p": prop([0, 0]), "a": prop([0, 0]), "s": prop([100, 100]), "r": prop(0), "o": prop(100), "sk": prop(0), "sa": prop(0), "nm": "Transform"})
    return [{"ty": "gr", "it": items, "nm": "Ellipse group"}]


def rect(position, size, radius, color, *, stroke=None, stroke_width=0):
    items = [
        {"ty": "rc", "p": prop(list(position)), "s": prop(list(size)), "r": prop(radius), "nm": "Rectangle"},
        {"ty": "fl", "c": prop(color), "o": prop(100), "r": 1, "nm": "Fill"},
    ]
    if stroke:
        items.append({"ty": "st", "c": prop(stroke), "o": prop(100), "w": prop(stroke_width), "lc": 2, "lj": 2, "nm": "Stroke"})
    items.append({"ty": "tr", "p": prop([0, 0]), "a": prop([0, 0]), "s": prop([100, 100]), "r": prop(0), "o": prop(100), "sk": prop(0), "sa": prop(0), "nm": "Transform"})
    return [{"ty": "gr", "it": items, "nm": "Rectangle group"}]


def star(position, outer, inner, points, color, *, rotation=0):
    return [{
        "ty": "gr",
        "it": [
            {"ty": "sr", "sy": 1, "d": 1, "pt": prop(points), "p": prop(list(position)), "r": prop(rotation), "or": prop(outer), "os": prop(0), "ir": prop(inner), "is": prop(0), "nm": "Star"},
            {"ty": "fl", "c": prop(color), "o": prop(100), "r": 1, "nm": "Fill"},
            {"ty": "tr", "p": prop([0, 0]), "a": prop([0, 0]), "s": prop([100, 100]), "r": prop(0), "o": prop(100), "sk": prop(0), "sa": prop(0), "nm": "Transform"},
        ],
        "nm": "Star group",
    }]


def layer(name, shapes, *, position=(256, 256, 0), scale_pulse=True, rotation=False, index=1):
    scale = keyframes([96, 96, 100], [104, 104, 100]) if scale_pulse else prop([100, 100, 100])
    turn = keyframes([-3], [3]) if rotation else prop(0)
    return {
        "ddd": 0,
        "ind": index,
        "ty": 4,
        "nm": name,
        "sr": 1,
        "ks": transform(position, scale=scale, rotation=turn),
        "ao": 0,
        "shapes": shapes,
        "ip": 0,
        "op": 90,
        "st": 0,
        "bm": 0,
    }


def sparkle_layer(index=9):
    shapes = []
    for pos, radius, color in [((-150, -128), 20, GOLD), ((145, -90), 15, TEAL), ((126, 130), 12, CREAM), ((-132, 118), 10, BLUE)]:
        shapes.extend(star(pos, radius, radius * 0.25, 4, color, rotation=45))
    return layer("Sparkles", shapes, scale_pulse=True, rotation=True, index=index)


def animation(name, layers):
    return {
        "v": "5.12.2",
        "fr": 30,
        "ip": 0,
        "op": 90,
        "w": 512,
        "h": 512,
        "nm": name,
        "ddd": 0,
        "assets": [],
        "layers": layers,
        "markers": [],
    }


def casino_chips():
    shapes = ellipse((0, 0), (260, 260), TEAL, stroke=GOLD, stroke_width=18)
    shapes += ellipse((0, 0), (154, 154), DARK, stroke=CREAM, stroke_width=10)
    shapes += star((0, 0), 56, 25, 5, GOLD)
    for x, y in [(0, -108), (108, 0), (0, 108), (-108, 0)]:
        shapes += rect((x, y), (28, 50) if x == 0 else (50, 28), 8, CREAM)
    return animation("Poker Chips", [layer("Chip", shapes, rotation=True), sparkle_layer()])


def royal_cards():
    back = rect((0, 0), (190, 260), 24, TEAL, stroke=GOLD, stroke_width=10) + star((0, 0), 62, 26, 4, CREAM, rotation=45)
    front = rect((0, 0), (190, 260), 24, CREAM, stroke=GOLD, stroke_width=10) + star((0, 5), 52, 20, 4, PINK, rotation=45)
    return animation("Royal Cards", [layer("Back card", back, position=(205, 260, 0), rotation=True, index=1), layer("Front card", front, position=(305, 250, 0), rotation=True, index=2), sparkle_layer()])


def lucky_crown():
    shapes = rect((0, 82), (280, 70), 20, GOLD, stroke=CREAM, stroke_width=8)
    for x, y, size in [(-105, -10, 78), (0, -72, 94), (105, -10, 78)]:
        shapes += star((x, y), size, size * 0.48, 4, GOLD, rotation=45)
        shapes += ellipse((x, y - 22), (28, 28), TEAL)
    return animation("Lucky Crown", [layer("Crown", shapes, rotation=True), sparkle_layer()])


def champion_cup():
    shapes = ellipse((0, -50), (210, 150), GOLD, stroke=CREAM, stroke_width=10)
    shapes += rect((0, 50), (48, 125), 18, GOLD)
    shapes += rect((0, 124), (180, 42), 18, GOLD, stroke=CREAM, stroke_width=8)
    shapes += ellipse((-118, -42), (92, 112), [0, 0, 0, 0], stroke=GOLD, stroke_width=20)
    shapes += ellipse((118, -42), (92, 112), [0, 0, 0, 0], stroke=GOLD, stroke_width=20)
    shapes += star((0, -48), 48, 22, 5, TEAL)
    return animation("Champion Cup", [layer("Cup", shapes), sparkle_layer()])


def winner_badge():
    shapes = rect((-48, 115), (72, 150), 14, BLUE) + rect((48, 115), (72, 150), 14, TEAL)
    shapes += ellipse((0, -20), (250, 250), GOLD, stroke=CREAM, stroke_width=12)
    shapes += star((0, -20), 76, 34, 5, TEAL)
    return animation("Winner Badge", [layer("Badge", shapes, rotation=True), sparkle_layer()])


def premium_gem():
    shapes = star((0, 0), 145, 92, 4, BLUE, rotation=45)
    shapes += star((0, -10), 95, 35, 4, TEAL, rotation=45)
    shapes += star((-40, -50), 42, 12, 4, CREAM, rotation=45)
    return animation("Premium Gem", [layer("Gem", shapes, rotation=True), sparkle_layer()])


def laurel_star():
    shapes = ellipse((0, 0), (265, 265), [0, 0, 0, 0], stroke=GOLD, stroke_width=18)
    for side in (-1, 1):
        for y in (-95, -45, 5, 55, 105):
            x = side * (115 - abs(y) * 0.2)
            shapes += ellipse((x, y), (34, 66), TEAL)
    shapes += star((0, 0), 84, 36, 5, GOLD)
    return animation("Laurel Star", [layer("Laurel", shapes), sparkle_layer()])


def jackcoin_stack():
    shapes = []
    for y, width in [(100, 240), (52, 240), (4, 240), (-44, 210), (-92, 176)]:
        shapes += ellipse((0, y), (width, 68), GOLD, stroke=CREAM, stroke_width=8)
    shapes += star((0, -92), 42, 18, 5, TEAL)
    return animation("JACKCOIN Stack", [layer("Coins", shapes), sparkle_layer()])


def coffee_cup():
    shapes = rect((-20, 35), (230, 185), 34, CREAM, stroke=GOLD, stroke_width=10)
    shapes += ellipse((112, 22), (100, 110), [0, 0, 0, 0], stroke=CREAM, stroke_width=25)
    shapes += ellipse((-20, -52), (205, 48), DARK, stroke=GOLD, stroke_width=8)
    shapes += ellipse((-58, -132), (24, 76), TEAL) + ellipse((0, -150), (24, 88), TEAL) + ellipse((58, -132), (24, 76), TEAL)
    return animation("Coffee Cup", [layer("Cup", shapes), sparkle_layer()])


def club_cocktail():
    shapes = ellipse((0, -48), (250, 150), PINK, stroke=CREAM, stroke_width=10)
    shapes += rect((0, 62), (28, 150), 12, GOLD)
    shapes += ellipse((0, 138), (150, 35), GOLD)
    shapes += ellipse((78, -108), (52, 52), TEAL, stroke=CREAM, stroke_width=7)
    shapes += rect((104, -44), (18, 150), 9, CREAM)
    return animation("Club Cocktail", [layer("Cocktail", shapes, rotation=True), sparkle_layer()])


ANIMATIONS = {
    "casino_chips": casino_chips,
    "royal_cards": royal_cards,
    "lucky_crown": lucky_crown,
    "champion_cup": champion_cup,
    "winner_badge": winner_badge,
    "premium_gem": premium_gem,
    "laurel_star": laurel_star,
    "jackcoin_stack": jackcoin_stack,
    "coffee_cup": coffee_cup,
    "club_cocktail": club_cocktail,
}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for key, factory in ANIMATIONS.items():
        (OUT / f"{key}.json").write_text(
            json.dumps(factory(), ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
