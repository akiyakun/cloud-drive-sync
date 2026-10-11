"""開発用: 書き出し形式の検証に使うテストデータを Drive に作る（中身はすべてダミー）

Drive に「cloud-drive-sync-test」フォルダを作り、その中に次の 2 つを作る。
- テスト用ドキュメント: 文字の装飾、チェックリスト、番号付きリスト、脚注、リンク、表（セルの色・結合）、コメント
- テスト用スプレッドシート: 背景色・文字色・取り消し線、セル内の一部だけの色、結合、チェックボックス、
  プルダウン、数式、メモ、リンク、IMAGE 関数、条件付き書式、グラフ、非表示シート、コメント

API では作れないもの（図形描画、画像の自由な配置など）は、手で足す。
手で足したファイルはこのアプリからは見えないため、このスクリプトを再実行しても消えない。

再実行すると、前回このスクリプトが作ったファイルはゴミ箱へ移してから作り直す（30 日以内なら元に戻せる）。

使い方（プロジェクトのフォルダで。先に python dev/auth_dev.py で認証しておく）:
    python dev/make_test_data.py
"""

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
TOKEN_FILE = "token.dev.json"

FOLDER_NAME = "cloud-drive-sync-test"
FOLDER_MIME = "application/vnd.google-apps.folder"
DOC_MIME = "application/vnd.google-apps.document"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"

# Google のロゴ（IMAGE 関数のテスト用。Google 側が取得する公開画像）
SAMPLE_IMAGE_URL = "https://www.google.com/images/branding/googlelogo/2x/googlelogo_color_272x92dp.png"


def rgb(hex_color):
    """#RRGGBB を API の色の形 {red, green, blue}（0〜1）にする"""
    return {key: int(hex_color[i:i + 2], 16) / 255 for key, i in (("red", 1), ("green", 3), ("blue", 5))}


def utf16_len(text):
    """ドキュメントの位置は UTF-16 の文字数で数えるため"""
    return len(text.encode("utf-16-le")) // 2


# --- フォルダ ---------------------------------------------------------------

def prepare_folder(drive):
    """テスト用フォルダを用意し、前回このスクリプトが作ったファイルをゴミ箱へ移す"""
    found = drive.files().list(
        q=f"name = '{FOLDER_NAME}' and mimeType = '{FOLDER_MIME}' and trashed = false",
        fields="files(id)",
    ).execute()["files"]
    if not found:
        return drive.files().create(
            body={"name": FOLDER_NAME, "mimeType": FOLDER_MIME}, fields="id"
        ).execute()["id"]

    folder_id = found[0]["id"]
    # drive.file 権限では、このアプリが作ったファイルだけが見える（手で作ったファイルは対象外）
    old_files = drive.files().list(
        q=f"'{folder_id}' in parents and trashed = false", fields="files(id, name)"
    ).execute()["files"]
    for f in old_files:
        drive.files().update(fileId=f["id"], body={"trashed": True}).execute()
        print(f"  前回のテストデータをゴミ箱へ移動: {f['name']}")
    return folder_id


def create_file(drive, name, mime, folder_id):
    return drive.files().create(
        body={"name": name, "mimeType": mime, "parents": [folder_id]}, fields="id, webViewLink"
    ).execute()


# --- ドキュメント -------------------------------------------------------------

class DocWriter:
    """本文の末尾に文字を足していき、書式の指定をまとめて作る"""

    def __init__(self):
        self.index = 1  # 本文は位置 1 から始まる
        self.inserts = []
        self.formats = []

    def text(self, text, **style):
        start = self.index
        self.inserts.append({"insertText": {"location": {"index": start}, "text": text}})
        self.index += utf16_len(text)
        if style:
            self.formats.append({"updateTextStyle": {
                "range": {"startIndex": start, "endIndex": self.index},
                "textStyle": style,
                "fields": ",".join(style),
            }})
        return start, self.index

    def paragraph(self, text="", heading=None):
        start, end = self.text(text + "\n")
        if heading:
            self.formats.append({"updateParagraphStyle": {
                "range": {"startIndex": start, "endIndex": end},
                "paragraphStyle": {"namedStyleType": heading},
                "fields": "namedStyleType",
            }})
        return start, end

    def bullets(self, items, preset):
        start = self.index
        for item in items:
            self.paragraph(item)
        self.formats.append({"createParagraphBullets": {
            "range": {"startIndex": start, "endIndex": self.index},
            "bulletPreset": preset,
        }})


def color_style(hex_color):
    return {"color": {"rgbColor": rgb(hex_color)}}


def build_document(docs, drive, doc_id):
    w = DocWriter()
    w.paragraph("テスト用ドキュメント", heading="HEADING_1")
    w.paragraph("このドキュメントは cloud-drive-sync の書き出し検証用のダミーです。")

    w.paragraph("文字の装飾", heading="HEADING_2")
    w.text("普通の文字、")
    w.text("赤い文字", foregroundColor=color_style("#FF0000"))
    w.text("、")
    w.text("黄色のハイライト", backgroundColor=color_style("#FFFF00"))
    w.text("、")
    w.text("太字", bold=True)
    w.text("、")
    w.text("斜体", italic=True)
    w.text("、")
    w.text("下線", underline=True)
    w.text("、")
    w.text("取り消し線", strikethrough=True)
    w.text("、")
    w.text("リンク", link={"url": "https://example.com/"})
    w.paragraph("。")

    w.paragraph("リスト", heading="HEADING_2")
    w.paragraph("チェックリスト:")
    w.bullets(["項目その1", "項目その2", "項目その3"], "BULLET_CHECKBOX")
    w.paragraph("番号付きリスト:")
    w.bullets(["手順その1", "手順その2", "手順その3"], "NUMBERED_DECIMAL_ALPHA_ROMAN")

    w.paragraph("脚注", heading="HEADING_2")
    w.text("この文の最後に脚注があります")
    footnote_at = w.index  # 「。」の直前に脚注を付ける
    w.paragraph("。")

    w.paragraph("表", heading="HEADING_2")
    w.paragraph("下の表には、セルの背景色と結合があります。")

    docs.documents().batchUpdate(documentId=doc_id, body={"requests": w.inserts + w.formats}).execute()

    # 表は末尾に追加してから、セルの位置を読み直して中身を入れる
    docs.documents().batchUpdate(documentId=doc_id, body={"requests": [
        {"insertTable": {"rows": 3, "columns": 3, "endOfSegmentLocation": {"segmentId": ""}}},
    ]}).execute()
    fill_table(docs, doc_id)

    # 脚注は本文の文字位置をずらすので最後に作る（表より前の位置なので、表の位置には影響しない）
    reply = docs.documents().batchUpdate(documentId=doc_id, body={"requests": [
        {"createFootnote": {"location": {"index": footnote_at}}},
    ]}).execute()
    footnote_id = reply["replies"][0]["createFootnote"]["footnoteId"]
    content = docs.documents().get(documentId=doc_id).execute()["footnotes"][footnote_id]["content"]
    docs.documents().batchUpdate(documentId=doc_id, body={"requests": [
        {"insertText": {
            "location": {"segmentId": footnote_id, "index": content[-1]["endIndex"] - 1},
            "text": "これは脚注の本文です。",
        }},
    ]}).execute()

    drive.comments().create(
        fileId=doc_id, body={"content": "これはテスト用のコメントです。"}, fields="id"
    ).execute()


def fill_table(docs, doc_id):
    cells = [
        ["結合した見出し", "", "見出し3"],
        ["薄い赤の背景", "普通", "薄い青の背景"],
        ["1", "2", "3"],
    ]
    body = docs.documents().get(documentId=doc_id).execute()["body"]["content"]
    table_element = [e for e in body if "table" in e][-1]
    table_start = table_element["startIndex"]

    # 後ろのセルから入れると、前のセルの位置がずれない
    inserts = []
    for r, row in enumerate(table_element["table"]["tableRows"]):
        for c, cell in enumerate(row["tableCells"]):
            if cells[r][c]:
                inserts.append((cell["content"][0]["startIndex"], cells[r][c]))
    requests = [
        {"insertText": {"location": {"index": index}, "text": text}}
        for index, text in sorted(inserts, reverse=True)
    ]

    def cell_range(row, col, row_span=1, col_span=1):
        return {
            "tableCellLocation": {"tableStartLocation": {"index": table_start}, "rowIndex": row, "columnIndex": col},
            "rowSpan": row_span,
            "columnSpan": col_span,
        }

    def background(row, col, hex_color):
        return {"updateTableCellStyle": {
            "tableRange": cell_range(row, col),
            "tableCellStyle": {"backgroundColor": {"color": {"rgbColor": rgb(hex_color)}}},
            "fields": "backgroundColor",
        }}

    requests += [
        background(1, 0, "#F4CCCC"),
        background(1, 2, "#C9DAF8"),
        {"mergeTableCells": {"tableRange": cell_range(0, 0, col_span=2)}},
    ]
    docs.documents().batchUpdate(documentId=doc_id, body={"requests": requests}).execute()


# --- スプレッドシート ---------------------------------------------------------

def cell(value=None, *, bg=None, color=None, bold=False, strike=False, note=None, runs=None, validation=None):
    """セル 1 つ分のデータ（値・書式・メモなど）を作る"""
    data = {}
    if isinstance(value, bool):
        data["userEnteredValue"] = {"boolValue": value}
    elif isinstance(value, (int, float)):
        data["userEnteredValue"] = {"numberValue": value}
    elif isinstance(value, str) and value.startswith("="):
        data["userEnteredValue"] = {"formulaValue": value}
    elif isinstance(value, str):
        data["userEnteredValue"] = {"stringValue": value}

    text_format = {}
    if color:
        text_format["foregroundColor"] = rgb(color)
    if bold:
        text_format["bold"] = True
    if strike:
        text_format["strikethrough"] = True
    cell_format = {}
    if bg:
        cell_format["backgroundColor"] = rgb(bg)
    if text_format:
        cell_format["textFormat"] = text_format
    if cell_format:
        data["userEnteredFormat"] = cell_format

    if note:
        data["note"] = note
    if runs:
        data["textFormatRuns"] = runs
    if validation:
        data["dataValidation"] = validation
    return data


def grid(sheet_id, row_start, row_end, col_start, col_end):
    """シート上の範囲（行・列は 0 始まり、終わりは含まない）"""
    return {
        "sheetId": sheet_id,
        "startRowIndex": row_start, "endRowIndex": row_end,
        "startColumnIndex": col_start, "endColumnIndex": col_end,
    }


def rows_request(sheet_id, rows):
    return {"updateCells": {
        "start": {"sheetId": sheet_id, "rowIndex": 0, "columnIndex": 0},
        "rows": [{"values": row} for row in rows],
        "fields": "userEnteredValue,userEnteredFormat,note,textFormatRuns,dataValidation",
    }}


def build_spreadsheet(sheets, drive, sheet_file_id):
    FORMAT, CHART, HIDDEN = 0, 1, 2
    sheets.spreadsheets().batchUpdate(spreadsheetId=sheet_file_id, body={"requests": [
        {"updateSheetProperties": {"properties": {"sheetId": FORMAT, "title": "書式"}, "fields": "title"}},
        {"addSheet": {"properties": {"sheetId": CHART, "title": "グラフ"}}},
        {"addSheet": {"properties": {"sheetId": HIDDEN, "title": "非表示", "hidden": True}}},
    ]}).execute()

    format_rows = [
        [cell("書式のテスト", bold=True)],
        [cell("背景色: 薄い赤", bg="#F4CCCC"), cell("背景色: 薄い緑", bg="#D9EAD3"), cell("文字色: 赤", color="#FF0000")],
        [
            cell("取り消し線", strike=True),
            cell("太字", bold=True),
            # 「赤い」の 2 文字だけ赤くする（セル内の一部だけの装飾）
            cell("一部だけ赤い文字", runs=[
                {"startIndex": 0, "format": {}},
                {"startIndex": 4, "format": {"foregroundColor": rgb("#FF0000")}},
                {"startIndex": 6, "format": {}},
            ]),
        ],
        [cell("結合したセル（A4:C4）")],
        [cell("チェックボックス"), cell(True, validation={"condition": {"type": "BOOLEAN"}})],
        [cell("プルダウン"), cell("選択肢B", validation={
            "condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": v} for v in ("選択肢A", "選択肢B", "選択肢C")]},
            "showCustomUi": True,
            "strict": True,
        })],
        [cell("合計（数式）"), cell("=SUM(B8:B10)")],
        [cell("値1"), cell(10)],
        [cell("値2"), cell(20)],
        [cell("値3"), cell(30)],
        [cell("メモ付きのセル", note="これはセルのメモです。")],
        [cell("リンク"), cell('=HYPERLINK("https://example.com/", "example.com")')],
        [cell("IMAGE 関数の画像"), cell(f'=IMAGE("{SAMPLE_IMAGE_URL}")')],
        [cell("条件付き書式（50 以上は緑）"), cell(30), cell(60)],
    ]
    chart_rows = [
        [cell("月"), cell("数")],
        [cell("1月"), cell(12)],
        [cell("2月"), cell(18)],
        [cell("3月"), cell(9)],
        [cell("4月"), cell(15)],
    ]

    sheets.spreadsheets().batchUpdate(spreadsheetId=sheet_file_id, body={"requests": [
        rows_request(FORMAT, format_rows),
        {"mergeCells": {"range": grid(FORMAT, 3, 4, 0, 3), "mergeType": "MERGE_ALL"}},
        {"addConditionalFormatRule": {"index": 0, "rule": {
            "ranges": [grid(FORMAT, 13, 14, 1, 3)],
            "booleanRule": {
                "condition": {"type": "NUMBER_GREATER_THAN_EQ", "values": [{"userEnteredValue": "50"}]},
                "format": {"backgroundColor": rgb("#B7E1CD")},
            },
        }}},
        rows_request(CHART, chart_rows),
        {"addChart": {"chart": {
            "spec": {
                "title": "月ごとの数（ダミー）",
                "basicChart": {
                    "chartType": "COLUMN",
                    "legendPosition": "BOTTOM_LEGEND",
                    "axis": [
                        {"position": "BOTTOM_AXIS", "title": "月"},
                        {"position": "LEFT_AXIS", "title": "数"},
                    ],
                    "domains": [{"domain": {"sourceRange": {"sources": [grid(CHART, 1, 5, 0, 1)]}}}],
                    "series": [{
                        "series": {"sourceRange": {"sources": [grid(CHART, 1, 5, 1, 2)]}},
                        "targetAxis": "LEFT_AXIS",
                    }],
                },
            },
            "position": {"overlayPosition": {"anchorCell": {"sheetId": CHART, "rowIndex": 1, "columnIndex": 3}}},
        }}},
        rows_request(HIDDEN, [[cell("非表示シートの内容")]]),
    ]}).execute()

    drive.comments().create(
        fileId=sheet_file_id, body={"content": "これはテスト用のコメントです。"}, fields="id"
    ).execute()


def main():
    creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    drive = build("drive", "v3", credentials=creds)
    docs = build("docs", "v1", credentials=creds)
    sheets = build("sheets", "v4", credentials=creds)

    folder_id = prepare_folder(drive)

    doc = create_file(drive, "テスト用ドキュメント", DOC_MIME, folder_id)
    build_document(docs, drive, doc["id"])
    print(f"作成: テスト用ドキュメント  {doc['webViewLink']}")

    sheet = create_file(drive, "テスト用スプレッドシート", SHEET_MIME, folder_id)
    build_spreadsheet(sheets, drive, sheet["id"])
    print(f"作成: テスト用スプレッドシート  {sheet['webViewLink']}")

    print()
    print(f"テスト用フォルダの ID: {folder_id}")
    print("（config.local.toml の [[folders]] に追加すると sync.py の対象になります）")


if __name__ == "__main__":
    main()
