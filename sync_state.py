"""同期の記録（state）と、出力の移動・退避

state/<同期先の名前>.json に、ファイルごとの記録を残す:
    {
      "converter_version": 1,
      "out_dir": "出力先（前回の同期のとき）",
      "files": {
        "<Drive のファイル ID>": {
          "name": "Drive 上の名前",
          "path": "出力先の中の場所（拡張子なし）。例: doc/仕様書/画面仕様",
          "mimeType": "...",
          "modifiedTime": "Drive 上の最終更新日時",
          "outputs": ["doc/仕様書/画面仕様.md", "doc/仕様書/画面仕様.images", ...]
        }
      }
    }

Drive から消えたファイルの出力は、AI が読む場所（output/）の外にある archive/ へ移す（消さない）。
退避の合計が上限を超えたら、古い退避から消せる（prune_archive。管理画面で ON にしたときだけ）。
"""

import json
import os
import re
import shutil
import unicodedata
from datetime import datetime
from pathlib import Path, PurePosixPath


class SyncState:
    def __init__(self, path, converter_version):
        self.path = Path(path)
        data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        self.files = data.get("files", {})
        self.out_dir = data.get("out_dir")  # 前回の出力先。変わっていたら出力を引っ越す
        # 変換処理の版が前回と違えば、Drive 側が変わっていなくても全部作り直す
        self.version_changed = bool(self.files) and data.get("converter_version") != converter_version
        self.converter_version = converter_version

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "converter_version": self.converter_version,
            "out_dir": self.out_dir,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "files": self.files,
        }
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)


STAMP_FORMAT = "%Y%m%d-%H%M%S"
STAMP_PATTERN = re.compile(r"\d{8}-\d{6}")


class Archiver:
    """出力を archive/<同期先の名前>/<実行日時>/ の下へ、出力先と同じ並びのまま移す"""

    def __init__(self, archive_root, target_name, out_dir):
        stamp = datetime.now().strftime(STAMP_FORMAT)
        self.base = Path(archive_root) / target_name / stamp
        self.out_dir = Path(out_dir)

    def archive(self, rel):
        """出力先の中の rel（ファイルまたはフォルダ）を退避する。無ければ何もしない"""
        src = self.out_dir / rel
        if not src.exists():
            return False
        dst = self.base / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        n = 1
        while dst.exists():  # 同じ実行で同じ名前を退避したとき
            dst = dst.with_name(f"{dst.name}.{n}")
            n += 1
        shutil.move(str(src), str(dst))
        return True


def move_outputs(out_dir, old_path, new_path, outputs, archiver):
    """名前変更・移動されたファイルの出力を、新しい名前・場所へ移す

    出力の名前は「<元の名前><後ろの部分>」（例: 画面仕様.md、画面仕様.images）なので、前の部分だけを差し替える。
    移動先に別のものがあれば、先にそれを退避する。戻り値は新しい outputs。
    """
    out_dir = Path(out_dir)
    old_base, new = PurePosixPath(old_path).name, PurePosixPath(new_path)
    moved = []
    for rel in outputs:
        name = PurePosixPath(rel).name
        suffix = name[len(old_base):] if name.startswith(old_base) else name
        new_rel = str(new.parent / f"{new.name}{suffix}")
        src, dst = out_dir / rel, out_dir / new_rel
        if src.exists() and new_rel != rel:
            if dst.exists():
                archiver.archive(new_rel)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
        moved.append(new_rel)
    return moved


def relocate_outputs(old_dir, new_dir, files):
    """出力先が変わったとき（設定の変更や、Drive のフォルダ名の変更）に、前の出力を新しい出力先へ移す

    新しい出力先に同じ名前のものがあれば上書きせず、そのままにする（次の同期で作り直される）。戻り値は移した数。
    """
    old_dir, new_dir = Path(old_dir), Path(new_dir)
    moved = 0
    for entry in files.values():
        for rel in entry.get("outputs", []):
            src, dst = old_dir / rel, new_dir / rel
            if src.exists() and not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                moved += 1
    return moved


def outputs_exist(out_dir, outputs):
    return all((Path(out_dir) / rel).exists() for rel in outputs)


def remove_empty_dirs(out_dir, keep_root=True):
    """移動・退避で空になったフォルダを片付ける（keep_root なら出力先そのものは残す）"""
    out_dir = Path(out_dir)
    if not out_dir.exists():
        return
    for dirpath, _, _ in sorted(os.walk(out_dir), key=lambda w: -len(w[0])):
        path = Path(dirpath)
        if (path != out_dir or not keep_root) and not any(path.iterdir()):
            path.rmdir()


def untracked_files(out_dir, files):
    """記録に載っていない出力（以前の版の出力など）を探す"""
    out_dir = Path(out_dir)
    # Mac から NAS の共有フォルダを見ると、濁点などが分かれた形（NFD）の名前が返るので、NFC にそろえて比べる
    tracked = {unicodedata.normalize("NFC", rel) for entry in files.values() for rel in entry.get("outputs", [])}
    found = []
    if not out_dir.exists():
        return found
    for path in sorted(out_dir.rglob("*")):
        if path.name.startswith(".") or not path.is_file():
            continue
        rel = unicodedata.normalize("NFC", path.relative_to(out_dir).as_posix())
        # 記録された出力そのもの、または記録された .images フォルダの中身なら対象外
        if rel in tracked or any(rel.startswith(t + "/") for t in tracked):
            continue
        found.append(rel)
    return found


def dir_size(path):
    """フォルダの中のファイルの大きさの合計（バイト）。リンクはたどらない"""
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass  # 調べている間に消えたもの
    return total


def prune_archive(archive_root, max_bytes, keep_since=None):
    """退避の合計が max_bytes を超えていたら、古い退避（archive/<同期先>/<実行日時>/）から順に消す

    - max_bytes が None なら消さずに、大きさだけを調べる
    - keep_since（実行日時の文字列。例 20261012-183600）より新しい退避は、上限を超えていても消さない
      （今回の同期で退避したものを、すぐに消してしまわないため）
    - 実行日時の形でないフォルダ・ファイルは消さない（大きさには数える）
    戻り値: {"size": 消したあとの合計, "removed": 消した退避の数, "removed_bytes": 消した大きさ, "errors": [...]}
    """
    root = Path(archive_root)
    result = {"size": 0, "removed": 0, "removed_bytes": 0, "errors": []}
    if not root.is_dir():
        return result
    batches = []
    for target in root.iterdir():
        if not target.is_dir() or target.is_symlink():
            continue
        for batch in target.iterdir():
            if batch.is_dir() and not batch.is_symlink() and STAMP_PATTERN.fullmatch(batch.name):
                batches.append((batch.name, target.name, batch))
    total = dir_size(root)
    if max_bytes is not None:
        for stamp, _, batch in sorted(batches):  # 実行日時の古い順
            if total <= max_bytes or (keep_since and stamp >= keep_since):
                break
            size = dir_size(batch)
            try:
                shutil.rmtree(batch)
            except OSError as e:
                result["errors"].append(f"{batch.parent.name}/{batch.name}: {type(e).__name__}")
                continue
            total -= size
            result["removed"] += 1
            result["removed_bytes"] += size
            if not any(batch.parent.iterdir()):
                batch.parent.rmdir()
    result["size"] = total
    return result
