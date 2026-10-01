#!/usr/bin/env bash
# 同步 @szyyw/design 运行时文件到 vendor —— 直接委托给上游的 sync.sh。
# 以前本脚本自带文件清单与 VENDORED.md 格式，正是各项目版本漂移的来源（旧清单缺 switcher.js）；
# 现在清单与格式只在 szyyw-design 仓库里维护。
# 用法: bash scripts/update-design.sh [tag|--local]   （默认 latest；--local 用本机 clone，见上游 sync.sh）
set -euo pipefail
DEST="$(cd "$(dirname "$0")/.." && pwd)/src/static/vendor/szyyw-design"
curl -fsSL https://raw.githubusercontent.com/Szyoo/szyyw-design/main/sync.sh | sh -s -- "$DEST" "${1:-latest}"
git -C "$DEST" status --short -- . || true
