"""同期用の認証（drive.readonly 権限）。ブラウザで許可すると token.json を作る

鍵の置き場所は Mac の中の ~/.config/cloud-drive-sync/（環境変数 CDS_SECRETS_DIR で変えられる）。
NAS の共有フォルダの中では chmod が効かず、共有にアクセスできる人に読まれてしまうため、共有フォルダには置かない。
OAuth クライアントの情報（Google Cloud からダウンロードした JSON）は、同じ場所の credentials.json に置く。
NAS のコンテナで使うときは、作った token.json を NAS の /volume3/.secrets/cloud-drive-sync/ へ SSH でコピーする。
token.json は「Drive 全体を読める鍵」なので、持ち主だけが読める設定で保存する。
AI が読む場所（output/ など）や、git には絶対に入れない。

使い方（プロジェクトのフォルダで）:
    python auth.py
"""

import os
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
SECRETS_DIR = Path(os.environ.get("CDS_SECRETS_DIR", "~/.config/cloud-drive-sync")).expanduser()
CREDENTIALS_FILE = SECRETS_DIR / "credentials.json"
TOKEN_FILE = SECRETS_DIR / "token.json"

SECRETS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)

# ブラウザで Google アカウントを認証
flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
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
