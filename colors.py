"""色を Markdown で表すための共通の部品（ドキュメントとスプレッドシートの両方で使う）

色は〔色A〕〔文字色B〕のような記号で表し、ファイルごとに「記号 → 色」の対応表を付ける。
"""

import colorsys

from openpyxl.utils import get_column_letter


def normalize(color):
    """'#ff0000' / 'FFFF0000'（ARGB）などを '#FF0000' にそろえる。読めなければ None"""
    if not isinstance(color, str):
        return None
    color = color.strip().lstrip("#")
    if len(color) == 8:  # ARGB
        color = color[2:]
    if len(color) == 3:  # #f00 の短縮形
        color = "".join(c * 2 for c in color)
    if len(color) != 6:
        return None
    try:
        int(color, 16)
    except ValueError:
        return None
    return f"#{color.upper()}"


def _hls(color):
    r, g, b = (int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return colorsys.rgb_to_hls(r, g, b)


def is_notable_text_color(color):
    """目立たせるために付けた文字色か（黒・灰・白は普通の文字色とみなして False）"""
    _, light, sat = _hls(color)
    return sat >= 0.15 and 0.12 < light < 0.95


def color_name(color):
    """#RRGGBB をおおまかな日本語の色名にする（AI と人が色の見当をつけやすくするため）"""
    hue, light, sat = _hls(color)
    if sat < 0.15 or light > 0.97:
        for limit, name in ((0.9, "白に近い灰"), (0.75, "薄い灰"), (0.4, "灰"), (0.12, "濃い灰")):
            if light > limit:
                return name
        return "黒"
    degree = hue * 360
    for limit, name in ((15, "赤"), (40, "橙"), (70, "黄"), (160, "緑"), (200, "水色"),
                        (260, "青"), (300, "紫"), (345, "桃"), (361, "赤")):
        if degree < limit:
            base = name
            break
    if light >= 0.78:
        return f"薄い{base}"
    if light < 0.3:
        return f"濃い{base}"
    return base


class ColorLabels:
    """色にアルファベットの記号（A, B, ..., Z, AA, ...）を割り当てる。同じ色には同じ記号"""

    def __init__(self):
        self._labels = {}

    def __contains__(self, color):
        return color in self._labels

    def label(self, color):
        if color not in self._labels:
            self._labels[color] = get_column_letter(len(self._labels) + 1)
        return self._labels[color]

    def sort_key(self, color):
        """記号の順（A, B, ..., Z, AA）に並べるためのキー"""
        label = self.label(color)
        return (len(label), label)

    def legend_line(self, color, usage):
        return f"- {self.label(color)}: {color}（{color_name(color)}）… {usage}"
