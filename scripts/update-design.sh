#!/usr/bin/env bash
# 同步 @szyyw/design 运行时文件到 vendor —— 直接委托给上游的 sync.sh。
# 以前本脚本自带文件清单与 VENDORED.md 格式，正是各项目版本漂移的来源（旧清单缺 switcher.js）；
# 现在清单与格式只在 szyyw-design 仓库里维护。
# 用法: bash scripts/update-design.sh [tag|--local]   （默认 latest；--local 用本机 clone，见上游 sync.sh）
set -euo pipefail
DEST="$(cd "$(dirname "$0")/.." && pwd)/src/static/vendor/szyyw-design"
REF="${1:-latest}"
if [ "$REF" = "latest" ]; then
  # 先解析出最新正式 tag，再从这个 tag 取 sync.sh——main 上的 sync.sh 可能领先于已发版的文件清单
  REF=$(git ls-remote --tags --refs https://github.com/Szyoo/szyyw-design.git 'v*' | sed 's#.*refs/tags/##' \
    | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -n1)
  [ -n "$REF" ] || { echo "取不到 szyyw-design 的最新 tag" >&2; exit 1; }
fi
if [ "$REF" = "--local" ]; then
  sh "${DESIGN_UPSTREAM:-$HOME/Documents/GitHub/szyyw-design}/sync.sh" "$DEST" --local
else
  curl -fsSL "https://raw.githubusercontent.com/Szyoo/szyyw-design/$REF/sync.sh" | sh -s -- "$DEST" "$REF"
fi
git -C "$DEST" status --short -- . || true
