"""設定ファイル（既定は data/config.toml）に書いた Drive のフォルダ・ファイルを同期する（Drive 側は変更しない）

同期先ごとに、出力のしかたを選ぶ（設定の ai_markdown・keep_originals）:

AI 用に変換する（ai_markdown = true。既定）:
- Google ドキュメント → HTML 書き出しから作った Markdown（画像・図形描画は <名前>.images/ に取り出す）
- Google スプレッドシート・アップロードされた Excel（.xlsx）→ XLSX から作った Markdown
- Google スライド → PDF（Markdown 化は未実装）
- それ以外のファイル → 原本のまま
- keep_originals = true なら、元のファイル（.docx・.xlsx・.pptx・アップロードされた .xlsx）も残す（既定は残さない）

変換しない（ai_markdown = false。人が使う同期）:
- Google 形式のファイル → 元のファイルを開くショートカット（<名前>.url。Windows・Mac で開ける）
- それ以外のファイル → 原本のまま

Markdown への変換は MarkItDown（＋ markitdown_ext の変換部品）で行う。
PDF は、Markdown を作れなかったときだけ保険として作る。

変更されたファイルだけを取り直す:
- 前回の同期の記録（state/）と Drive 上の最終更新日時を比べ、新しいファイル・更新されたファイルだけを処理する
- 名前変更・移動されたファイルは、出力も新しい名前・場所へ移す
- Drive から消えたファイルの出力は archive/ へ移す（一覧の取得に失敗したときは判断しない）
- --archive-max-mb を付けると、archive/ の合計が上限を超えたとき、古い退避から消す
- 変換処理の版（markitdown_ext.CONVERTER_VERSION）が変わったら、全部作り直す

使い方:
    python sync.py                     # 変更されたファイルだけ
    python sync.py --full              # 全部作り直す
    python sync.py --config 別の設定.toml
    python sync.py --summary 結果.json        # 結果を JSON で書き出す（管理画面が読む）
    python sync.py --archive-max-mb 1024      # 退避の合計が 1024 MB を超えたら、古い退避から消す
"""

import argparse
import io
import json
import os
import re
import sys
import tomllib
from datetime import datetime
from pathlib import Path, PurePosixPath

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload
from markitdown import FileConversionException, StreamInfo

from list_folder import FOLDER_MIME, SHORTCUT_MIME, list_children
from markitdown_ext import CONVERTER_VERSION, GOOGLE_DOCS_ZIP_MIME, ConversionResult, create_markitdown
from sync_state import (STAMP_FORMAT, Archiver, SyncState, move_outputs, outputs_exist, prune_archive,
                        relocate_outputs, remove_empty_dirs, remove_tree, untracked_files)

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
DEFAULT_CONFIG = Path(os.environ.get("CDS_CONFIG", "data/config.toml"))
# どこで動いているか（同期先の environments と比べる）。Mac での開発は dev、NAS のコンテナは nas にする
ENVIRONMENT = os.environ.get("CDS_ENV", "dev")
# 場所の設定は環境変数で上書きできる（NAS のコンテナでは、コンテナの中の場所にする）
PATH_SETTINGS = {"token_file": "CDS_TOKEN_FILE", "output_root": "CDS_OUTPUT_ROOT",
                 "state_root": "CDS_STATE_ROOT", "archive_root": "CDS_ARCHIVE_ROOT"}

DOC_MIME = "application/vnd.google-apps.document"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"
SLIDES_MIME = "application/vnd.google-apps.presentation"
SUPPORTED_GOOGLE_MIMES = {DOC_MIME, SHEET_MIME, SLIDES_MIME}

MARKDOWN_MIME = "text/markdown"
HTML_ZIP_MIME = "application/zip"
PDF_MIME = "application/pdf"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSM_MIME = "application/vnd.ms-excel.sheet.macroEnabled.12"
DEFAULT_OPTIONS = {"ai_markdown": True, "keep_originals": False}

# NAS(SMB) や Windows でも扱えるよう、ファイル名に使えない文字を置き換える
INVALID_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_name(name):
    name = INVALID_CHARS.sub("_", name).strip().rstrip(".")
    return name or "_"


# Google 形式のファイルから作る出力の後ろの部分（<名前>.md、<名前>.pdf など）
DERIVED_SUFFIXES = (".md", ".images", ".pdf", ".docx", ".xlsx", ".pptx", ".url")


def assign_names(items):
    """フォルダ内の各アイテムに保存名を決める。同名があれば ID の先頭6文字を付けて区別する

    Google 形式のファイルから作る出力（例: スライド「説明」→ 説明.pdf）が、同じフォルダのアップロードされた
    ファイル（例: 説明.pdf）と同じ名前になるときも、Google 形式のほうに ID の先頭6文字を付ける
    （大文字・小文字の違いも同じとみなす。NAS の共有フォルダでは区別されないため）
    """
    by_name = {}
    for item in items:
        by_name.setdefault(safe_name(item["name"]).casefold(), []).append(item)

    names = {}
    for group in by_name.values():
        for item in group:
            base = safe_name(item["name"])
            names[item["id"]] = base if len(group) == 1 else f"{base}_{item['id'][:6]}"

    uploaded = {names[i["id"]].casefold() for i in items if not i["mimeType"].startswith("application/vnd.google-apps.")}
    for item in items:
        name = names[item["id"]]
        if item["mimeType"].startswith("application/vnd.google-apps.") and any(
            (name + suffix).casefold() in uploaded for suffix in DERIVED_SUFFIXES
        ):
            names[item["id"]] = f"{name}_{item['id'][:6]}"
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
            remove_tree(leftover)

    if files:
        tmp.mkdir()
        for filename, data in files:
            (tmp / filename).write_bytes(data)
    if path.exists():
        path.rename(old)
    if files:
        tmp.rename(path)
    if old.exists():
        remove_tree(old)


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


DOWNLOAD_CHUNK = 16 * 1024 * 1024
# これより大きいアップロードされた Excel は、Markdown にしない（変換でメモリを使い切らないように）
MAX_EXCEL_BYTES = 50 * 1024 * 1024


def download_media(service, file_id, dest):
    """Google 形式でない通常ファイル（PDF・画像・動画など）を、原本のまま dest に保存する

    大きなファイルでもメモリを使い切らないように、少しずつ書き出す（コンテナのメモリには上限がある）
    """
    with open(dest, "wb") as fh:
        downloader = MediaIoBaseDownload(
            fh, service.files().get_media(fileId=file_id, supportsAllDrives=True), chunksize=DOWNLOAD_CHUNK
        )
        done = False
        while not done:
            _, done = downloader.next_chunk()


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
        stats["pdf_fallback"].append(f"{shown} → PDF  ({reason})")
        return [f"{name}.pdf"]


def is_google_file(item):
    mime = item["mimeType"]
    return mime.startswith("application/vnd.google-apps.") and mime not in (FOLDER_MIME, SHORTCUT_MIME)


def is_excel(item):
    return item["mimeType"] in (XLSX_MIME, XLSM_MIME) or item["name"].lower().endswith((".xlsx", ".xlsm"))


def is_supported(item, options=DEFAULT_OPTIONS):
    """同期できる種類か。変換しない（人が使う）同期では、Google 形式はすべてショートカットにできる"""
    mime = item["mimeType"]
    if mime == SHORTCUT_MIME:
        return False
    if is_google_file(item):
        return not options["ai_markdown"] or mime in SUPPORTED_GOOGLE_MIMES
    return True


def shortcut_file(item):
    """Google 形式のファイルを開くショートカット（Windows の「インターネット ショートカット」。Mac でも開ける）"""
    url = item.get("webViewLink") or f"https://drive.google.com/open?id={item['id']}"
    return f"[InternetShortcut]\r\nURL={url}\r\n".encode("utf-8")


def save_original(service, item, out_dir, name):
    """原本をダウンロードして保存する（一時ファイルに書いてから差し替える）"""
    tmp = out_dir / f".{name}.download"
    try:
        download_media(service, item["id"], tmp)
        os.replace(tmp, out_dir / name)
    finally:
        tmp.unlink(missing_ok=True)
    return [name]


def save_uploaded_excel(service, item, out_dir, name, stats, shown, keep):
    """アップロードされた Excel から Markdown を作る（名前は「<元の名前>.md」。例: 予算.xlsx.md）

    作れないとき・大きすぎるときは、原本を残す（AI が読めなくても、資料が無くなるよりよい）
    """
    tmp = out_dir / f".{name}.download"
    try:
        download_media(service, item["id"], tmp)
        size = tmp.stat().st_size
        made = []
        if size > MAX_EXCEL_BYTES:
            reason = f"{size // (1024 * 1024)} MB と大きいため、Markdown にしない（上限 {MAX_EXCEL_BYTES // (1024 * 1024)} MB）"
        else:
            try:
                made, reason = save_xlsx_markdown(out_dir, name, tmp.read_bytes()), None
            except Exception as e:
                reason = describe_error(e)
        if reason:
            print(f"  Markdown を作れなかったため原本のまま保存: {shown}  ({reason})")
            stats["pdf_fallback"].append(f"{shown} → 原本のまま  ({reason})")
            keep = True
        if keep:
            os.replace(tmp, out_dir / name)
            made.append(name)
        return made
    finally:
        tmp.unlink(missing_ok=True)


def sync_file(service, item, out_dir, name, stats, shown, options=DEFAULT_OPTIONS):
    """1ファイルを、出力のしかた（options）と種類に応じて書き出す

    戻り値: 作った出力の名前（out_dir の中での名前）。失敗したら None
    """
    mime = item["mimeType"]
    file_id = item["id"]
    keep = options["keep_originals"]
    try:
        if not options["ai_markdown"]:
            # 人が使う同期: Google 形式はダウンロードできないので、元のファイルを開くショートカットにする
            if is_google_file(item):
                write_atomic(out_dir / f"{name}.url", shortcut_file(item))
                return [f"{name}.url"]
            return save_original(service, item, out_dir, name)

        if mime == DOC_MIME:
            made = markdown_or_pdf(
                service, item, out_dir, name,
                lambda: save_doc_markdown(service, file_id, out_dir, name),
                stats, shown,
            )
            if keep:
                write_atomic(out_dir / f"{name}.docx", export(service, file_id, DOCX_MIME))
                made.append(f"{name}.docx")
            return made
        if mime == SHEET_MIME:
            data = export(service, file_id, XLSX_MIME)
            made = markdown_or_pdf(
                service, item, out_dir, name,
                lambda: save_xlsx_markdown(out_dir, name, data),
                stats, shown,
            )
            if keep:
                write_atomic(out_dir / f"{name}.xlsx", data)
                made.append(f"{name}.xlsx")
            return made
        if mime == SLIDES_MIME:
            # スライドの Markdown 化は未実装なので、AI が読める PDF を作る
            write_atomic(out_dir / f"{name}.pdf", export(service, file_id, PDF_MIME))
            made = [f"{name}.pdf"]
            if keep:
                write_atomic(out_dir / f"{name}.pptx", export(service, file_id, PPTX_MIME))
                made.append(f"{name}.pptx")
            return made
        if is_excel(item):
            return save_uploaded_excel(service, item, out_dir, name, stats, shown, keep)
        return save_original(service, item, out_dir, name)
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
            fileId=file_id, fields="id, name, mimeType, modifiedTime, webViewLink", supportsAllDrives=True
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


def load_state(config, target_name, options=None):
    return SyncState(Path(config.get("state_root", "state")) / f"{safe_name(target_name)}.json", CONVERTER_VERSION,
                     options)


def target_options(target):
    """同期先の出力のしかた。元のファイルを残すかは、AI 用に変換するときだけ意味がある"""
    ai = target.get("ai_markdown", DEFAULT_OPTIONS["ai_markdown"])
    keep = target.get("keep_originals", DEFAULT_OPTIONS["keep_originals"])
    if not isinstance(ai, bool) or not isinstance(keep, bool):
        sys.exit(f"設定エラー: 同期先「{target['name']}」の ai_markdown・keep_originals は true か false で書いてください。")
    return {"ai_markdown": ai, "keep_originals": keep and ai}


def sync_target(service, kind, target_name, out_dir, entries, complete, config, full, options=DEFAULT_OPTIONS):
    """同期先 1 つ分（kind が folder ならフォルダ 1 つ、file なら個別ファイル 1 つ）を同期する

    個別ファイルは、ほかの同期先と同じフォルダに置けるので、自分の出力以外には触らない
    （記録にないファイルの検査や、空のフォルダの片付けをしない）。
    """
    stats = new_stats()
    state = load_state(config, target_name, options)
    archiver = Archiver(config.get("archive_root", "archive"), safe_name(target_name), out_dir)

    # 出力先が変わった（設定の変更や、Drive のフォルダ名の変更）: 前の出力を新しい出力先へ移す
    if state.out_dir and Path(state.out_dir) != out_dir:
        moved = relocate_outputs(state.out_dir, out_dir, state.files)
        print(f"  出力先が変わったため、出力を移しました（{moved} 件）: {state.out_dir} → {out_dir}")
        if kind == "folder":
            remove_empty_dirs(state.out_dir, keep_root=False)
        else:
            try:
                Path(state.out_dir).rmdir()  # 空になったときだけ消える（ほかのものが残っていれば何もしない）
            except OSError:
                pass
    state.out_dir = str(out_dir)

    if state.version_changed:
        print(f"  変換処理の版が変わったため、全部作り直します（版 {CONVERTER_VERSION}）")
    if state.options_changed:
        print(f"  出力のしかたが変わったため、全部作り直します（AI 用に変換: {options['ai_markdown']}、"
              f"元のファイルも残す: {options['keep_originals']}）")
    # この出力を作ったときの版と出力のしかた。ファイルごとに記録し、違うファイルだけを作り直す
    # （作り直しの途中で失敗したファイルも、次の同期でまた試す）
    mode = {"converter_version": CONVERTER_VERSION, **options}

    seen = set()
    for item, path in entries:
        if not is_supported(item, options):
            print(f"  スキップ（未対応の種類）: {path}  [{item['mimeType']}]")
            stats["skipped"].append(path)
            stale = state.files.pop(item["id"], None)
            if stale:
                # 前の出力のしかたで作った出力（例: 人が使う同期のときのショートカット）は退避する
                for rel in stale["outputs"]:
                    archiver.archive(rel)
                print(f"  退避（この出力のしかたでは同期しない種類）: {stale['path']} → {archiver.base}")
                stats["archived"] += 1
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
            or prev.get("mode", state.legacy_mode) != mode
            or not outputs_exist(out_dir, prev["outputs"])
        )
        if not (full or changed):
            stats["unchanged"] += 1
            continue

        rel_dir = PurePosixPath(path).parent
        (out_dir / rel_dir).mkdir(parents=True, exist_ok=True)
        made = sync_file(service, item, out_dir / rel_dir, PurePosixPath(path).name, stats, path, options)
        if made is None:
            continue  # 失敗: 前回の出力と記録はそのまま残し、次回また試す
        outputs = [str(rel_dir / name) for name in made]

        # 前回はあって今回は作らなかった出力（例: 前回は保険の PDF だった）は退避する
        for old in set(prev["outputs"] if prev else []) - set(outputs):
            archiver.archive(old)

        state.files[file_id] = {
            "name": item["name"], "path": path, "mimeType": item["mimeType"],
            "modifiedTime": item["modifiedTime"], "outputs": outputs, "mode": mode,
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

    if kind == "folder":
        remove_empty_dirs(out_dir)
    state.save()

    leftover = untracked_files(out_dir, state.files) if kind == "folder" else []
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
        f"（Markdown を作れず代替 {len(stats['pdf_fallback'])}） ---"
    )
    for line in stats["pdf_fallback"]:
        print(f"  Markdown を作れず代替: {line}")
    for line in stats["failed"]:
        print(f"  失敗: {line}")
    print()


def load_config(path):
    """設定ファイルを読む。場所の設定（PATH_SETTINGS）は環境変数で上書きでき、~ はホームフォルダにする"""
    config = tomllib.loads(path.read_text(encoding="utf-8"))
    for key, env in PATH_SETTINGS.items():
        if os.environ.get(env):
            config[key] = os.environ[env]
        if key in config:
            config[key] = str(Path(config[key]).expanduser())
    return config


def active_targets(config):
    """この環境（CDS_ENV）で同期する同期先。environments を書いた同期先は、そこに含まれる環境でだけ同期する"""
    result, names = [], set()
    for kind, key in (("folder", "folders"), ("file", "files")):
        for target in config.get(key, []):
            name = safe_name(target["name"])
            if name in names:
                sys.exit(f"設定エラー: 同期先の名前「{target['name']}」が重複しています。")
            names.add(name)
            environments = target.get("environments")
            if environments is None or ENVIRONMENT in environments:
                result.append((kind, target))
    return result


def resolve_out_dir(config, target, drive_name):
    """出力先 = output_root の下の、同期先の output（省略時は同期先の名前）

    output の中の {drive_name} は、Drive 上のフォルダ名・ファイル名にする。名前を取得できなかったときは None
    """
    output = target.get("output", target["name"])
    if "{drive_name}" in output:
        if not drive_name:
            return None
        output = output.replace("{drive_name}", safe_name(drive_name))
    return (Path(config["output_root"]) / output).resolve()


def check_out_dirs(resolved):
    """同期先どうしの出力が混ざらないか、同期を始める前に確かめる

    - フォルダの同期先どうしは、同じ場所・入れ子にできない
    - 個別ファイルは、ほかの同期先と同じフォルダや、フォルダの同期先の外側（親）に置ける。フォルダの同期先の中には置けない
    """
    folders = [(target["name"], out_dir) for kind, target, out_dir in resolved if kind == "folder"]
    for i, (name, out_dir) in enumerate(folders):
        for other_name, other in folders[i + 1:]:
            if out_dir == other or out_dir in other.parents or other in out_dir.parents:
                sys.exit(f"設定エラー: 「{name}」と「{other_name}」の出力先が同じか入れ子になっています。")
    for kind, target, out_dir in resolved:
        if kind == "file":
            for other_name, other in folders:
                if out_dir == other or other in out_dir.parents:
                    sys.exit(f"設定エラー: 個別ファイル「{target['name']}」の出力先が、フォルダの同期先「{other_name}」の中になっています。")


def prepare_targets(service, config, errors):
    """同期先ごとに、Drive 上の名前を取得して出力先を決める。[(種類, 同期先, 出力先, 個別ファイルの一覧, 取れたか), ...]

    同期できない同期先があれば、理由を errors に足す
    """
    prepared = []
    for kind, target in active_targets(config):
        if kind == "folder":
            try:
                drive_name = service.files().get(
                    fileId=target["folder_id"], fields="name", supportsAllDrives=True
                ).execute()["name"]
            except HttpError as e:
                print(f"=== {target['name']}: フォルダの情報を取得できないため、今回は同期しません（{describe_error(e)}） ===\n")
                errors.append(f"{target['name']}: フォルダの情報を取得できない（{describe_error(e)}）")
                continue
            entries, complete = None, True
        else:
            entries, complete = get_single_file(service, target["file_id"])
            drive_name = entries[0][0]["name"] if entries else None
        out_dir = resolve_out_dir(config, target, drive_name)
        if out_dir is None:
            # 名前を取得できない（消えた・通信エラー）ときは、前回の出力先のまま扱う
            previous = load_state(config, target["name"]).out_dir
            if not previous:
                print(f"=== {target['name']}: 出力先を決められないため、今回は同期しません ===\n")
                errors.append(f"{target['name']}: 出力先を決められない")
                continue
            out_dir = Path(previous)
        prepared.append((kind, target, out_dir, entries, complete))
    check_out_dirs([(kind, target, out_dir) for kind, target, out_dir, _, _ in prepared])
    return prepared


def describe_fatal(e):
    """同期全体が止まったエラーを、利用者向けの短い文にする"""
    text = str(e)
    if "invalid_grant" in text:
        return "Google の鍵（token.json）が無効です（期限切れ・取り消し）。auth.py で認証し直し、鍵を置き直してください。"
    if isinstance(e, SystemExit):
        return text or "設定の誤りで止まりました。"
    if isinstance(e, FileNotFoundError):
        return f"ファイルが見つかりません: {e.filename}"
    return describe_error(e)


def clean_archive(config, max_mb, started, errors):
    """退避の大きさを調べ、上限（MB）があれば超えた分を古い退避から消す。今回の同期で退避したものは消さない"""
    max_bytes = max_mb * 1024 * 1024 if max_mb and max_mb > 0 else None
    result = prune_archive(config.get("archive_root", "archive"), max_bytes, keep_since=started.strftime(STAMP_FORMAT))
    if result["removed"]:
        print(f"退避の合計が上限（{max_mb} MB）を超えたため、古い退避を {result['removed']} 回分削除しました"
              f"（{result['removed_bytes'] / 1024 / 1024:.1f} MB）")
    for line in result.pop("errors"):
        errors.append(f"古い退避を削除できなかった: {line}")
    return {**result, "max_bytes": max_bytes}


def run(args):
    """同期を 1 回行い、結果（同期先ごとの件数・失敗・止まった理由）を辞書で返す"""
    started = datetime.now()
    summary = {"started_at": started.isoformat(timespec="seconds"), "targets": [], "errors": [], "fatal": None}
    try:
        if not args.config.exists():
            sys.exit(f"{args.config} がありません。config.example.toml をコピーして作成してください。")
        config = load_config(args.config)
        for _, target in active_targets(config):  # 名前の重複などの誤りは、Drive につなぐ前に見つける
            target_options(target)

        creds = Credentials.from_authorized_user_file(config["token_file"], SCOPES)
        service = build("drive", "v3", credentials=creds)

        for kind, target, out_dir, entries, complete in prepare_targets(service, config, summary["errors"]):
            if kind == "folder":
                print(f"=== {target['name']} → {out_dir} ===")
                entries, complete = list_tree(service, target["folder_id"], target.get("recursive", True))
                if not complete:
                    summary["errors"].append(f"{target['name']}: 一覧を最後まで取れなかった")
            else:
                print(f"=== {target['name']}（個別ファイル） → {out_dir} ===")
            stats = sync_target(service, kind, target["name"], out_dir, entries, complete, config, args.full,
                                target_options(target))
            print_summary(stats)
            summary["targets"].append({
                "name": target["name"], "kind": kind,
                **{k: stats[k] for k in ("new", "updated", "unchanged", "moved", "archived")},
                "skipped": len(stats["skipped"]), "failed": stats["failed"], "pdf_fallback": stats["pdf_fallback"],
            })
        summary["archive"] = clean_archive(config, args.archive_max_mb, started, summary["errors"])
    except (Exception, SystemExit) as e:  # noqa: BLE001 - 結果に理由を残してから、もう一度投げる
        summary["fatal"] = describe_fatal(e)
        print(f"同期が止まりました: {summary['fatal']}", file=sys.stderr)
        raise
    finally:
        summary["finished_at"] = datetime.now().isoformat(timespec="seconds")
        if args.summary:
            args.summary.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description="Google Drive の資料を AI 向けの Markdown に同期する")
    parser.add_argument("--full", action="store_true", help="変更の有無にかかわらず全部作り直す")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help=f"設定ファイル（既定: {DEFAULT_CONFIG}）")
    parser.add_argument("--summary", type=Path, help="結果を JSON で書き出す場所（管理画面が読む）")
    parser.add_argument("--archive-max-mb", type=int, metavar="MB",
                        help="退避（archive/）の合計の上限。超えたら古い退避から消す（既定: 消さない）")
    args = parser.parse_args()

    # 作るファイルの権限（NAS のコンテナでは 0002 にして、グループのメンバーも書き換えられるようにする）
    if os.environ.get("CDS_UMASK"):
        os.umask(int(os.environ["CDS_UMASK"], 8))
    run(args)


if __name__ == "__main__":
    main()
