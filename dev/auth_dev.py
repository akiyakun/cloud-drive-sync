"""開発用: テストデータ作成スクリプトのための認証（drive.file 権限）

drive.file は「このアプリ自身が作ったファイルだけ」を読み書きできる権限。
既存の資料は見ることも編集することもできないので、テストデータ作りに使っても安全。
普段の同期（sync.py）は token.json（読み取り専用）を使い、この鍵は使わない。

使い方（プロジェクトのフォルダで）:
    python dev/auth_dev.py
"""

import os

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
TOKEN_FILE = "token.dev.json"

flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
creds = flow.run_local_server(port=0)

with open(TOKEN_FILE, "w") as f:
    f.write(creds.to_json())
os.chmod(TOKEN_FILE, 0o600)  # 持ち主だけが読み書きできるようにする

print(f"開発用の認証に成功しました（{TOKEN_FILE} に保存）")
