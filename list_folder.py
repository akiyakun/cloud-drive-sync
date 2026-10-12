"""設定ファイルに書いた Drive フォルダの中身を一覧表示する（読み取りのみ・ダウンロードしない）"""

import sys
import tomllib
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
CONFIG_FILE = Path("data/config.toml")

FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"

# 表示を読みやすくするための種類名
MIME_LABELS = {
    FOLDER_MIME: "フォルダ",
    SHORTCUT_MIME: "ショートカット",
    "application/vnd.google-apps.document": "Googleドキュメント",
    "application/vnd.google-apps.spreadsheet": "Googleスプレッドシート",
    "application/vnd.google-apps.presentation": "Googleスライド",
    "application/vnd.google-apps.drawing": "Google図形描画",
    "application/pdf": "PDF",
}


def label_for(mime):
    if mime in MIME_LABELS:
        return MIME_LABELS[mime]
    if mime.startswith("image/"):
        return "画像"
    if mime.startswith("application/vnd.google-apps."):
        return "Google形式(その他)"
    return mime


def list_children(service, folder_id):
    """フォルダ直下のファイルを全件取得する（100件ずつのページを順に読む）"""
    items = []
    page_token = None
    while True:
        result = service.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            fields="nextPageToken, files(id, name, mimeType, size, modifiedTime, webViewLink, shortcutDetails)",
            orderBy="folder, name",
            pageSize=100,
            pageToken=page_token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        items.extend(result.get("files", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            return items


def walk(service, folder_id, recursive, depth, counts):
    """フォルダの中身を表示し、recursive なら中のフォルダもたどる"""
    indent = "  " * depth
    for item in list_children(service, folder_id):
        mime = item["mimeType"]
        label = label_for(mime)
        counts[label] = counts.get(label, 0) + 1

        info = f"[{label}]"
        if "size" in item:
            info += f" {int(item['size']):,} バイト"
        if mime == SHORTCUT_MIME:
            target = item.get("shortcutDetails", {}).get("targetMimeType", "?")
            info += f" → 実体: {label_for(target)}"

        print(f"{indent}{item['name']}  {info}  更新={item['modifiedTime'][:10]}")

        if mime == FOLDER_MIME and recursive:
            walk(service, item["id"], recursive, depth + 1, counts)


def main():
    if not CONFIG_FILE.exists():
        sys.exit(f"{CONFIG_FILE} がありません。config.example.toml をコピーして作成してください。")
    config = tomllib.loads(CONFIG_FILE.read_text(encoding="utf-8"))

    creds = Credentials.from_authorized_user_file(config["token_file"], SCOPES)
    service = build("drive", "v3", credentials=creds)

    for folder in config["folders"]:
        root = service.files().get(
            fileId=folder["folder_id"], fields="name", supportsAllDrives=True
        ).execute()
        print(f"=== {folder['name']}: {root['name']} (recursive={folder['recursive']}) ===")

        counts = {}
        walk(service, folder["folder_id"], folder["recursive"], 0, counts)

        print("--- 種類ごとの件数 ---")
        for label, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"  {label}: {n}")
        print()


if __name__ == "__main__":
    main()
