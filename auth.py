"""同期用の認証（drive.readonly 権限）。ブラウザで許可すると token.json を作る

token.json は「Drive 全体を読める鍵」なので、持ち主だけが読める設定で保存する。
AI が読む場所（output/ など）や、git には絶対に入れない。

使い方（プロジェクトのフォルダで）:
    python auth.py
"""

import os

from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
TOKEN_FILE = "token.json"

# ブラウザで Google アカウントを認証
flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
creds = flow.run_local_server(port=0)

# 同期で使う鍵を保存（持ち主だけが読み書きできるようにする）
with open(TOKEN_FILE, "w") as f:
    f.write(creds.to_json())
os.chmod(TOKEN_FILE, 0o600)

# 接続テスト
service = build("drive", "v3", credentials=creds)
result = service.files().list(pageSize=5, fields="files(id,name,mimeType)").execute()

print("認証成功！")
for item in result.get("files", []):
    print(item["name"], item["mimeType"])
