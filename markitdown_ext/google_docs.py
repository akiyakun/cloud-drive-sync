"""Google ドキュメントの HTML 書き出し（application/zip）を Markdown に変換する

Google の Markdown 書き出しでは消えてしまう情報を残すため、HTML 書き出しから自分で Markdown を作る。
- 図形描画（HTML では PNG 画像になっている）と画像を <名前>.images/ に取り出す
- 文字の色は〔文字色A:その文字〕、ハイライトは〔色A:その文字〕、下線は <u>…</u> で残す
- 表のセルの背景色は〔色A〕、結合は〔結合 横2×縦1〕の印で残す
- チェックリストは HTML では普通の箇条書きになるため、Google の Markdown 書き出しから「- [ ]」を補う

MarkItDown 用の変換部品（GoogleDocsZipConverter）として使う。
"""

import io
import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlparse

from markitdown import DocumentConverter, StreamInfo

from .colors import ColorLabels, is_notable_text_color, normalize
from .result import ConversionResult

# sync.py が Google ドキュメントの HTML 書き出しを渡すときに付ける種類名（独自）
GOOGLE_DOCS_ZIP_MIME = "application/vnd.google-apps.document+zip"

VOID_TAGS = {"img", "br", "hr", "meta", "link", "input", "col", "area", "base", "wbr", "source"}
LIST_CLASS = re.compile(r"lst-kix_([\w-]+?)-(\d+)\b")
CHECKLIST_LINE = re.compile(r"^\s*[-*+] \[( |x|X)\] (.*)$")


# --- HTML を木の形に読み込む -----------------------------------------------------

@dataclass
class Node:
    tag: str
    attrs: dict
    children: list = field(default_factory=list)

    def classes(self):
        return self.attrs.get("class", "").split()

    def text(self):
        return "".join(c if isinstance(c, str) else c.text() for c in self.children)


class _TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("root", {})
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: v or "" for k, v in attrs})
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(Node(tag, {k: v or "" for k, v in attrs}))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def _parse_html(html):
    builder = _TreeBuilder()
    builder.feed(html)
    return builder.root


def _find(node, tag):
    for child in node.children:
        if isinstance(child, Node):
            if child.tag == tag:
                return child
            found = _find(child, tag)
            if found is not None:
                return found
    return None


def _style(node):
    """style 属性を {名前: 値} にする"""
    result = {}
    for decl in node.attrs.get("style", "").split(";"):
        if ":" in decl:
            key, value = decl.split(":", 1)
            result[key.strip().lower()] = value.strip().lower()
    return result


# --- 文字の書式 -------------------------------------------------------------

@dataclass(frozen=True)
class Fmt:
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strike: bool = False
    color: str | None = None
    highlight: str | None = None
    link: str | None = None


def _span_fmt(node, base):
    s = _style(node)
    fmt = dict(base.__dict__)
    weight = s.get("font-weight")
    if weight:
        fmt["bold"] = weight == "bold" or (weight.isdigit() and int(weight) >= 600)
    if "font-style" in s:
        fmt["italic"] = s["font-style"] == "italic"
    if "text-decoration" in s:
        fmt["underline"] = "underline" in s["text-decoration"]
        fmt["strike"] = "line-through" in s["text-decoration"]
    if "color" in s:
        color = normalize(s["color"])
        fmt["color"] = color if color and is_notable_text_color(color) else None
    if "background-color" in s:
        bg = normalize(s["background-color"])
        fmt["highlight"] = bg if bg and bg != "#FFFFFF" else None
    return Fmt(**fmt)


def _unwrap_link(href):
    """Google が付ける https://www.google.com/url?q=… の中継を外して、本来のリンク先にする"""
    parsed = urlparse(href)
    if parsed.netloc in ("www.google.com", "google.com") and parsed.path == "/url":
        q = parse_qs(parsed.query).get("q")
        if q:
            return q[0]
    return href


class Converter:
    def __init__(self, images_dirname, checklist):
        self.images_dirname = images_dirname
        self.checklist = checklist  # 正規化した文字 → チェック済みか
        self.labels = ColorLabels()
        self.font_counts = {}
        self.highlight_counts = {}
        self.cell_counts = {}
        self.images = []  # ZIP 内のパス
        self.footnotes = []  # (番号, 本文)
        self.comments = []  # (記号, 本文)
        self.list_counters = {}

    def _count(self, counts, color):
        counts[color] = counts.get(color, 0) + 1
        return self.labels.label(color)

    # --- 段落の中身（インライン） ---

    def _runs(self, node, fmt, runs, in_heading):
        for child in node.children:
            if isinstance(child, str):
                runs.append((child.replace("\n", " "), fmt))
                continue
            tag = child.tag
            if tag == "br":
                runs.append(("\n", Fmt()))
            elif tag == "img":
                src = child.attrs.get("src", "")
                name = posixpath.basename(src)
                if src and not src.startswith(("http:", "https:", "data:")):
                    self.images.append(posixpath.normpath(src))
                    runs.append((f"![](<{self.images_dirname}/{name}>)", None))
                elif src.startswith(("http:", "https:")):
                    runs.append((f"![](<{src}>)", None))
            elif tag == "a":
                aid, href = child.attrs.get("id", ""), child.attrs.get("href", "")
                if aid.startswith("ftnt_ref"):
                    runs.append((f"[^{aid[len('ftnt_ref'):]}]", None))
                elif aid.startswith("cmnt_ref"):
                    runs.append((f"〔コメント {child.text().strip('[]')}〕", None))
                elif aid.startswith(("ftnt", "cmnt")):
                    continue  # 脚注・コメント本文の先頭の番号
                elif href and not href.startswith("#"):
                    self._runs(child, Fmt(**{**_span_fmt(child, fmt).__dict__, "link": _unwrap_link(href)}),
                               runs, in_heading)
                else:
                    self._runs(child, fmt, runs, in_heading)
            elif tag in ("span", "sup", "sub", "b", "strong", "i", "em", "u", "s", "font"):
                sub = _span_fmt(child, fmt)
                if tag in ("b", "strong"):
                    sub = Fmt(**{**sub.__dict__, "bold": True})
                elif tag in ("i", "em"):
                    sub = Fmt(**{**sub.__dict__, "italic": True})
                self._runs(child, sub, runs, in_heading)
            else:
                self._runs(child, fmt, runs, in_heading)

    def _render_run(self, text, fmt, in_heading):
        if fmt is None:  # 画像・脚注などの、そのまま出す部分
            return text
        core = text.strip()
        if not core:
            return text
        lead, trail = text[:len(text) - len(text.lstrip())], text[len(text.rstrip()):]
        if fmt.strike:
            core = f"~~{core}~~"
        if fmt.italic:
            core = f"*{core}*"
        if fmt.bold and not in_heading:
            core = f"**{core}**"
        if fmt.link:
            core = f"[{core}](<{fmt.link}>)"  # リンクの青い文字や下線は印を付けない
        else:
            if fmt.underline:
                core = f"<u>{core}</u>"
            if fmt.color:
                core = f"〔文字色{self._count(self.font_counts, fmt.color)}:{core}〕"
        if fmt.highlight:
            core = f"〔色{self._count(self.highlight_counts, fmt.highlight)}:{core}〕"
        return lead + core + trail

    def inline(self, node, in_heading=False):
        runs = []
        self._runs(node, Fmt(), runs, in_heading)
        merged = []  # 同じ書式が続く部分はまとめる（**a****b** のようにならないように）
        for text, fmt in runs:
            if merged and fmt is not None and merged[-1][1] == fmt and "\n" not in text:
                merged[-1] = (merged[-1][0] + text, fmt)
            else:
                merged.append((text, fmt))
        return "".join(self._render_run(t, f, in_heading) for t, f in merged).strip()

    # --- ブロック ---

    def blocks(self, node, out):
        """段落・見出し・リスト・表などを ("種類", 文字) の並びにする"""
        for child in node.children:
            if isinstance(child, str):
                if child.strip():
                    out.append(("p", child.strip()))
                continue
            tag = child.tag
            if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
                text = self.inline(child, in_heading=True)
                if text:
                    out.append(("p", "#" * int(tag[1]) + " " + text))
            elif tag == "p":
                self._paragraph(child, out)
            elif tag in ("ul", "ol"):
                self._list(child, out)
            elif tag == "table":
                out.append(("p", self._table(child)))
            elif tag == "hr":
                if "none" not in _style(child).get("display", ""):  # 改ページは出さない
                    out.append(("p", "---"))
            elif tag in ("style", "script", "head", "title", "meta"):
                continue
            else:
                self.blocks(child, out)

    def _paragraph(self, node, out):
        anchor = _find(node, "a")
        aid = anchor.attrs.get("id", "") if anchor is not None else ""
        if aid.startswith("ftnt") and not aid.startswith("ftnt_ref"):
            self.footnotes.append((aid[len("ftnt"):], self.inline(node)))
            return
        if aid.startswith("cmnt") and not aid.startswith("cmnt_ref"):
            self.comments.append((anchor.text().strip("[]"), self.inline(node)))
            return
        if "title" in node.classes():  # 「タイトル」の書式と、タブの名前
            text = self.inline(node, in_heading=True)
            if text:
                out.append(("p", f"# {text}"))
            return
        text = self.inline(node)
        if text:
            # 段落内の改行は <br> で表す（MarkItDown は行末の空白を削るので「空白 2 つ」の改行は使えない）
            out.append(("p", text.replace("\n", "<br>\n")))

    def _list(self, node, out):
        m = next((LIST_CLASS.search(c) for c in node.classes() if LIST_CLASS.search(c)), None)
        list_id, level = (m.group(1), int(m.group(2))) if m else ("", 0)
        ordered = node.tag == "ol"
        # 上の階層の項目が来たら、下の階層の番号を振り直す
        for key in [k for k in self.list_counters if k[0] == list_id and k[1] > level]:
            del self.list_counters[key]
        if ordered and node.attrs.get("start"):
            self.list_counters[(list_id, level)] = int(node.attrs["start"]) - 1
        for li in (c for c in node.children if isinstance(c, Node) and c.tag == "li"):
            text = self.inline(li)
            if ordered:
                n = self.list_counters.get((list_id, level), 0) + 1
                self.list_counters[(list_id, level)] = n
                marker = f"{n}."
            else:
                state = self.checklist.get(_normalize_text(li.text()))
                marker = "-" if state is None else ("- [x]" if state else "- [ ]")
            out.append(("li", "    " * level + f"{marker} {text}"))

    def _table(self, node):
        rows = []
        occupied = {}  # (行, 列) → 上のセルの縦結合で埋まっている
        for r, tr in enumerate(_all(node, "tr")):
            cells, c = [], 0
            for td in (x for x in tr.children if isinstance(x, Node) and x.tag in ("td", "th")):
                while occupied.get((r, c)):
                    cells.append("")
                    c += 1
                colspan = int(td.attrs.get("colspan", "1") or 1)
                rowspan = int(td.attrs.get("rowspan", "1") or 1)
                parts = []
                inner = []
                self.blocks(td, inner)
                text = "<br>".join(t for _, t in inner).replace("<br>\n", "<br>").replace("|", "\\|")
                parts.append(text)
                bg = normalize(_style(td).get("background-color", ""))
                if bg and bg != "#FFFFFF":
                    parts.append(f"〔色{self._count(self.cell_counts, bg)}〕")
                if colspan > 1 or rowspan > 1:
                    parts.append(f"〔結合 横{colspan}×縦{rowspan}〕")
                cells.append(" ".join(p for p in parts if p))
                for dr in range(rowspan):
                    for dc in range(colspan):
                        if dr or dc:
                            occupied[(r + dr, c + dc)] = True
                cells.extend([""] * (colspan - 1))
                c += colspan
            while occupied.get((r, c)):
                cells.append("")
                c += 1
            rows.append(cells)
        width = max((len(r) for r in rows), default=0)
        if not width:
            return ""
        rows = [r + [""] * (width - len(r)) for r in rows]
        lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
        lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
        return "\n".join(lines)

    def legend(self):
        colors = sorted(set(self.font_counts) | set(self.highlight_counts) | set(self.cell_counts),
                        key=self.labels.sort_key)
        if not colors:
            return []
        lines = ["色の対応表（〔文字色A:…〕= 文字の色、〔色A:…〕= ハイライト、表の〔色A〕= セルの背景色）:", ""]
        for color in colors:
            usage = []
            if self.font_counts.get(color):
                usage.append(f"文字 {self.font_counts[color]} か所")
            if self.highlight_counts.get(color):
                usage.append(f"ハイライト {self.highlight_counts[color]} か所")
            if self.cell_counts.get(color):
                usage.append(f"表のセル {self.cell_counts[color]} 個")
            lines.append(self.labels.legend_line(color, "、".join(usage)))
        return lines


def _all(node, tag):
    for child in node.children:
        if isinstance(child, Node):
            if child.tag == tag:
                yield child
            else:
                yield from _all(child, tag)


def _normalize_text(text):
    """チェックリストの項目を突き合わせるため、記号や空白を除いた文字にする"""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # Markdown のリンク
    return re.sub(r"[\s*_~`\\\[\]()<>#]", "", text)


def checklist_from_markdown(md):
    """Google の Markdown 書き出しから、チェックリストの項目と状態を読み取る"""
    result = {}
    for line in md.splitlines():
        m = CHECKLIST_LINE.match(line)
        if m:
            result[_normalize_text(m.group(2))] = m.group(1).lower() == "x"
    return result


def docs_zip_to_markdown(zip_data, title, images_dirname, markdown_export=None):
    """HTML 書き出しの ZIP を Markdown に変換する

    markdown_export: Google の Markdown 書き出し（チェックリストを補うため。無くても変換はできる）
    戻り値: (Markdown 文字列, [(画像ファイル名, 画像データ), ...])
    """
    zf = zipfile.ZipFile(io.BytesIO(zip_data))
    html_name = next(n for n in zf.namelist() if n.endswith(".html"))
    root = _parse_html(zf.read(html_name).decode("utf-8"))
    body = _find(root, "body") or root

    checklist = checklist_from_markdown(markdown_export) if markdown_export else {}
    conv = Converter(images_dirname, checklist)
    blocks = []
    conv.blocks(body, blocks)

    while blocks and blocks[-1] == ("p", "---"):  # 脚注の前の区切り線は飾りなので出さない
        blocks.pop()
    body_lines = []
    for i, (kind, text) in enumerate(blocks):
        if i:
            # 箇条書きの項目どうしは空行をはさまない
            body_lines.append("\n" if kind == "li" and blocks[i - 1][0] == "li" else "\n\n")
        body_lines.append(text)

    parts = [
        f"> 「{title}」: Google ドキュメントの HTML 書き出しから自動生成。"
        "文字の色は〔文字色A:…〕、ハイライトは〔色A:…〕、表のセルの背景色は〔色A〕、結合は〔結合 横2×縦1〕で示す"
        "（同じアルファベットはファイル内で同じ色）。下線は <u>…</u>。",
        "",
    ]
    legend = conv.legend()
    if legend:
        parts += legend + [""]
    parts.append("".join(body_lines))
    if conv.footnotes:
        parts += ["", "\n".join(f"[^{n}]: {text}" for n, text in conv.footnotes)]
    if conv.comments:
        parts += ["", "## コメント", "", "\n".join(f"- {mark}: {text}" for mark, text in conv.comments)]

    names = set(zf.namelist())
    images = []
    for path in dict.fromkeys(conv.images):  # 同じ画像は 1 回だけ
        if path in names:
            images.append((posixpath.basename(path), zf.read(path)))
    return "\n".join(parts) + "\n", images


def _looks_like_google_docs_zip(file_stream):
    """Google ドキュメントの「ウェブページ（.html、zip）」書き出しの形か（中身を読んだら位置を元に戻す）"""
    position = file_stream.tell()
    try:
        zf = zipfile.ZipFile(file_stream)
        names = zf.namelist()
        htmls = [n for n in names if n.endswith(".html")]
        if len(htmls) != 1 or "/" in htmls[0]:
            return False
        if any(n != htmls[0] and not n.startswith("images/") for n in names):
            return False
        return b"doc-content" in zf.read(htmls[0])[:200_000]
    except zipfile.BadZipFile:
        return False
    finally:
        file_stream.seek(position)


class GoogleDocsZipConverter(DocumentConverter):
    """MarkItDown 用の Google ドキュメント変換部品（HTML 書き出しの zip を受け付ける）

    convert() に渡せる追加の引数:
        title: 冒頭の説明に入れる名前（省略時はファイル名）
        images_dirname: 画像リンクの置き場所（省略時は「<title>.images」）
        markdown_export: Google の Markdown 書き出し（チェックリストを補うため。省略可）
    """

    def accepts(self, file_stream, stream_info: StreamInfo, **kwargs):
        if (stream_info.mimetype or "") == GOOGLE_DOCS_ZIP_MIME:
            return True
        if (stream_info.extension or "").lower() == ".zip":
            return _looks_like_google_docs_zip(file_stream)
        return False

    def convert(self, file_stream, stream_info: StreamInfo, **kwargs):
        title = kwargs.get("title") or posixpath.splitext(stream_info.filename or "document")[0]
        images_dirname = kwargs.get("images_dirname") or f"{title}.images"
        markdown, images = docs_zip_to_markdown(
            file_stream.read(), title, images_dirname, kwargs.get("markdown_export")
        )
        return ConversionResult(markdown, title=title, images=images)
