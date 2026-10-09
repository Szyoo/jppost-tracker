#!/usr/bin/env bash
# 升级设计包 @szyyw/design：页面直接引用自托管 CDN https://design.szyyw.xyz/<tag>/，仓库里不放副本。
# 升级 = 把 src/app.py 的 DESIGN_VERSION 改成目标 tag，并先验证 CDN 上该版本已存在（tag 推上 GitHub 后 ≤10 分钟出现）。
# 用法: bash scripts/update-design.sh [vX.Y.Z]   （缺省取 GitHub 上最新正式 tag）
set -euo pipefail
cd "$(dirname "$0")/.."
FILE=src/app.py
REF="${1:-latest}"
if [ "$REF" = "latest" ]; then
  REF=$(git ls-remote --tags --refs https://github.com/Szyoo/szyyw-design.git 'v*' | sed 's#.*refs/tags/##' \
    | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -n1)
  [ -n "$REF" ] || { echo "取不到 szyyw-design 的最新 tag" >&2; exit 1; }
fi
echo "$REF" | grep -qE '^v[0-9]+\.[0-9]+\.[0-9]+$' || { echo "版本号须为 vX.Y.Z（不得用 latest 目录）：$REF" >&2; exit 1; }
# CDN 上还没有这个版本就失败退出（bot 下一轮会重试）
if ! curl -fsI --max-time 20 "https://design.szyyw.xyz/$REF/version.js" >/dev/null; then
  echo "CDN 上还没有 $REF（https://design.szyyw.xyz/$REF/version.js 不可用），稍后再试" >&2
  exit 1
fi
cur=$(sed -n 's/^DESIGN_VERSION = "\(v[0-9]*\.[0-9]*\.[0-9]*\)"$/\1/p' "$FILE" | head -n1)
[ -n "$cur" ] || { echo "$FILE 里找不到 DESIGN_VERSION" >&2; exit 1; }
sed -i.bak "s/^DESIGN_VERSION = \"$cur\"$/DESIGN_VERSION = \"$REF\"/" "$FILE" && rm -f "$FILE.bak"
echo "szyyw-design $cur → $REF（$FILE 的 DESIGN_VERSION）" >&2
