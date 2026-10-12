"""設定ファイルに書いた Drive のフォルダ・ファイルを output/ に同期する（Drive 側は変更しない）

- Google ドキュメント → HTML 書き出しから作った Markdown（画像・図形描画は <名前>.images/ に取り出す）
- Google スプレッドシート → XLSX と、XLSX から作った Markdown
- Google スライド → PPTX と PDF（Markdown 化は未実装）
- それ以外のファイル → 原本のまま

Markdown への変換は MarkItDown（＋ markitdown_ext の変換部品）で行う。
PDF は、Markdown を作れなかったときだけ保険として作る。

変更されたファイルだけを取り直す:
- 前回の同期の記録（state/）と Drive 上の最終更新日時を比べ、新しいファイル・更新されたファイルだけを処理する
- 名前変更・移動されたファイルは、出力も新しい名前・場所へ移す
- Drive から消えたファイルの出力は archive/ へ移す（一覧の取得に失敗したときは判断しない）
- 変換処理の版（markitdown_ext.CONVERTER_VERSION）が変わったら、全部作り直す

使い方:
    python sync.py          # 変更されたファイルだけ
    python sync.py --full   # 全部作り直す
"""

import argparse
import io
import os
import re
import shutil
import sys
import tomllib
from pathlib import Path, PurePosixPath

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload
from markitdown import FileConversionException, StreamInfo

from list_folder import FOLDER_MIME, SHORTCUT_MIME, list_children
from markitdown_ext import CONVERTER_VERSION, GOOGLE_DOCS_ZIP_MIME, ConversionResult, create_markitdown
from sync_state import Archiver, SyncState, move_outputs, outputs_exist, remove_empty_dirs, untracked_files

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


# --- ファイルの書き出し -------------------------------------------------------

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
    """画像を <name>.images/ に保存してから、それを参照する <name>.md を保存する。作った出力の名前を返す"""
    replace_dir(out_dir / f"{name}.images", images)
    write_atomic(out_dir / f"{name}.md", md.encode("utf-8"))
    return [f"{name}.md"] + ([f"{name}.images"] if images else [])


_markitdown = None


def convert_to_markdown(data, stream_info, **kwargs):
    """MarkItDown（自前の変換部品だけを登録したもの）で Markdown に変換する

    MarkItDown 標準の変換部品は登録しない。標準の変換は色や図形などが消えるうえ、
    自前の部品が失敗したときに標準の部品で「成功」してしまうと、失敗の本当の理由も見えなくなるため。
    """
    global _markitdown
    if _markitdown is None:
        _markitdown = create_markitdown(enable_builtins=False)
    try:
        result = _markitdown.convert_stream(io.BytesIO(data), stream_info=stream_info, **kwargs)
    except FileConversionException as e:
        # 自前の部品が出した本当のエラーを伝える
        if e.attempts and e.attempts[0].exc_info:
            raise e.attempts[0].exc_info[1] from e
        raise
    if not isinstance(result, ConversionResult):
        raise RuntimeError("自前の変換部品で変換できなかった")
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
    return write_markdown(out_dir, name, result.markdown, result.images)


def save_xlsx_markdown(out_dir, name, data):
    """XLSX からシートごとの Markdown と画像を作って保存する"""
    result = convert_to_markdown(
        data,
        StreamInfo(mimetype=XLSX_MIME, extension=".xlsx", filename=f"{name}.xlsx"),
        title=name, images_dirname=f"{name}.images",
    )
    return write_markdown(out_dir, name, result.markdown, result.images)


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
    """Markdown を作る。作れなかったときだけ、保険として PDF を書き出す。作った出力の名前を返す

    PDF の書き出しも失敗した場合は、例外をそのまま上に伝えて「失敗」扱いにする
    """
    try:
        return make_markdown()
    except Exception as e:
        reason = describe_error(e)
        print(f"  Markdown を作れなかったため PDF を作成: {shown}  ({reason})")
        write_atomic(out_dir / f"{name}.pdf", export(service, item["id"], PDF_MIME))
        stats["pdf_fallback"].append(f"{shown}  ({reason})")
        return [f"{name}.pdf"]


def is_supported(item):
    mime = item["mimeType"]
    return mime != SHORTCUT_MIME and (
        not mime.startswith("application/vnd.google-apps.") or mime in SUPPORTED_GOOGLE_MIMES
    )


def sync_file(service, item, out_dir, name, stats, shown):
    """1ファイルを種類に応じて書き出す（Google 形式は export、それ以外は原本をダウンロード）

    戻り値: 作った出力の名前（out_dir の中での名前）。失敗したら None
    """
    mime = item["mimeType"]
    file_id = item["id"]
    try:
        if mime == DOC_MIME:
            return markdown_or_pdf(
                service, item, out_dir, name,
                lambda: save_doc_markdown(service, file_id, out_dir, name),
                stats, shown,
            )
        if mime == SHEET_MIME:
            data = export(service, file_id, XLSX_MIME)
            write_atomic(out_dir / f"{name}.xlsx", data)
            return [f"{name}.xlsx"] + markdown_or_pdf(
                service, item, out_dir, name,
                lambda: save_xlsx_markdown(out_dir, name, data),
                stats, shown,
            )
        if mime == SLIDES_MIME:
            # スライドの Markdown 化は未実装なので、PPTX と PDF を両方作る
            write_atomic(out_dir / f"{name}.pptx", export(service, file_id, PPTX_MIME))
            write_atomic(out_dir / f"{name}.pdf", export(service, file_id, PDF_MIME))
            return [f"{name}.pptx", f"{name}.pdf"]
        write_atomic(out_dir / name, download_media(service, file_id))
        return [name]
    except Exception as e:
        # 1ファイルの失敗で全体を止めない
        # （10MB を超える書き出し、ダウンロード禁止の共有ファイル、変換処理の想定外のエラーなど）
        print(f"  失敗: {shown}  ({describe_error(e)})")
        stats["failed"].append(f"{shown}  ({describe_error(e)})")
        return None


# --- 一覧の取得 -------------------------------------------------------------

def list_tree(service, folder_id, recursive, rel=PurePosixPath(".")):
    """フォルダの中身を全部たどり、[(ファイル, 出力先の中の場所), ...] と「最後まで取れたか」を返す"""
    try:
        items = list_children(service, folder_id)
    except HttpError as e:
        print(f"  一覧の取得に失敗: {rel}  ({describe_error(e)})")
        return [], False
    names = assign_names(items)
    entries, complete = [], True
    for item in items:
        path = rel / names[item["id"]]
        if item["mimeType"] == FOLDER_MIME:
            if recursive:
                sub, sub_complete = list_tree(service, item["id"], recursive, path)
                entries += sub
                complete = complete and sub_complete
        else:
            entries.append((item, str(path)))
    return entries, complete


def get_single_file(service, file_id):
    """個別ファイルの情報。消された・共有が外れた（404）なら空の一覧、ほかのエラーは「取れなかった」"""
    try:
        item = service.files().get(
            fileId=file_id, fields="id, name, mimeType, modifiedTime", supportsAllDrives=True
        ).execute()
    except HttpError as e:
        if e.status_code == 404:
            return [], True
        print(f"  情報の取得に失敗: {file_id}  ({describe_error(e)})")
        return [], False
    return [(item, safe_name(item["name"]))], True


# --- 同期 -----------------------------------------------------------------

def new_stats():
    return {"new": 0, "updated": 0, "unchanged": 0, "moved": 0, "archived": 0,
            "skipped": [], "failed": [], "pdf_fallback": []}


def sync_target(service, target_name, out_dir, entries, complete, config, full):
    """同期先 1 つ分（フォルダ 1 つ、または個別ファイル 1 つ）を同期する"""
    stats = new_stats()
    state = SyncState(Path(config.get("state_root", "state")) / f"{safe_name(target_name)}.json", CONVERTER_VERSION)
    archiver = Archiver(config.get("archive_root", "archive"), safe_name(target_name), out_dir)
    if state.version_changed:
        print(f"  変換処理の版が変わったため、全部作り直します（版 {CONVERTER_VERSION}）")

    seen = set()
    for item, path in entries:
        if not is_supported(item):
            print(f"  スキップ（未対応の種類）: {path}  [{item['mimeType']}]")
            stats["skipped"].append(path)
            continue
        file_id = item["id"]
        seen.add(file_id)
        prev = state.files.get(file_id)

        # 名前変更・移動: 出力も新しい名前・場所へ移す
        renamed = False
        if prev and prev["path"] != path:
            prev["outputs"] = move_outputs(out_dir, prev["path"], path, prev["outputs"], archiver)
            print(f"  移動: {prev['path']} → {path}")
            # 名前が変わったときは、Markdown の中の画像リンク（<名前>.images/…）と見出しも変わるので作り直す
            renamed = PurePosixPath(prev["path"]).name != PurePosixPath(path).name
            prev["path"] = path
            prev["name"] = item["name"]
            stats["moved"] += 1

        changed = (
            prev is None
            or renamed
            or prev["modifiedTime"] != item["modifiedTime"]
            or not outputs_exist(out_dir, prev["outputs"])
        )
        if not (full or state.version_changed or changed):
            stats["unchanged"] += 1
            continue

        rel_dir = PurePosixPath(path).parent
        (out_dir / rel_dir).mkdir(parents=True, exist_ok=True)
        made = sync_file(service, item, out_dir / rel_dir, PurePosixPath(path).name, stats, path)
        if made is None:
            continue  # 失敗: 前回の出力と記録はそのまま残し、次回また試す
        outputs = [str(rel_dir / name) for name in made]

        # 前回はあって今回は作らなかった出力（例: 前回は保険の PDF だった）は退避する
        for old in set(prev["outputs"] if prev else []) - set(outputs):
            archiver.archive(old)

        state.files[file_id] = {
            "name": item["name"], "path": path, "mimeType": item["mimeType"],
            "modifiedTime": item["modifiedTime"], "outputs": outputs,
        }
        print(f"  {'更新' if prev else '新規'}: {path}")
        stats["updated" if prev else "new"] += 1

    # Drive から消えたファイル: 一覧を最後まで取れたときだけ判断する（通信エラーで消さないため）
    if complete:
        for file_id in [fid for fid in state.files if fid not in seen]:
            entry = state.files.pop(file_id)
            for rel in entry["outputs"]:
                archiver.archive(rel)
            print(f"  退避（Drive から消えた）: {entry['path']} → {archiver.base}")
            stats["archived"] += 1
    else:
        print("  一覧を最後まで取れなかったため、Drive から消えたファイルの判断はしません")

    remove_empty_dirs(out_dir)
    state.save()

    leftover = untracked_files(out_dir, state.files)
    if leftover:
        print(f"  記録にないファイルが {len(leftover)} 件あります（以前の版の出力など。不要なら手で整理してください）:")
        for rel in leftover[:5]:
            print(f"    {rel}")
        if len(leftover) > 5:
            print(f"    …ほか {len(leftover) - 5} 件")
    return stats


def print_summary(stats):
    print(
        f"--- 完了: 新規 {stats['new']} / 更新 {stats['updated']} / 変化なし {stats['unchanged']}"
        f" / 移動 {stats['moved']} / 退避 {stats['archived']}"
        f" / スキップ {len(stats['skipped'])} / 失敗 {len(stats['failed'])}"
        f"（PDF で代替 {len(stats['pdf_fallback'])}） ---"
    )
    for line in stats["pdf_fallback"]:
        print(f"  PDF で代替: {line}")
    for line in stats["failed"]:
        print(f"  失敗: {line}")
    print()


def main():
    parser = argparse.ArgumentParser(description="Google Drive の資料を AI 向けの Markdown に同期する")
    parser.add_argument("--full", action="store_true", help="変更の有無にかかわらず全部作り直す")
    args = parser.parse_args()

    if not CONFIG_FILE.exists():
        sys.exit(f"{CONFIG_FILE} がありません。config.example.toml をコピーして作成してください。")
    config = tomllib.loads(CONFIG_FILE.read_text(encoding="utf-8"))

    creds = Credentials.from_authorized_user_file(config["token_file"], SCOPES)
    service = build("drive", "v3", credentials=creds)
    output_root = Path(config["output_root"])

    for folder in config.get("folders", []):
        out_dir = output_root / safe_name(folder["name"])
        print(f"=== {folder['name']} → {out_dir} ===")
        entries, complete = list_tree(service, folder["folder_id"], folder["recursive"])
        print_summary(sync_target(service, folder["name"], out_dir, entries, complete, config, args.full))

    for entry in config.get("files", []):
        out_dir = output_root / safe_name(entry["name"])
        print(f"=== {entry['name']}（個別ファイル） → {out_dir} ===")
        entries, complete = get_single_file(service, entry["file_id"])
        print_summary(sync_target(service, entry["name"], out_dir, entries, complete, config, args.full))


if __name__ == "__main__":
    main()
