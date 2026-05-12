#!/bin/bash
# キャッシュ内の再生数0エントリ（取得エラー由来の可能性あり）を表示する
# 使い方: ./check_cache.sh

cd "$(dirname "$0")"
PYTHON=/Users/yoshihide/.pyenv/versions/yt-learn-3.11.9/bin/python

"$PYTHON" - <<'EOF'
import json
from pathlib import Path

cache_dir = Path("cache")
total_zeros = 0

for f in sorted(cache_dir.glob("*_view_cache.json")):
    d = json.loads(f.read_text(encoding="utf-8"))
    zeros = [k for k, v in d.items() if v == 0]
    total = len(d)
    pct = len(zeros) * 100 // total if total else 0
    print(f"{f.stem.replace('_view_cache', '')}")
    print(f"  0件: {len(zeros)} / {total} ({pct}%)")
    total_zeros += len(zeros)

print()
print(f"合計 {total_zeros} 件が 0（エラー由来の可能性あり）")
EOF
