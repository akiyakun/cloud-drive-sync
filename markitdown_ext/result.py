"""変換結果（MarkItDown の結果に、取り出した画像の一覧を足したもの）"""

from markitdown import DocumentConverterResult


class ConversionResult(DocumentConverterResult):
    """images: [(画像ファイル名, 画像データ), ...]

    Markdown の中の画像リンクは <images_dirname>/<画像ファイル名> を指す。
    画像を保存するのは呼び出す側の役目（MarkItDown の結果には画像を入れる場所がないため）。
    """

    def __init__(self, markdown, *, title=None, images=None):
        super().__init__(markdown=markdown, title=title)
        self.images = images or []
