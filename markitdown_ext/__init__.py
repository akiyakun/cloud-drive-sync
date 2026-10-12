"""MarkItDown の拡張（プラグイン）

MarkItDown（https://github.com/microsoft/markitdown）を土台にし、MarkItDown 標準の変換では
消えてしまう情報（色・結合・図形描画・グラフ・コメントなど）を残すための変換部品を差し込む。

- XlsxConverter: .xlsx（MarkItDown 標準の XLSX 変換の代わり）
- GoogleDocsZipConverter: Google ドキュメントの HTML 書き出し（zip）

使い方:
    from markitdown_ext import create_markitdown
    md = create_markitdown()
    result = md.convert("資料.xlsx")
    result.markdown  # Markdown
    result.images    # [(画像ファイル名, 画像データ), ...]（保存は呼び出す側で行う）

MarkItDown のプラグインの決まり（__plugin_interface_version__ と register_converters）にも沿っているので、
パッケージとしてインストールして entry point（markitdown.plugin）に登録すれば、
MarkItDown の enable_plugins=True からも使える。
"""

from markitdown import MarkItDown

from .google_docs import GOOGLE_DOCS_ZIP_MIME, GoogleDocsZipConverter
from .result import ConversionResult
from .xlsx import XlsxConverter

__plugin_interface_version__ = 1

# 変換結果が変わる修正をしたら 1 つ上げる。sync.py は版が変わると、Drive 側が変わっていなくても全部作り直す
CONVERTER_VERSION = 1

__all__ = [
    "CONVERTER_VERSION",
    "ConversionResult",
    "GOOGLE_DOCS_ZIP_MIME",
    "GoogleDocsZipConverter",
    "XlsxConverter",
    "create_markitdown",
    "register_converters",
]


def register_converters(markitdown: MarkItDown, **kwargs):
    """MarkItDown に変換部品を登録する

    同じ優先度なら後から登録したものが先に試されるので、MarkItDown 標準の変換より先に使われる。
    """
    markitdown.register_converter(XlsxConverter())
    markitdown.register_converter(GoogleDocsZipConverter())


def create_markitdown(**kwargs) -> MarkItDown:
    """この拡張を登録済みの MarkItDown を作る（kwargs は MarkItDown にそのまま渡す）"""
    markitdown = MarkItDown(**kwargs)
    register_converters(markitdown)
    return markitdown
