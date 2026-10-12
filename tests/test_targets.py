"""同期先の編集（cds_admin/targets.py と管理画面）のテスト。Drive にはつながない

実行: .venv/bin/python -m unittest discover -s tests -v
"""

import json
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cds_admin import targets  # noqa: E402
from cds_admin.app import create_app  # noqa: E402
from test_admin import FakeNotifier, csrf_of  # noqa: E402

FOLDER = "application/vnd.google-apps.folder"
SHEET = "application/vnd.google-apps.spreadsheet"
DRIVE = {
    "folderAAAAAAAA": {"name": "共有フォルダ", "mimeType": FOLDER},
    "folderBBBBBBBB": {"name": "別のフォルダ", "mimeType": FOLDER},
    "sheetCCCCCCCCC": {"name": "進行表", "mimeType": SHEET},
}


def fake_lookup(config, drive_id):
    if drive_id not in DRIVE:
        raise ValueError("Drive で見つかりません（ID がちがうか、このアカウントに共有されていません）")
    return {"id": drive_id, **DRIVE[drive_id]}


class ParseTests(unittest.TestCase):
    def test_drive_id_from_url_or_id(self):
        for text in ["folderAAAAAAAA",
                     "https://drive.google.com/drive/folders/folderAAAAAAAA",
                     "https://drive.google.com/drive/u/0/folders/folderAAAAAAAA?usp=sharing",
                     "https://docs.google.com/spreadsheets/d/folderAAAAAAAA/edit#gid=0",
                     "https://drive.google.com/open?id=folderAAAAAAAA"]:
            self.assertEqual(targets.parse_drive_id(text), "folderAAAAAAAA", text)
        for bad in ["", "short", "https://example.com/folders/folderAAAAAAAA", "abc def ghijklmn"]:
            with self.assertRaises(ValueError, msg=bad):
                targets.parse_drive_id(bad)

    def test_name(self):
        self.assertEqual(targets.parse_name(" 案件A "), "案件A")
        for bad in ["", "a/b", ".hidden", "x" * 65, "a:b"]:
            with self.assertRaises(ValueError, msg=bad):
                targets.parse_name(bad)

    def test_output(self):
        self.assertIsNone(targets.parse_output("  "))
        self.assertEqual(targets.parse_output("/案件A/GoogleDrive-AI/{drive_name}/"), "案件A/GoogleDrive-AI/{drive_name}")
        self.assertEqual(targets.parse_output("a\\b"), "a/b")
        for bad in ["../x", "a/../b", "a//b", "a/{other}", "a/b:c", "a/./b"]:
            with self.assertRaises(ValueError, msg=bad):
                targets.parse_output(bad)

    def test_render_toml_round_trip(self):
        data = {"token_file": "/secrets/token.json", "output_root": "/output",
                "folders": [{"name": "案件\"A\"", "output": "a\\b/{drive_name}", "folder_id": "x", "recursive": False,
                             "environments": ["dev"]}],
                "files": [{"name": "f", "file_id": "y"}]}
        text = targets.render_toml(data)
        self.assertEqual(tomllib.loads(text), data)
        self.assertTrue(text.startswith("# CloudDrive-Sync の設定"))
        self.assertEqual(tomllib.loads(targets.render_toml({"folders": []})), {"folders": []})


class TargetPagesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.output, self.state, self.archive = root / "output", root / "state", root / "archive"
        self.config_path = root / "config.toml"
        self.config_path.write_text(
            f'# 手で書いた設定\ntoken_file = "{root}/token.json"\noutput_root = "{self.output}"\n'
            f'state_root = "{self.state}"\narchive_root = "{self.archive}"\n\n'
            '[[folders]]\nname = "main"\noutput = "Proj/AI/{drive_name}"\nfolder_id = "folderAAAAAAAA"\nrecursive = true\n\n'
            '[[folders]]\nname = "devonly"\noutput = "_dev/x"\nfolder_id = "folderBBBBBBBB"\nenvironments = ["nas"]\n',
            encoding="utf-8")
        self.notifier = FakeNotifier()
        self.app = create_app(data_dir=root / "admin", command=[sys.executable, "-c", "pass"],
                              allowed_hosts=["testserver"], run_scheduler=False, notifier=self.notifier,
                              config_path=self.config_path, drive_lookup=fake_lookup)
        self.client = TestClient(self.app, follow_redirects=False)
        page = self.client.get("/login")
        self.client.post("/login", data={"csrf": csrf_of(page.text), "password": "admin"})

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def csrf(self):
        return csrf_of(self.client.get("/").text)

    def config(self):
        return tomllib.loads(self.config_path.read_text(encoding="utf-8"))

    def save(self, **fields):
        data = {"csrf": self.csrf(), "kind": "folder", "name": "", "drive": "", "output": "", "recursive": "1"}
        data.update(fields)
        return self.client.post("/targets/save", data=data)

    def make_state(self, name, out_dir, outputs):
        out_dir.mkdir(parents=True, exist_ok=True)
        for rel in outputs:
            (out_dir / rel).parent.mkdir(parents=True, exist_ok=True)
            (out_dir / rel).write_text("x")
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / f"{name}.json").write_text(json.dumps({
            "out_dir": str(out_dir), "converter_version": 1,
            "files": {"id1": {"name": "n", "path": "p", "mimeType": SHEET, "modifiedTime": "t", "outputs": outputs}},
        }))

    def test_list_shows_targets(self):
        page = self.client.get("/").text
        self.assertIn("main", page)
        self.assertIn("この環境では同期しない", page)  # devonly は nas だけ（テストは dev）
        self.assertIn("/targets/edit?name=main", page)

    def test_add_folder_and_file(self):
        response = self.save(name="案件B", drive="https://drive.google.com/drive/folders/folderBBBBBBBB",
                             output="B/AI", recursive="")
        self.assertEqual(response.status_code, 303)
        added = self.config()["folders"][-1]
        self.assertEqual(added, {"name": "案件B", "output": "B/AI", "folder_id": "folderBBBBBBBB", "recursive": False})
        self.assertIn("手で書いた設定", (self.config_path.parent / "config.toml.bak").read_text())  # 直前の内容を残す
        self.assertIn("Drive 上の名前: 別のフォルダ", self.client.get("/").text)

        self.save(kind="file", name="進行表", drive="https://docs.google.com/spreadsheets/d/sheetCCCCCCCCC/edit",
                  output="Proj/AI")
        self.assertEqual(self.config()["files"], [{"name": "進行表", "output": "Proj/AI", "file_id": "sheetCCCCCCCCC"}])
        self.assertEqual(self.config()["folders"][1]["environments"], ["nas"])  # ほかの同期先の設定は残る

    def test_groups(self):
        self.assertEqual(targets.parse_group("  案件A "), "案件A")
        self.assertIsNone(targets.parse_group(""))
        for bad in ["x" * 41, "a\nb"]:
            with self.assertRaises(ValueError, msg=bad):
                targets.parse_group(bad)
        self.save(name="案件B", group="案件B", drive="folderBBBBBBBB", output="案件B/AI")
        self.save(kind="file", name="進行表", group="案件B", drive="sheetCCCCCCCCC", output="案件B")
        self.assertEqual(self.config()["folders"][-1]["group"], "案件B")
        page = self.client.get("/").text
        self.assertIn("グループなし", page)  # main・devonly はグループなし
        self.assertLess(page.index("案件B"), page.index("グループなし"))  # グループなしは最後
        # グループから追加: 出力先の頭にグループ名が入る
        form = self.client.get("/targets/new?kind=folder&group=" + "案件B").text
        self.assertIn('value="案件B/"', form)
        self.assertIn('<option value="案件B">', form)
        # グループを外す
        self.save(original="案件B", name="案件B", group="", drive="folderBBBBBBBB", output="案件B/AI")
        self.assertNotIn("group", self.config()["folders"][-1])

    def test_popup_shows_result_page(self):
        response = self.save(name="案件B", drive="folderBBBBBBBB", output="B", popup="1")
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="done"', response.text)
        self.assertIn("保存しました", response.text)
        self.assertNotIn("保存しました", self.client.get("/").text)  # 元の画面には重ねて出さない
        error = self.save(name="x", drive="folderZZZZZZZZ", popup="1")
        self.assertIn('name="popup" value="1"', error.text)  # まちがいを直して保存し直しても、別ウィンドウのまま
        deleted = self.client.post("/targets/delete", data={"csrf": self.csrf(), "name": "案件B", "popup": "1"})
        self.assertIn("削除しました", deleted.text)

    def test_output_root_note(self):
        page = self.client.get("/").text
        self.assertIn("CDS_OUTPUT_DIR", page)
        self.assertIn("自動では引っ越しません", page)

    def test_rejects_bad_input(self):
        before = self.config_path.read_text()
        cases = [
            ({"name": "MAIN", "drive": "folderBBBBBBBB"}, "すでにあります"),                     # 大文字・小文字だけ違う
            ({"name": "x", "drive": "folderZZZZZZZZ"}, "見つかりません"),                         # Drive で読めない
            ({"name": "x", "drive": "sheetCCCCCCCCC"}, "フォルダではありません"),                 # 種類が違う
            ({"name": "x", "drive": "folderBBBBBBBB", "output": "Proj/AI/共有フォルダ/sub"}, "入れ子"),  # main の中
            ({"name": "x", "drive": "folderBBBBBBBB", "output": "../out"}, "使えません"),
        ]
        for fields, expected in cases:
            response = self.save(**fields)
            self.assertEqual(response.status_code, 400, fields)
            self.assertIn(expected, response.text, fields)
        self.assertEqual(self.config_path.read_text(), before)

    def test_rename_moves_state_and_archive(self):
        self.make_state("main", self.output / "Proj/AI/共有フォルダ", ["a.md"])
        (self.archive / "main" / "20261001-090000").mkdir(parents=True)
        self.save(original="main", name="案件A", drive="folderAAAAAAAA", output="Proj/AI/{drive_name}")
        self.assertEqual(self.config()["folders"][0]["name"], "案件A")
        self.assertTrue((self.state / "案件A.json").exists())
        self.assertFalse((self.state / "main.json").exists())
        self.assertTrue((self.archive / "案件A" / "20261001-090000").exists())

    def test_rename_refused_when_old_records_exist(self):
        self.make_state("main", self.output / "Proj/AI/共有フォルダ", ["a.md"])
        (self.state / "old.json").write_text("{}")  # 前に同じ名前の同期先があった跡
        response = self.save(original="main", name="old", drive="folderAAAAAAAA", output="Proj/AI/{drive_name}")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.config()["folders"][0]["name"], "main")
        self.assertTrue((self.state / "main.json").exists())

    def test_rename_case_only(self):
        self.make_state("main", self.output / "Proj/AI/共有フォルダ", ["a.md"])
        self.assertEqual(self.save(original="main", name="Main", drive="folderAAAAAAAA",
                                   output="Proj/AI/{drive_name}").status_code, 303)
        self.assertEqual(self.config()["folders"][0]["name"], "Main")
        self.assertEqual([p.name for p in self.state.iterdir()], ["Main.json"])

    def test_delete_archives_outputs(self):
        out_dir = self.output / "Proj/AI/共有フォルダ"
        self.make_state("main", out_dir, ["doc/a.md", "doc/a.images/1.png"])
        (out_dir / "memo.txt").write_text("手で置いたもの")
        page = self.client.get("/targets/delete?name=main").text
        self.assertIn("1 個分", page)
        self.client.post("/targets/delete", data={"csrf": self.csrf(), "name": "main"})
        self.assertEqual([f["name"] for f in self.config()["folders"]], ["devonly"])
        self.assertFalse((self.state / "main.json").exists())
        archived = list((self.archive / "main").glob("*/doc/a.md"))
        self.assertEqual(len(archived), 1)
        self.assertTrue((out_dir / "memo.txt").exists())  # 記録にないものは移さない
        self.assertFalse((out_dir / "doc").exists())       # 空になったフォルダは片付ける

    def test_changes_refused_while_syncing(self):
        self.app.state.runner.is_running = lambda: True
        before = self.config_path.read_text()
        self.assertEqual(self.save(name="x", drive="folderBBBBBBBB", output="X").status_code, 400)
        self.client.post("/targets/delete", data={"csrf": self.csrf(), "name": "main"})
        self.assertEqual(self.config_path.read_text(), before)


if __name__ == "__main__":
    unittest.main()
