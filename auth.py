
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

# ブラウザでGoogleアカウントを認証
flow = InstalledAppFlow.from_client_secrets_file(
    "credentials.json", SCOPES
)
creds = flow.run_local_server(port=0)

# NASで再利用する認証トークンを保存
with open("token.json", "w") as f:
    f.write(creds.to_json())

# 接続テスト
service = build("drive", "v3", credentials=creds)
result = service.files().list(
    pageSize=5,
    fields="files(id,name,mimeType)"
).execute()

print("認証成功！")
for item in result.get("files", []):
    print(item["name"], item["mimeType"])
