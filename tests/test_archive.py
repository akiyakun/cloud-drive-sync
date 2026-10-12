"""退避の自動削除（sync_state.prune_archive）のテスト

実行: .venv/bin/python -m unittest discover -s tests -v
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sync_state import prune_archive  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
