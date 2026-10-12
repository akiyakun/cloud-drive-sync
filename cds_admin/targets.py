"""同期先（config.toml の [[folders]]・[[files]]）を管理画面から編集する

- 設定ファイルは書き換えるたびに作り直す（コメントは残らない）。直前の内容は config.toml.bak に残す
- 名前を変えたら、同期の記録（state/<名前>.json）と退避（archive/<名前>/）の名前も変える
- 同期先を削除したら、その同期先の出力を退避（archive/<名前>/<実行日時>/）へ移し、記録を消す
- 出力先が変わったときの出力の引っ越しは、次の同期で sync.py が行う（記録の out_dir と比べる）
"""

import json
import os
import re
import shutil
import tomllib
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import sync
from sync_state import Archiver, remove_empty_dirs

KINDS = {"folder": "folders", "file": "files"}
KIND_LABELS = {"folder": "フォルダ", "file": "ファイル"}
FOLDER_MIME = "application/vnd.google-apps.folder"
MAX_NAME_LENGTH = 64
DRIVE_ID = re.compile(r"[A-Za-z0-9_-]{10,200}")
BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
HEADER = (
    "# CloudDrive-Sync の設定。管理画面の「同期先」で保存すると、このファイルは作り直される（コメントは残らない）\n"
    "# 直前の内容は config.toml.bak に残る。書き方はリポジトリの config.example.toml を参照\n"
)


# --- 入力のチェック ---

def parse_name(text):
    name = (text or "").strip()
    if not name:
        raise ValueError("名前を入れてください")
    if len(name) > MAX_NAME_LENGTH:
        raise ValueError(f"名前は {MAX_NAME_LENGTH} 文字までです")
    if name.startswith(".") or sync.safe_name(name) != name:
        raise ValueError('名前に使えない文字があります（\\ / : * ? " < > | や、先頭の . は使えません）')
    return name


def parse_drive_id(text):
    """Drive の URL か ID から、ID を取り出す

    例: https://drive.google.com/drive/folders/<ID>、https://docs.google.com/spreadsheets/d/<ID>/edit、?id=<ID>
    """
    text = (text or "").strip()
    if DRIVE_ID.fullmatch(text):
        return text
    url = urlparse(text)
    if url.scheme in ("http", "https") and url.hostname in ("drive.google.com", "docs.google.com"):
        parts = [p for p in url.path.split("/") if p]
        for marker in ("folders", "d"):
            if marker in parts and parts.index(marker) + 1 < len(parts):
                candidate = parts[parts.index(marker) + 1]
                if DRIVE_ID.fullmatch(candidate):
                    return candidate
        for candidate in parse_qs(url.query).get("id", []):
            if DRIVE_ID.fullmatch(candidate):
                return candidate
    raise ValueError("Drive の URL か ID を入れてください（例: https://drive.google.com/drive/folders/…）")


def parse_output(text):
    """出力先（出力の土台から見た場所）。空なら None（同期先の名前と同じ場所になる）"""
    text = (text or "").strip().replace("\\", "/").strip("/")
    if not text:
        return None
    for part in text.split("/"):
        plain = part.replace("{drive_name}", "x")
        if not part or part in (".", "..") or "{" in plain or "}" in plain or sync.safe_name(plain) != plain:
            raise ValueError(f"出力先の「{part}」は使えません（使えない文字か、. や .. があります。"
                             "Drive 上の名前を使うときは {drive_name} と書きます）")
    return text


# --- 設定ファイル ---

def render_toml(data):
    """設定を TOML の文字にする（使うのは 文字・数・真偽・文字の並び・表の並び だけ）"""
    lines = [HEADER]
    tables = []
    for key, value in data.items():
        if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
            tables.append((key, value))
        elif isinstance(value, dict):
            raise ValueError(f"設定の「{key}」は書き出せない形です")
        else:
            lines.append(f"{_key(key)} = {_value(value)}")
    for key, items in tables:
        for item in items:
            lines.append("")
            lines.append(f"[[{_key(key)}]]")
            for k, v in item.items():
                lines.append(f"{_key(k)} = {_value(v)}")
    return "\n".join(lines) + "\n"


def _key(key):
    return key if BARE_KEY.fullmatch(key) else json.dumps(key, ensure_ascii=False)


def _value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)  # TOML の基本文字列と同じ書き方になる
    if isinstance(value, list) and all(isinstance(v, (str, bool, int, float)) for v in value):
        return "[" + ", ".join(_value(v) for v in value) + "]"
    raise ValueError(f"設定の値 {value!r} は書き出せない形です")


class ConfigFile:
    def __init__(self, path):
        self.path = Path(path)

    def raw(self):
        """書いてあるとおりの設定（環境変数での上書き前）"""
        return tomllib.loads(self.path.read_text(encoding="utf-8"))

    def effective(self):
        """同期で使う設定（場所は環境変数で上書き済み）"""
        return sync.load_config(self.path)

    def save(self, data):
        text = render_toml(data)
        if tomllib.loads(text) != data:  # 書き出した内容を読み直して、同じになることを確かめる
            raise ValueError("設定を正しく書き出せませんでした")
        if self.path.exists():
            shutil.copyfile(self.path, self.path.with_name(self.path.name + ".bak"))
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self.path)


def list_targets(data):
    """[(種類, 同期先), ...]（設定に書いてある順）"""
    return [(kind, target) for kind, key in KINDS.items() for target in data.get(key, [])]


def find_target(data, name):
    for kind, target in list_targets(data):
        if target["name"] == name:
            return kind, target
    return None, None


def is_active(target):
    environments = target.get("environments")
    return environments is None or sync.ENVIRONMENT in environments


def check_unique(data, name, ignore=None):
    """名前の重複を確かめる。記録のファイル名に使うので、大文字・小文字の違いも同じとみなす"""
    key = sync.safe_name(name).casefold()
    for _, target in list_targets(data):
        if target is not ignore and sync.safe_name(target["name"]).casefold() == key:
            raise ValueError(f"「{target['name']}」という同期先がすでにあります")


def missing_drive_names(config, data, drive_names):
    """出力先を確かめるのに、Drive 上の名前が必要な同期先（{drive_name} を使い、まだ同期していない）[(名前, ID), ...]"""
    needed = []
    for kind, target in list_targets(data):
        if (is_active(target) and target["name"] not in drive_names
                and "{drive_name}" in target.get("output", "")
                and not sync.load_state(config, target["name"]).out_dir):
            needed.append((target["name"], target.get("folder_id" if kind == "folder" else "file_id")))
    return needed


def check_out_dirs(config, data, drive_names):
    """出力先どうしが混ざらないか確かめる（sync.py が同期の前に確かめるのと同じ決まり）

    {drive_name} を使う同期先の Drive 上の名前は drive_names から、無ければ前回の同期の出力先を使う
    """
    resolved = []
    for kind, target in list_targets(data):
        if not is_active(target):
            continue
        out_dir = sync.resolve_out_dir(config, target, drive_names.get(target["name"]))
        if out_dir is None:
            previous = sync.load_state(config, target["name"]).out_dir
            if not previous:
                continue  # まだ一度も同期していない: 同期のときに確かめる
            out_dir = Path(previous)
        resolved.append((kind, target, out_dir))
    try:
        sync.check_out_dirs(resolved)
    except SystemExit as e:
        raise ValueError(str(e).removeprefix("設定エラー: ")) from None


# --- Drive ---

def lookup_drive(config, drive_id):
    """Drive 上の名前と種類を調べる。読めなければ ValueError"""
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    try:
        creds = Credentials.from_authorized_user_file(config["token_file"], sync.SCOPES)
        service = build("drive", "v3", credentials=creds, cache_discovery=False)
        return service.files().get(fileId=drive_id, fields="id, name, mimeType", supportsAllDrives=True).execute()
    except HttpError as e:
        if e.status_code == 404:
            raise ValueError("Drive で見つかりません（ID がちがうか、このアカウントに共有されていません）") from None
        raise ValueError(f"Drive に問い合わせできませんでした（{sync.describe_error(e)}）") from None
    except Exception as e:  # noqa: BLE001 - 鍵の期限切れ・通信エラーなど
        raise ValueError(f"Drive に問い合わせできませんでした（{sync.describe_fatal(e)}）") from None


def check_drive_kind(kind, item):
    is_folder = item.get("mimeType") == FOLDER_MIME
    if kind == "folder" and not is_folder:
        raise ValueError(f"「{item.get('name')}」はフォルダではありません（ファイルは「ファイルを追加」から足します）")
    if kind == "file" and is_folder:
        raise ValueError(f"「{item.get('name')}」はフォルダです（フォルダは「フォルダを追加」から足します）")


# --- 名前の変更・削除の後始末 ---

def _state_path(config, name):
    return Path(config.get("state_root", "state")) / f"{sync.safe_name(name)}.json"


def _archive_dir(config, name):
    return Path(config.get("archive_root", "archive")) / sync.safe_name(name)


def rename_records(config, old, new):
    """同期の記録と退避の名前を変える。戻り値は元に戻す関数

    新しい名前の記録がすでにあると（前に同じ名前の同期先があった跡）、それを引き継いでしまうので変えない。
    退避は、新しい名前のものだけがあるなら、そのまま使う（中身は日時ごとのフォルダなので混ざっても困らない）
    """
    state_src, state_dst = _state_path(config, old), _state_path(config, new)
    archive_src, archive_dst = _archive_dir(config, old), _archive_dir(config, new)
    # 大文字・小文字だけの変更は、同じファイルとして扱われることがある（Mac・NAS の共有フォルダ）
    if state_dst.exists() and not _same_file(state_src, state_dst):
        raise ValueError(f"「{new}」の同期の記録が残っているため、この名前にはできません（前に同じ名前の同期先があった跡です）")
    if archive_src.exists() and archive_dst.exists() and not _same_file(archive_src, archive_dst):
        raise ValueError(f"「{old}」と「{new}」の両方の退避があるため、この名前にはできません")
    pairs = [(src, dst) for src, dst in ((state_src, state_dst), (archive_src, archive_dst)) if src.exists() and src != dst]
    done = []
    for src, dst in pairs:
        _move(src, dst)
        done.append((src, dst))

    def undo():
        for src, dst in reversed(done):
            _move(dst, src)
    return undo


def _same_file(a, b):
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _move(src, dst):
    if _same_file(src, dst):  # 大文字・小文字だけの変更: いったん別の名前を経由する
        tmp = src.with_name(f".{src.name}.renaming")
        os.replace(src, tmp)
        os.replace(tmp, dst)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))


def retire_outputs(config, kind, name):
    """削除した同期先の出力を退避へ移し、同期の記録を消す。戻り値は退避した数"""
    state = sync.load_state(config, name)
    archived = 0
    if state.out_dir:
        archiver = Archiver(config.get("archive_root", "archive"), sync.safe_name(name), state.out_dir)
        for entry in state.files.values():
            for rel in entry.get("outputs", []):
                archived += archiver.archive(rel)
        if kind == "folder":
            remove_empty_dirs(state.out_dir, keep_root=False)  # 記録にないファイルがあれば、そのフォルダは残る
        else:
            try:
                Path(state.out_dir).rmdir()  # 空になったときだけ消える
            except OSError:
                pass
    state.path.unlink(missing_ok=True)
    return archived


def summarize(config, kind, target):
    """一覧・削除の確認に出す情報（前回の出力先・記録にあるファイルの数）"""
    state = sync.load_state(config, target["name"])
    return {"out_dir": state.out_dir, "files": len(state.files)}
