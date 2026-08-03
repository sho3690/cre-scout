#!/bin/zsh
# 手動で今すぐ調査したいとき用(普段は毎日8:00に自動実行されるので不要)
cd "$(dirname "$0")"
echo "EDINET DBから企業不動産データを調べています(数分かかることがあります)..."
python3 tools/edinetdb_scan.py --screen
open "file://$(pwd)/index.html"
echo ""
echo "終わりました。このウィンドウは閉じてかまいません。"
