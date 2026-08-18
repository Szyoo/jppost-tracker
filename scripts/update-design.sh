#!/bin/bash
# 一键同步 @szyyw/design 到本项目 vendor 目录。
# 用法：bash scripts/update-design.sh          # 默认：拉 GitHub 最新 tag（远端为准）
#       bash scripts/update-design.sh --local  # 例外：从本机 clone 的工作区同步（调试未发版的改动用）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$SCRIPT_DIR/../src/static/vendor/szyyw-design"
FILES=(tokens.css components.css dotfield.js scheme.js corner.js settings.js version.js)

if [[ "${1:-}" == "--local" ]]; then
  UPSTREAM="${DESIGN_UPSTREAM:-$HOME/Documents/GitHub/szyyw-design}"
  if [[ -n "$(git -C "$UPSTREAM" status --porcelain "${FILES[@]}" 2>/dev/null)" ]]; then
    echo "⚠️  本机上游工作区有未提交改动，将按工作区当前内容拷贝。"
  fi
else
  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT
  LATEST=$(curl -fsSL "https://api.github.com/repos/Szyoo/szyyw-design/tags?per_page=1" | python3 -c "import json,sys; print(json.load(sys.stdin)[0]['name'])")
  echo "从 GitHub 拉取 $LATEST ..."
  curl -fsSL "https://codeload.github.com/Szyoo/szyyw-design/tar.gz/refs/tags/$LATEST" | tar xz -C "$TMP" --strip-components=1
  UPSTREAM="$TMP"
fi

for f in "${FILES[@]}"; do
  cp "$UPSTREAM/$f" "$DEST/$f"
done

VERSION=$(python3 -c "import json; print(json.load(open('$UPSTREAM/package.json'))['version'])")
# 同步 VENDORED.md 里的版本号记录（macOS/BSD 与 GNU sed 的 -i 参数不同，做兼容）
if sed --version >/dev/null 2>&1; then
  sed -i -E "s/当前版本：\*\*v[0-9.]+[^*]*\*\*/当前版本：**v$VERSION**/" "$DEST/VENDORED.md"
else
  sed -i '' -E "s/当前版本：\*\*v[0-9.]+[^*]*\*\*/当前版本：**v$VERSION**/" "$DEST/VENDORED.md"
fi

echo "✅ 已同步 @szyyw/design v$VERSION（${#FILES[@]} 个文件）→ $DEST"
git -C "$SCRIPT_DIR/.." status --short -- src/static/vendor/szyyw-design/
