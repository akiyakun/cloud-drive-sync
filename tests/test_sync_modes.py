"""同期先ごとの出力のしかた（AI 用に変換する・元のファイルも残す・変換しない）のテスト。Drive にはつながない

実行: .venv/bin/python -m unittest discover -s tests -v
"""

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

import openpyxl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sync  # noqa: E402


def xlsx_bytes(value="こんにちは"):
    book = openpyxl.Workbook()
    book.active["A1"] = value
    buf = io.BytesIO()
    book.save(buf)
    return buf.getvalue()


def item(file_id, name, mime, modified="2026-10-01T00:00:00Z"):
    return {"id": file_id, "name": name, "mimeType": mime, "modifiedTime": modified,
            "webViewLink": f"https://example.test/{file_id}"}


ENTRIES = [
    (item("doc1", "仕様", sync.DOC_MIME), "仕様"),
    (item("sheet1", "進行表", sync.SHEET_MIME), "進行表"),
    (item("slide1", "説明", sync.SLIDES_MIME), "説明"),
    (item("form1", "アンケート", "application/vnd.google-apps.form"), "アンケート"),
    (item("up1", "予算.xlsx", sync.XLSX_MIME), "予算.xlsx"),
    (item("pdf1", "見積.pdf", "application/pdf"), "見積.pdf"),
]


class SyncModesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.out = root / "out"
        self.out.mkdir()
        self.config = {"output_root": str(root), "state_root": str(root / "state"), "archive_root": str(root / "archive")}
        self.exports = []
        self.originals = {
            "export": sync.export, "download_media": sync.download_media, "save_doc_markdown": sync.save_doc_markdown,
        }

        def fake_export(service, file_id, mime):
            self.exports.append((file_id, mime))
            if mime == sync.XLSX_MIME:
                return xlsx_bytes()
            return f"{file_id}:{mime}".encode()

        def fake_download(service, file_id, dest):
            Path(dest).write_bytes(self.uploads.get(file_id, b"%PDF original"))

        self.uploads = {"up1": xlsx_bytes("アップロード")}

        def fake_doc_markdown(service, file_id, out_dir, name):
            return sync.write_markdown(out_dir, name, "# ドキュメント\n", [])

        sync.export, sync.download_media, sync.save_doc_markdown = fake_export, fake_download, fake_doc_markdown

    def tearDown(self):
        for key, value in self.originals.items():
            setattr(sync, key, value)
        self.tmp.cleanup()

    def run_sync(self, ai=True, keep=False, entries=ENTRIES):
        return sync.sync_target(None, "folder", "t", self.out, entries, True, self.config, False,
                                {"ai_markdown": ai, "keep_originals": keep})

    def files(self):
        return sorted(p.relative_to(self.out).as_posix() for p in self.out.rglob("*") if p.is_file())

    def archived(self):
        root = Path(self.config["archive_root"])
        return sorted(p.name for p in root.rglob("*") if p.is_file()) if root.exists() else []

    def test_ai_without_originals(self):
        stats = self.run_sync()
        self.assertEqual(self.files(), sorted(["仕様.md", "見積.pdf", "説明.pdf", "進行表.md", "予算.xlsx.md"]))
        self.assertIn("アップロード", (self.out / "予算.xlsx.md").read_text())  # アップロードされた Excel も Markdown に
        self.assertEqual(stats["skipped"], ["アンケート"])  # フォームは AI 用に変換できない
        self.assertNotIn(("slide1", sync.PPTX_MIME), self.exports)

    def test_ai_with_originals(self):
        self.run_sync(keep=True)
        self.assertEqual(self.files(), sorted(["仕様.docx", "仕様.md", "見積.pdf", "説明.pdf", "説明.pptx", "進行表.md",
                                        "進行表.xlsx", "予算.xlsx", "予算.xlsx.md"]))

    def test_without_conversion_makes_shortcuts(self):
        stats = self.run_sync(ai=False)
        self.assertEqual(self.files(), sorted(["アンケート.url", "仕様.url", "見積.pdf", "説明.url", "進行表.url", "予算.xlsx"]))
        self.assertEqual((self.out / "仕様.url").read_bytes(), b"[InternetShortcut]\r\nURL=https://example.test/doc1\r\n")
        self.assertEqual(self.exports, [])  # Google 形式は書き出さない
        self.assertEqual(stats["skipped"], [])

    def test_changing_options_rebuilds_and_archives(self):
        self.run_sync(keep=True)
        self.run_sync(keep=False)  # Drive 側は変わっていなくても、作り直す
        self.assertNotIn("進行表.xlsx", self.files())
        self.assertEqual(self.archived(), sorted(["仕様.docx", "説明.pptx", "進行表.xlsx", "予算.xlsx"]))
        self.run_sync(ai=False)
        self.assertIn("仕様.url", self.files())
        self.assertNotIn("仕様.md", self.files())
        stats = self.run_sync(ai=False)
        self.assertEqual(stats["unchanged"], 6)  # 同じ設定なら作り直さない

    def test_state_from_before_options_is_rebuilt(self):
        # 出力のしかたを記録する前の版: スプレッドシートの .xlsx を残していた
        self.run_sync(keep=True)
        state_path = Path(self.config["state_root"]) / "t.json"
        data = json.loads(state_path.read_text())
        del data["options"]
        state_path.write_text(json.dumps(data))
        self.run_sync()
        self.assertNotIn("進行表.xlsx", self.files())
        self.assertIn("進行表.xlsx", self.archived())

    def test_broken_uploaded_excel_keeps_original(self):
        self.uploads["up1"] = b"not an xlsx"
        stats = self.run_sync(entries=[(item("up1", "壊れ.xlsx", sync.XLSX_MIME), "壊れ.xlsx")])
        self.assertEqual(self.files(), sorted(["壊れ.xlsx"]))
        self.assertIn("原本のまま", stats["pdf_fallback"][0])
        self.assertEqual(stats["failed"], [])

    def test_failed_file_is_retried_after_rebuild(self):
        self.run_sync(keep=True)
        real = sync.sync_file

        def flaky(service, it, *args, **kwargs):
            return None if it["id"] == "sheet1" else real(service, it, *args, **kwargs)  # 一時的な失敗

        sync.sync_file = flaky
        try:
            self.run_sync(keep=False)
        finally:
            sync.sync_file = real
        self.assertIn("進行表.xlsx", self.files())
        stats = self.run_sync(keep=False)  # 次の同期で、失敗したファイルだけを作り直す
        self.assertNotIn("進行表.xlsx", self.files())
        self.assertEqual((stats["updated"], stats["unchanged"]), (1, 4))

    def test_state_from_v050_is_not_rebuilt(self):
        # v0.5.0 の記録（同期先全体の options はあるが、ファイルごとの mode は無い）
        self.run_sync()
        state_path = Path(self.config["state_root"]) / "t.json"
        data = json.loads(state_path.read_text())
        for entry in data["files"].values():
            del entry["mode"]
        state_path.write_text(json.dumps(data))
        self.assertEqual(self.run_sync()["unchanged"], 5)

    def test_generated_names_do_not_overwrite_uploads(self):
        entries = [(item("slideABCDEF1", "説明", sync.SLIDES_MIME), "説明"),
                   (item("pdf1", "説明.PDF", "application/pdf"), "説明.PDF")]
        names = sync.assign_names([e[0] for e in entries])
        self.assertEqual(names, {"slideABCDEF1": "説明_slideA", "pdf1": "説明.PDF"})
        # ぶつからなければ、名前は変えない
        self.assertEqual(sync.assign_names([entries[0][0]]), {"slideABCDEF1": "説明"})

    def test_large_uploaded_excel_is_kept_as_is(self):
        original = sync.MAX_EXCEL_BYTES
        sync.MAX_EXCEL_BYTES = 10
        try:
            stats = self.run_sync(entries=[(item("up1", "大きい.xlsx", sync.XLSX_MIME), "大きい.xlsx")])
        finally:
            sync.MAX_EXCEL_BYTES = original
        self.assertEqual(self.files(), ["大きい.xlsx"])
        self.assertIn("大きいため", stats["pdf_fallback"][0])
        self.assertEqual([p.name for p in self.out.iterdir() if p.name.startswith(".")], [])  # 一時ファイルは残さない

    def test_switching_to_ai_archives_unsupported_shortcuts(self):
        self.run_sync(ai=False)
        stats = self.run_sync(ai=True)
        self.assertNotIn("アンケート.url", self.files())
        self.assertIn("アンケート.url", self.archived())
        self.assertEqual(stats["skipped"], ["アンケート"])

    def test_options_validation(self):
        self.assertEqual(sync.target_options({"name": "x"}), {"ai_markdown": True, "keep_originals": False})
        self.assertEqual(sync.target_options({"name": "x", "ai_markdown": False, "keep_originals": True}),
                         {"ai_markdown": False, "keep_originals": False})
        with self.assertRaises(SystemExit):
            sync.target_options({"name": "x", "ai_markdown": "yes"})


if __name__ == "__main__":
    unittest.main()
