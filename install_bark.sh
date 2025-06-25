#!/bin/bash

# 如果任何命令失败，脚本将立即退出
set -e

# --- 配置 ---
# 这是 app.py 文件中硬编码的可执行文件名。
# 脚本将创建一个指向实际下载文件的符号链接，其名称为此变量的值。
APP_EXECUTABLE_NAME="bark-server_linux_amd64"

# --- 脚本开始 ---
echo "Bark Server 自动安装脚本"
echo "-----------------------------------"

# 1. 检测操作系统和架构
OS=$(uname -s | tr '[:upper:]' '[:lower:]')
ARCH=$(uname -m)

echo "正在检测您的系统..."

case "$ARCH" in
    "x86_64")
        ARCH="amd64"
        ;;
    "aarch64" | "arm64")
        ARCH="arm64"
        ;;
    *)
        echo "❌ 错误: 不支持的CPU架构 '$ARCH'。脚本仅支持 x86_64 (amd64) 和 aarch64/arm64。"
        exit 1
        ;;
esac

if [[ "$OS" != "linux" && "$OS" != "darwin" ]]; then
    echo "❌ 错误: 不支持的操作系统 '$OS'。脚本仅支持 Linux 和 macOS (darwin)。"
    exit 1
fi

echo "✅ 系统检测完成: OS=${OS}, Arch=${ARCH}"

# 2. 构建下载文件名和 URL
DOWNLOAD_FILENAME="bark-server_${OS}_${ARCH}"
DOWNLOAD_URL="https://github.com/Finb/bark-server/releases/latest/download/${DOWNLOAD_FILENAME}"

echo "将要下载的文件: ${DOWNLOAD_FILENAME}"

# 3. 检查文件是否已存在，并下载
if [[ -f "$DOWNLOAD_FILENAME" ]]; then
    # -n 1 表示读取一个字符后立即返回, -r 禁止反斜杠转义
    read -p "❓ 文件 '$DOWNLOAD_FILENAME' 已存在。是否重新下载? (y/N) " -n 1 -r
    echo # 换行
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "⏭️  跳过下载。"
    else
        echo "📥  正在从 GitHub 下载最新版本..."
        curl -L -# -o "$DOWNLOAD_FILENAME" "$DOWNLOAD_URL"
    fi
else
    echo "📥  正在从 GitHub 下载最新版本..."
    curl -L -# -o "$DOWNLOAD_FILENAME" "$DOWNLOAD_URL"
fi

# 检查下载是否成功
if [ ! -f "$DOWNLOAD_FILENAME" ]; then
    echo "❌ 错误: 下载失败，请检查您的网络或访问 GitHub Releases 页面手动下载。"
    exit 1
fi

# 4. 设置执行权限
echo "🔑  正在为 '$DOWNLOAD_FILENAME' 添加执行权限..."
chmod +x "$DOWNLOAD_FILENAME"

# 5. 创建符号链接以兼容 app.py
echo "🔗  正在创建符号链接: ${APP_EXECUTABLE_NAME} -> ${DOWNLOAD_FILENAME}"
# -s 表示创建符号链接, -f 表示如果链接已存在则强制覆盖
ln -sf "$DOWNLOAD_FILENAME" "$APP_EXECUTABLE_NAME"

echo ""
echo "🎉 安装成功！"
echo "您现在可以通过网页界面的 '启动服务' 按钮来运行 Bark Server 了。"
