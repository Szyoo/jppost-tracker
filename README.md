# 日本邮政快递追踪与 Bark 通知服务

该项目提供一个基于 Flask + Vue 的简单网页界面，用于追踪日本邮政快递并通过 [Bark](https://github.com/Finb/bark-server) 进行推送通知。界面允许启动/停止追踪脚本和 Bark 服务，并支持在线修改 `.env` 环境变量。

## 功能亮点

- 实时查看追踪脚本和 Bark Server 输出日志
- 通过浏览器一键启动或停止追踪脚本、Bark Server
- 在线编辑 `.env` 配置，无需手动重启
- 支持在 `logs/` 目录中保存历史日志
- Windows 用户可直接运行 `run.bat` 启动追踪脚本

## 安装

1. 安装 Python 依赖：
   ```bash
   pip install -r requirements.txt
   ```
2. （可选）运行 `install_bark.sh` 下载并配置 Bark Server 可执行文件：
   ```bash
   bash install_bark.sh
   ```

## 使用

1. 在项目根目录新建或编辑 `.env` 文件，设置以下变量：
   ```ini
   TRACKING_NUMBER=你的快递单号
   CHECK_INTERVAL=300
   # CHECK_INTERVAL 在网页中可按“分钟 + 秒”输入，仍以秒保存
   BARK_SERVER=https://你的-bark-地址
   BARK_KEY=你的-bark-key(一般等于设备token)
   BARK_QUERY_PARAMS=?sound=minuet&level=timeSensitive
   # BARK_QUERY_PARAMS 可在网页上直接修改完整字符串
   # 或通过列表方式逐项编辑，效果等同
   BARK_URL_ENABLED=1
   # 是否在推送中附带追踪链接，可在网页中开关
   ```
2. 启动网页控制台：
   ```bash
   python src/app.py
   ```
3. 在浏览器访问 `http://localhost:6060`，即可通过界面管理追踪脚本和 Bark 服务。

## 在 Render 上部署 Bark Server

如果你没有自建 Bark Server，可直接用 Render 免费托管一份 `bark-server`，然后把地址填到 `BARK_SERVER`。

1. 在 Render 新建 **Web Service**。
2. 选择仓库：`https://github.com/Finb/bark-server`。
3. Render 配置项按下面设置（与官方 serverless 用法兼容）：
   - **Repository**: `https://github.com/Finb/bark-server`
   - **Branch**: `master`
   - **Git Credentials**: 使用你的凭据（`Use My Credentials`）
   - **Root Directory**: 留空
   - **Build Command**: `go mod download && go build -o bark-server`
   - **Start Command**: `./bark-server -serverless true`
   - **Auto-Deploy**: `On Commit`
4. 部署完成后，Render 会给你一个服务地址，例如：  
   `https://your-bark.onrender.com`
   这就是本项目 `.env` 里的 `BARK_SERVER`。

## 在 Bark 客户端里生成并配置 Key

1. 打开 iOS Bark 客户端，进入 **Settings**。
2. 找到 **Server**，填入你的 Render 地址（如 `https://your-bark.onrender.com`）。
3. 在 **Name** 里给这个 Server 起一个好记的名字（比如 `Render Bark`）。
4. 返回首页，Bark 会展示你的推送 URL/Key。  
   - 把 key 填到本项目的 `BARK_KEY`。
   - 示例推送 URL 形如：`https://your-bark.onrender.com/<token>/<title>/<body>`

## 目录结构

- `src/app.py`：Flask 应用及 WebSocket 服务
- `src/tracker.py`：查询日本邮政物流并推送 Bark 的脚本
- `src/templates/`：前端页面模板
- `src/static/`：前端静态资源
- `install_bark.sh`：自动下载并安装 Bark Server 的脚本

## 许可

本项目基于 MIT 许可证发布，详情参见仓库中的 LICENSE（如有）。
