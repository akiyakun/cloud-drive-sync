"""同期の記録（state）と、出力の移動・退避

state/<同期先の名前>.json に、ファイルごとの記録を残す:
    {
      "converter_version": 1,
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
"""

import json
import os
import shutil
from datetime import datetime
from pathlib import Path, PurePosixPath


class SyncState:
    def __init__(self, path, converter_version):
        self.path = Path(path)
        data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        self.files = data.get("files", {})
        # 変換処理の版が前回と違えば、Drive 側が変わっていなくても全部作り直す
        self.version_changed = bool(self.files) and data.get("converter_version") != converter_version
        self.converter_version = converter_version

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "converter_version": self.converter_version,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "files": self.files,
        }
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)


class Archiver:
    """出力を archive/<同期先の名前>/<実行日時>/ の下へ、出力先と同じ並びのまま移す"""

    def __init__(self, archive_root, target_name, out_dir):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
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


def outputs_exist(out_dir, outputs):
    return all((Path(out_dir) / rel).exists() for rel in outputs)


def remove_empty_dirs(out_dir):
    """移動・退避で空になったフォルダを片付ける（出力先そのものは残す）"""
    out_dir = Path(out_dir)
    if not out_dir.exists():
        return
    for dirpath, _, _ in sorted(os.walk(out_dir), key=lambda w: -len(w[0])):
        path = Path(dirpath)
        if path != out_dir and not any(path.iterdir()):
            path.rmdir()


def untracked_files(out_dir, files):
    """記録に載っていない出力（以前の版の出力など）を探す"""
    out_dir = Path(out_dir)
    tracked = {rel for entry in files.values() for rel in entry.get("outputs", [])}
    found = []
    if not out_dir.exists():
        return found
    for path in sorted(out_dir.rglob("*")):
        if path.name.startswith(".") or not path.is_file():
            continue
        rel = path.relative_to(out_dir).as_posix()
        # 記録された出力そのもの、または記録された .images フォルダの中身なら対象外
        if rel in tracked or any(rel.startswith(t + "/") for t in tracked):
            continue
        found.append(rel)
    return found
