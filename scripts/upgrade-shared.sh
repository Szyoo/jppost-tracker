#!/usr/bin/env bash
# upgrade-shared.sh — 把本仓库固定的共享包升到上游最新的正式 tag（vX.Y.Z）。
#   szyyw-auth   ：requirements.txt 里的归档 URL
#   szyyw-design ：src/app.py 的 DESIGN_VERSION（页面直接引用 design.szyyw.xyz CDN；调用 scripts/update-design.sh 改版本并验证 CDN 已有该版本）
# 有改动时 stdout 只打印一行摘要（给提交信息用），没有就什么都不打印；其余输出一律走 stderr。
# CI（.github/workflows/upgrade-shared.yml）和本地都能跑。
set -euo pipefail
cd "$(dirname "$0")/.."

latest() { # <仓库> -> v0.8.0（只认正式 tag）
  git ls-remote --tags --refs "https://github.com/Szyoo/$1.git" 'v*' | sed 's#.*refs/tags/##' \
    | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -n1
}
newer() { # <当前> <最新> —— 最新严格更新才返回 0
  [ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | tail -n1)" = "$2" ]
}

parts=()

# --- szyyw-auth（Python 包，requirements.txt 固定 tag）---
req=requirements.txt
cur=$(grep -oE 'szyyw-auth/archive/refs/tags/v[0-9]+\.[0-9]+\.[0-9]+' "$req" | sed 's#.*/##' | head -n1)
new=$(latest szyyw-auth)
[ -n "$cur" ] || { echo "requirements.txt 里找不到 szyyw-auth 的固定版本" >&2; exit 1; }
[ -n "$new" ] || { echo "取不到 szyyw-auth 的 tag" >&2; exit 1; }
if newer "$cur" "$new"; then
  sed -i.bak "s#szyyw-auth/archive/refs/tags/$cur#szyyw-auth/archive/refs/tags/$new#" "$req" && rm -f "$req.bak"
  parts+=("szyyw-auth $cur → $new")
fi

# --- szyyw-design（CDN 引用，版本记在 src/app.py 的 DESIGN_VERSION）---
cur=$(sed -n 's/^DESIGN_VERSION = "\(v[0-9]*\.[0-9]*\.[0-9]*\)"$/\1/p' src/app.py | head -n1)
new=$(latest szyyw-design)
[ -n "$cur" ] || { echo "src/app.py 里找不到 DESIGN_VERSION" >&2; exit 1; }
[ -n "$new" ] || { echo "取不到 szyyw-design 的 tag" >&2; exit 1; }
if newer "$cur" "$new"; then
  # CDN 上还没出现新 tag 时 update-design.sh 失败退出，本轮整体失败，bot 下一轮重试
  bash scripts/update-design.sh "$new" >&2
  parts+=("szyyw-design $cur → $new")
fi

# 多个包同时升级时用「、」连成一行
if [ "${#parts[@]}" -gt 0 ]; then
  (IFS='、'; echo "${parts[*]}")
fi
