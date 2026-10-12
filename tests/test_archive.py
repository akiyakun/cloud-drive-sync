"""退避の自動削除（sync_state.prune_archive）のテスト

実行: .venv/bin/python -m unittest discover -s tests -v
"""

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sync import clean_archive  # noqa: E402
import sync_state  # noqa: E402
from sync_state import prune_archive, remove_tree  # noqa: E402


class PruneArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "archive"

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, rel, size):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)

    def test_no_limit_only_measures(self):
        self.put("a/20261001-090000/doc/x.md", 300)
        result = prune_archive(self.root, None)
        self.assertEqual((result["size"], result["removed"]), (300, 0))
        self.assertTrue((self.root / "a/20261001-090000").exists())

    def test_removes_oldest_first_across_targets_until_under_limit(self):
        self.put("a/20261001-090000/x.md", 100)   # いちばん古い
        self.put("b/20261002-090000/y.md", 100)
        self.put("a/20261003-090000/z.md", 100)
        result = prune_archive(self.root, 150)
        self.assertEqual((result["size"], result["removed"], result["removed_bytes"]), (100, 2, 200))
        self.assertFalse((self.root / "a/20261001-090000").exists())
        self.assertFalse((self.root / "b").exists())  # 空になった同期先のフォルダも片付ける
        self.assertTrue((self.root / "a/20261003-090000/z.md").exists())

    def test_keeps_batches_from_this_run(self):
        self.put("a/20261001-090000/x.md", 100)
        self.put("a/20261012-180000/y.md", 500)  # 今回の同期で退避したもの
        result = prune_archive(self.root, 100, keep_since="20261012-175900")
        self.assertEqual((result["removed"], result["size"]), (1, 500))  # 上限を超えていても残す
        self.assertTrue((self.root / "a/20261012-180000/y.md").exists())

    def test_under_limit_removes_nothing(self):
        self.put("a/20261001-090000/x.md", 100)
        self.assertEqual(prune_archive(self.root, 1000)["removed"], 0)

    def test_ignores_folders_not_named_by_date(self):
        self.put("a/memo/keep.txt", 500)
        self.put("a/20261001-090000/x.md", 100)
        result = prune_archive(self.root, 10)
        self.assertEqual((result["removed"], result["size"]), (1, 500))
        self.assertTrue((self.root / "a/memo/keep.txt").exists())

    def test_missing_root(self):
        self.assertEqual(prune_archive(self.root, 10)["size"], 0)



class RemoveTreeTests(unittest.TestCase):
    def test_falls_back_without_following_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            keep = root / "keep"
            keep.mkdir()
            (keep / "important.txt").write_text("x")
            victim = root / "victim"
            (victim / "sub").mkdir(parents=True)
            (victim / "sub" / "a.png").write_bytes(b"1")
            (victim / "link").symlink_to(keep, target_is_directory=True)
            original = sync_state.shutil.rmtree

            def failing_rmtree(path, *args, **kwargs):  # SMB で「リンクかもしれない」と止まった場合
                raise OSError("Cannot call rmtree on a symbolic link")

            sync_state.shutil.rmtree = failing_rmtree
            try:
                remove_tree(victim)
            finally:
                sync_state.shutil.rmtree = original
            self.assertFalse(victim.exists())
            self.assertTrue((keep / "important.txt").exists())  # リンクの先は消さない


class CleanArchiveTests(unittest.TestCase):
    """sync.py の同期のあとの処理（--archive-max-mb の MB を、今回の同期の開始を残して適用する）"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "archive"
        for rel, size in [("a/20261001-090000/x.bin", 2 * 1024 * 1024), ("a/20261012-180005/y.bin", 2 * 1024 * 1024)]:
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.root / rel).write_bytes(b"x" * size)
        self.config = {"archive_root": str(self.root)}
        self.started = datetime(2026, 10, 12, 18, 0, 0)  # 2 つ目は、この同期で退避したもの

    def tearDown(self):
        self.tmp.cleanup()

    def test_limit_in_mb_keeps_this_run(self):
        errors = []
        result = clean_archive(self.config, 1, self.started, errors)
        self.assertEqual((result["removed"], result["size"], errors), (1, 2 * 1024 * 1024, []))
        self.assertTrue((self.root / "a/20261012-180005/y.bin").exists())

    def test_without_limit_only_measures(self):
        for limit in (None, 0, -5):
            result = clean_archive(self.config, limit, self.started, [])
            self.assertEqual((result["removed"], result["max_bytes"]), (0, None))


if __name__ == "__main__":
    unittest.main()
