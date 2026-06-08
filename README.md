# 日本邮政快递追踪与 Bark 通知服务

该项目提供一个基于 Flask + Vue 的简单网页界面，用于追踪日本邮政快递并通过 [Bark](https://github.com/Finb/bark-server) 进行推送通知。界面允许启动/停止追踪脚本和 Bark 服务，并支持在线修改 `.env` 环境变量。

当前版本额外支持更适合树莓派的本地部署方式：

- `tracker.py` 可以优先走本机 Bark 地址
- 手机 Bark 客户端可以单独使用公网 HTTPS 地址
- Web 控制台支持启动时自动拉起本地 Bark
- 支持本地 SQLite 账号与参数绑定，普通用户可注册登录
- 支持为每个账号保存历史单号档案
- 适合配合 Tailscale Funnel 暴露 Bark，管理页仍留在内网

## 功能亮点

- 实时查看追踪脚本和 Bark Server 输出日志
- 通过浏览器一键启动或停止追踪脚本、Bark Server
- 支持自动启动本地 Bark 服务
- 在线编辑 `.env` 配置，无需手动重启
- 支持管理员后台和普通用户自助门户
- 支持多用户并行追踪，每个用户绑定自己的 Bark Keys
- 支持记录每个账号用过的历史单号
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

说明：

- 在树莓派这类环境里，如果官方预编译 `bark-server` 二进制不可用，`install_bark.sh` 会自动退回到 Docker 包装器模式。
- 如果系统刚装好、只有 `git`，通常还需要先安装 `python3-venv`；若要使用 Docker 包装器，还需要有可用的 `docker`。

## 树莓派本地部署

如果你要在树莓派 Ubuntu 25 上本地运行，并通过 Tailscale Funnel 给手机提供 Bark HTTPS 地址，直接看这份说明：

- [docs/raspberry-pi-funnel.md](/Users/next/Documents/GitHub/jppost-tracker/docs/raspberry-pi-funnel.md)

## 使用

1. 在项目根目录新建或编辑 `.env` 文件，设置以下变量：
   ```ini
   # 推荐新部署拆成内外两个地址
   BARK_SERVER_INTERNAL=http://127.0.0.1:8080
   BARK_SERVER_PUBLIC=https://你的-bark-地址

   # 兼容旧版配置；若已配置内部/公网地址可留空
   BARK_SERVER=

   BARK_HEALTH_PATH=/ping
   BARK_HEALTH_TIMEOUT=15
   BARK_BIND_ADDRESS=0.0.0.0:8080
   APP_PORT=6060
   AUTO_START_BARK_SERVER=0

   # 如果要把 6060 管理页公开出去，建议启用下面这组
   SECRET_KEY=一段足够长的随机字符串
   ADMIN_USERNAME=admin
   ADMIN_PASSWORD_HASH=生成后的密码哈希
   SESSION_COOKIE_SECURE=1
   ADMIN_SESSION_HOURS=24
   ```
2. 启动网页控制台：
   ```bash
   .venv/bin/python src/app.py
   ```
3. 在浏览器访问 `http://localhost:6060/login`。
4. 管理员登录后进入后台；普通用户可在登录页直接注册并进入自己的设置页。

重要：

- Bark key 不是通用的。换到新的自建 Bark Server 后，必须在手机 Bark App 里把 Server 指向新地址，再重新复制这台服务器对应的 key。
- 当前版本支持多端推送，但实现方式是“一个用户保存多个设备各自的 key”，不是“一个 key 自动广播所有设备”。
- 如果要公开 `6060`，不要再裸暴露旧版本控制台。当前版本支持登录保护，但需要你先配置 `ADMIN_PASSWORD_HASH`。

生成 `ADMIN_PASSWORD_HASH` 的一个简单方法：

```bash
python3 - <<'PY'
from werkzeug.security import generate_password_hash
print(generate_password_hash('替换成你的强密码'))
PY
```

补充说明：

- `ADMIN_PASSWORD_HASH` 不会显示在网页环境变量列表里，避免误泄露。
- 开启登录保护后，`/update_env`、`/remote_bark_status` 和 Socket.IO 控制入口都需要会话认证。
- 保活探针已改成无鉴权的 `/healthz`，不会因为登录保护把旧的 Render 保活逻辑打坏。

## 本地用户与参数存储

当前版本会把账号、追踪参数和追踪状态持久化到本地 SQLite：

- 数据库路径：`data/app.db`
- 主表：`accounts`
- 单号档案表：`tracking_history`
- 首次启动时，会把旧版 `.env` / `user_profiles` 迁移成一个 `legacy-user-*` 默认追踪用户
- 如果 `.env` 里配置了 `ADMIN_USERNAME` 和 `ADMIN_PASSWORD_HASH`，还会自动引导出管理员账号

页面行为：

- 管理员登录后进入后台，可管理系统配置、查看日志、维护账号
- 普通用户登录后只看到自己的“我的快递与 Bark 设置”页面
- `tracker.py` 会并行读取所有 `tracking_enabled=1` 的用户，不再依赖“当前激活用户”

哪些参数跟用户绑定：

- `TRACKING_NUMBER`
- `CHECK_INTERVAL`
- `BARK Keys`
- `BARK_QUERY_PARAMS`
- `BARK_URL_ENABLED`

哪些参数仍保存在 `.env`：

- `BARK_SERVER_INTERNAL`
- `BARK_SERVER_PUBLIC`
- `BARK_SERVER`
- `BARK_HEALTH_PATH`
- `BARK_HEALTH_TIMEOUT`
- `BARK_BIND_ADDRESS`
- `PUBLIC_URL`
- `APP_PORT`
- `AUTO_START_BARK_SERVER`
- `ADMIN_USERNAME`
- `ADMIN_PASSWORD_HASH`
- `SECRET_KEY`

建议：

- 备份时把 `.env`、`data/app.db`、`bark-data/` 一起保存
- 不要把 `data/app.db` 提交到 Git 仓库

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
4. 返回首页，Bark 会展示你的推送 URL / Key。  
   - 普通用户把完整推送 URL 或 key 填到自己门户页里的 `Bark Keys`。
   - 如果同一个用户要推到多个设备，就把多个设备各自的 key 一行填一个。

如果之后改成树莓派自建的 Bark Server，需要重新在 Bark App 里切换 Server，并复制新的 key；旧 Render key 不能直接复用。

## 目录结构

- `src/app.py`：Flask 应用及 WebSocket 服务
- `src/tracker.py`：查询日本邮政物流并推送 Bark 的脚本
- `src/templates/`：前端页面模板
- `src/static/`：前端静态资源
- `install_bark.sh`：自动下载并安装 Bark Server 的脚本

## 许可

本项目基于 MIT 许可证发布，详情参见仓库中的 LICENSE（如有）。
