"""設定ファイルに書いた Drive フォルダを output/ にダウンロードする（Drive 側は変更しない）

- Google ドキュメント → HTML 書き出しから作った Markdown（画像・図形描画は <名前>.images/ に取り出す）
- Google スプレッドシート → XLSX と、XLSX から作った Markdown

Markdown への変換は MarkItDown（＋ markitdown_ext の変換部品）で行う。
- Google スライド → PPTX と PDF（Markdown 化は未実装）
- それ以外のファイル → 原本のまま

PDF は、Markdown を作れなかったときだけ保険として作る。
今の版は毎回すべて取り直す。変更されたものだけ取り直す仕組みは次の工程で追加する。
"""

import io
import os
import re
import shutil
import sys
import tomllib
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

from markitdown import StreamInfo

from list_folder import FOLDER_MIME, SHORTCUT_MIME, list_children
from markitdown_ext import GOOGLE_DOCS_ZIP_MIME, ConversionResult, create_markitdown

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
CONFIG_FILE = Path("config.local.toml")

DOC_MIME = "application/vnd.google-apps.document"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"
SLIDES_MIME = "application/vnd.google-apps.presentation"
SUPPORTED_GOOGLE_MIMES = {DOC_MIME, SHEET_MIME, SLIDES_MIME}

MARKDOWN_MIME = "text/markdown"
HTML_ZIP_MIME = "application/zip"
PDF_MIME = "application/pdf"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

# NAS(SMB) や Windows でも扱えるよう、ファイル名に使えない文字を置き換える
INVALID_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_name(name):
    name = INVALID_CHARS.sub("_", name).strip().rstrip(".")
    return name or "_"


def assign_names(items):
    """フォルダ内の各アイテムに保存名を決める。同名があれば ID の先頭6文字を付けて区別する"""
    by_name = {}
    for item in items:
        by_name.setdefault(safe_name(item["name"]).casefold(), []).append(item)

    names = {}
    for group in by_name.values():
        for item in group:
            base = safe_name(item["name"])
            names[item["id"]] = base if len(group) == 1 else f"{base}_{item['id'][:6]}"
    return names


def write_atomic(path, data):
    """一時ファイルに書いてから差し替える。途中で失敗しても前の完成版は壊れない"""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def replace_dir(path, files):
    """フォルダを丸ごと作り直す。完成してから差し替えるので、途中で失敗しても前の版は残る

    files が空なら、前回の残りがあれば消すだけ（画像が無くなったドキュメント用）
    """
    tmp = path.with_name(f".{path.name}.tmp")
    old = path.with_name(f".{path.name}.old")
    for leftover in (tmp, old):
        if leftover.exists():
            shutil.rmtree(leftover)

    if files:
        tmp.mkdir()
        for filename, data in files:
            (tmp / filename).write_bytes(data)
    if path.exists():
        path.rename(old)
    if files:
        tmp.rename(path)
    if old.exists():
        shutil.rmtree(old)


def write_markdown(out_dir, name, md, images):
    """画像を <name>.images/ に保存してから、それを参照する <name>.md を保存する"""
    replace_dir(out_dir / f"{name}.images", images)
    write_atomic(out_dir / f"{name}.md", md.encode("utf-8"))


_markitdown = None


def convert_to_markdown(data, stream_info, **kwargs):
    """MarkItDown で Markdown に変換する

    自前の変換部品（markitdown_ext）が失敗すると、MarkItDown は標準の変換を試す。
    標準の変換は色や図形などが消えるので、それで成功した場合も「失敗」として扱い、保険の PDF に回す。
    """
    global _markitdown
    if _markitdown is None:
        _markitdown = create_markitdown()
    result = _markitdown.convert_stream(io.BytesIO(data), stream_info=stream_info, **kwargs)
    if not isinstance(result, ConversionResult):
        raise RuntimeError("拡張の変換部品で変換できず、MarkItDown 標準の変換が使われた")
    return result


def save_doc_markdown(service, file_id, out_dir, name):
    """ドキュメントを HTML で書き出し、Markdown と画像（図形描画を含む）にして保存する

    Google の Markdown 書き出しは、チェックリストの「- [ ]」を補うためだけに使う（失敗しても続ける）
    """
    zip_data = export(service, file_id, HTML_ZIP_MIME)
    try:
        markdown_export = export(service, file_id, MARKDOWN_MIME).decode("utf-8")
    except Exception:
        markdown_export = None
    result = convert_to_markdown(
        zip_data,
        StreamInfo(mimetype=GOOGLE_DOCS_ZIP_MIME, extension=".zip", filename=f"{name}.zip"),
        title=name, images_dirname=f"{name}.images", markdown_export=markdown_export,
    )
    write_markdown(out_dir, name, result.markdown, result.images)


def save_xlsx_markdown(out_dir, name, data):
    """XLSX からシートごとの Markdown と画像を作って保存する"""
    result = convert_to_markdown(
        data,
        StreamInfo(mimetype=XLSX_MIME, extension=".xlsx", filename=f"{name}.xlsx"),
        title=name, images_dirname=f"{name}.images",
    )
    write_markdown(out_dir, name, result.markdown, result.images)


def download_media(service, file_id):
    """Google 形式でない通常ファイル（PDF・画像など）を原本のまま取得する"""
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(
        buf, service.files().get_media(fileId=file_id, supportsAllDrives=True)
    )
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


def export(service, file_id, mime):
    """Google 形式のファイルを指定の形式で書き出す（上限 10MB）"""
    return service.files().export(fileId=file_id, mimeType=mime).execute()


def describe_error(e):
    if isinstance(e, HttpError):
        return f"{e.status_code} {e.reason}"
    return f"{type(e).__name__}: {e}"


def markdown_or_pdf(service, item, out_dir, name, make_markdown, stats, shown):
    """Markdown を作る。作れなかったときだけ、保険として PDF を書き出す

    PDF の書き出しも失敗した場合は、例外をそのまま上に伝えて「失敗」扱いにする
    """
    try:
        make_markdown()
    except Exception as e:
        reason = describe_error(e)
        print(f"  Markdown を作れなかったため PDF を作成: {shown}  ({reason})")
        write_atomic(out_dir / f"{name}.pdf", export(service, item["id"], PDF_MIME))
        stats["pdf_fallback"].append(f"{shown}  ({reason})")


def sync_file(service, item, out_dir, name, stats, shown):
    """1ファイルを種類に応じて書き出す（Google 形式は export、それ以外は原本をダウンロード）"""
    mime = item["mimeType"]
    file_id = item["id"]
    if mime == SHORTCUT_MIME or (
        mime.startswith("application/vnd.google-apps.") and mime not in SUPPORTED_GOOGLE_MIMES
    ):
        print(f"  スキップ（未対応の種類）: {shown}  [{mime}]")
        stats["skipped"].append(shown)
        return

    try:
        if mime == DOC_MIME:
            markdown_or_pdf(
                service, item, out_dir, name,
                lambda: save_doc_markdown(service, file_id, out_dir, name),
                stats, shown,
            )
        elif mime == SHEET_MIME:
            data = export(service, file_id, XLSX_MIME)
            write_atomic(out_dir / f"{name}.xlsx", data)
            markdown_or_pdf(
                service, item, out_dir, name,
                lambda: save_xlsx_markdown(out_dir, name, data),
                stats, shown,
            )
        elif mime == SLIDES_MIME:
            # スライドの Markdown 化は未実装なので、PPTX と PDF を両方作る
            write_atomic(out_dir / f"{name}.pptx", export(service, file_id, PPTX_MIME))
            write_atomic(out_dir / f"{name}.pdf", export(service, file_id, PDF_MIME))
        else:
            write_atomic(out_dir / name, download_media(service, file_id))
        print(f"  OK: {shown}")
        stats["ok"] += 1
    except Exception as e:
        # 1ファイルの失敗で全体を止めない
        # （10MB を超える書き出し、ダウンロード禁止の共有ファイル、変換処理の想定外のエラーなど）
        print(f"  失敗: {shown}  ({describe_error(e)})")
        stats["failed"].append(f"{shown}  ({describe_error(e)})")


def sync_folder(service, folder_id, out_dir, recursive, stats, rel=""):
    out_dir.mkdir(parents=True, exist_ok=True)
    items = list_children(service, folder_id)
    names = assign_names(items)

    for item in items:
        name = names[item["id"]]
        shown = f"{rel}{name}"
        if item["mimeType"] == FOLDER_MIME:
            if recursive:
                sync_folder(service, item["id"], out_dir / name, recursive, stats, f"{shown}/")
            continue
        sync_file(service, item, out_dir, name, stats, shown)


def new_stats():
    return {"ok": 0, "skipped": [], "failed": [], "pdf_fallback": []}


def print_summary(stats):
    print(
        f"--- 完了: 成功 {stats['ok']}（うち PDF で代替 {len(stats['pdf_fallback'])}）"
        f" / スキップ {len(stats['skipped'])} / 失敗 {len(stats['failed'])} ---"
    )
    for line in stats["pdf_fallback"]:
        print(f"  PDF で代替: {line}")
    for line in stats["failed"]:
        print(f"  失敗: {line}")
    print()


def main():
    if not CONFIG_FILE.exists():
        sys.exit(f"{CONFIG_FILE} がありません。config.example.toml をコピーして作成してください。")
    config = tomllib.loads(CONFIG_FILE.read_text(encoding="utf-8"))

    creds = Credentials.from_authorized_user_file(config["token_file"], SCOPES)
    service = build("drive", "v3", credentials=creds)
    output_root = Path(config["output_root"])

    for folder in config.get("folders", []):
        out_dir = output_root / safe_name(folder["name"])
        print(f"=== {folder['name']} → {out_dir} ===")
        stats = new_stats()
        sync_folder(service, folder["folder_id"], out_dir, folder["recursive"], stats)
        print_summary(stats)

    for entry in config.get("files", []):
        out_dir = output_root / safe_name(entry["name"])
        print(f"=== {entry['name']}（個別ファイル） → {out_dir} ===")
        stats = new_stats()
        try:
            item = service.files().get(
                fileId=entry["file_id"], fields="id, name, mimeType", supportsAllDrives=True
            ).execute()
        except HttpError as e:
            # ID の間違いや共有の解除などで見つからない場合
            stats["failed"].append(f"{entry['file_id']}  ({describe_error(e)})")
        else:
            out_dir.mkdir(parents=True, exist_ok=True)
            name = safe_name(item["name"])
            sync_file(service, item, out_dir, name, stats, name)
        print_summary(stats)


if __name__ == "__main__":
    main()
