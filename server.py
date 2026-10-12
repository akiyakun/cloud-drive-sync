"""管理画面と定期実行を起動する（NAS のコンテナの入口）

使い方:
    python server.py                 # 0.0.0.0:8000 で待ち受ける（コンテナの中）
    python server.py --port 8103     # Mac で試すとき
"""

import argparse

import uvicorn

from cds_admin.app import create_app


def main():
    parser = argparse.ArgumentParser(description="CloudDrive-Sync の管理画面")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning",
                server_header=False, proxy_headers=False, access_log=False)


if __name__ == "__main__":
    main()
