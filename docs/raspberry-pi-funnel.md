# 树莓派 Ubuntu 25 本地部署说明

本方案适用于把 `jppost-tracker` 跑在树莓派本机，同时满足两个目标：

1. `tracker.py` 通过树莓派本机的 Bark Server 发送推送。
2. iPhone 上的 Bark App 不加入 Tailscale，也能通过 Tailscale Funnel 提供的 HTTPS 地址完成配置和接收推送。

## 推荐架构

- `tracker.py` 访问 `BARK_SERVER_INTERNAL=http://127.0.0.1:8080`
- iPhone Bark App 配置 `BARK_SERVER_PUBLIC=https://<your-node>.<tailnet>.ts.net`
- Flask 控制台只在局域网或 Tailscale 内访问，不对公网开放
- Bark Server 对外只公开 `8080`，通过 Funnel 映射成公网 HTTPS

## 1. 安装依赖

如果树莓派当前只装了 `git`，先补系统依赖：

```bash
sudo apt-get update
sudo apt-get install -y python3-venv
```

如果你打算使用 `install_bark.sh` 的 Docker 回退方案，还要确保机器上已经有可用的 Docker。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
bash install_bark.sh
```

安装脚本会根据 CPU 架构下载对应的 Bark Server，并创建通用入口 `./bark-server`。
如果官方发布页暂时没有可用的 `arm64` 二进制，脚本会自动改用仓库内置的 Docker 包装器。

## 2. 配置 `.env`

推荐最小配置如下：

```ini
# tracker.py 优先使用这个地址给本机 Bark 推送
BARK_SERVER_INTERNAL=http://127.0.0.1:8080

# 手机 Bark 客户端配置这个 HTTPS 地址
BARK_SERVER_PUBLIC=https://your-node.your-tailnet.ts.net

# 兼容旧版；新部署可以留空
BARK_SERVER=

BARK_HEALTH_PATH=/ping
BARK_HEALTH_TIMEOUT=15
BARK_BIND_ADDRESS=0.0.0.0:8080

# Web 控制台本地端口
APP_PORT=6060

# 推荐在树莓派服务模式下开启
AUTO_START_BARK_SERVER=1

# 本地部署通常留空；这是云端保活用的
PUBLIC_URL=
```

变量说明：

- `BARK_SERVER_INTERNAL` 给追踪脚本使用，优先级最高。
- `BARK_SERVER_PUBLIC` 给手机 Bark App 和网页健康检查使用。
- `BARK_SERVER` 仅作兼容回退。
- `AUTO_START_BARK_SERVER=1` 时，Web 控制台启动后会自动拉起本地 Bark Server。

## 2.1 本地 SQLite 用户参数

树莓派本地部署现在支持真正的账号体系和多用户并行追踪，默认保存在：

```text
data/app.db
```

行为说明：

- 首次启动时会从旧版 `.env` / `user_profiles` 自动迁移出一个 `legacy-user-*` 默认追踪用户
- 如果 `.env` 中配置了 `ADMIN_USERNAME` 和 `ADMIN_PASSWORD_HASH`，系统会自动引导管理员账号
- 普通用户可在登录页直接注册
- 管理员登录后进入后台；普通用户登录后只看到自己的设置页
- `tracker.py` 会并行追踪所有 `tracking_enabled=1` 的用户，不再依赖“当前激活用户”

当前跟用户绑定的字段：

- `TRACKING_NUMBER`
- `CHECK_INTERVAL`
- `Bark Keys`
- `BARK_QUERY_PARAMS`
- `BARK_URL_ENABLED`
- 历史单号档案（保存在 `tracking_history` 表）

系统级字段仍保存在 `.env`：

- `BARK_SERVER_INTERNAL`
- `BARK_SERVER_PUBLIC`
- `BARK_SERVER`
- `BARK_HEALTH_PATH`
- `BARK_HEALTH_TIMEOUT`
- `BARK_BIND_ADDRESS`
- `PUBLIC_URL`
- `APP_PORT`
- `AUTO_START_BARK_SERVER`

备份建议：

```bash
tar czf jppost-backup.tgz .env data/app.db bark-data
```

## 3. 启用 Tailscale Funnel

首次配置建议：

```bash
sudo tailscale set --operator=$USER
sudo tailscale funnel --bg 8080
tailscale funnel status
```

期望看到类似：

```text
https://your-node.your-tailnet.ts.net
|-- / proxy http://127.0.0.1:8080
```

验证：

```bash
curl http://127.0.0.1:8080/ping
curl https://your-node.your-tailnet.ts.net/ping
```

说明：

- Funnel 地址自带 HTTPS，不需要单独购买域名。
- 第一次从公网访问 Funnel 可能会稍慢，健康检查超时默认调到了 15 秒。
- `tailscale funnel --bg` 的配置会持久化，重启后通常会自动恢复。

## 4. 安装 systemd 服务

仓库提供了示例服务文件：

- [deploy/systemd/jppost-tracker-web.service.example](/Users/next/Documents/GitHub/jppost-tracker/deploy/systemd/jppost-tracker-web.service.example)

参考安装：

```bash
sudo cp deploy/systemd/jppost-tracker-web.service.example /etc/systemd/system/jppost-tracker-web.service
sudo systemctl daemon-reload
sudo systemctl enable --now jppost-tracker-web
sudo systemctl status jppost-tracker-web
```

如果你的实际路径不是 `/opt/jppost-tracker`，或运行用户不是当前默认值，先修改示例文件里的 `User`、`Group`、`WorkingDirectory` 和 `ExecStart`。

## 4.1 如果要公开 6060 管理页

不建议裸公开。当前版本已经补上了登录保护，但要先在 `.env` 里配置这组变量：

```ini
SECRET_KEY=一段足够长的随机字符串
ADMIN_USERNAME=admin
ADMIN_PASSWORD_HASH=生成后的密码哈希
SESSION_COOKIE_SECURE=1
ADMIN_SESSION_HOURS=24
```

生成密码哈希：

```bash
python3 - <<'PY'
from werkzeug.security import generate_password_hash
print(generate_password_hash('替换成你的强密码'))
PY
```

说明：

- `ADMIN_PASSWORD_HASH` 只建议手工写入 `.env`，不会显示在网页环境变量列表里。
- 登录保护启用后，`/`、`/update_env`、`/remote_bark_status` 和 Socket.IO 控制入口都要先登录。
- 程序另外提供了一个无鉴权的 `/healthz`，只用于保活和 systemd / 反向代理健康检查。

## 5. 配置 iPhone Bark

1. 在手机 Bark App 中新增或修改服务器地址为 `BARK_SERVER_PUBLIC`。
2. 打开对应服务器页面，复制 App 里给出的推送 URL / Key。
3. 将完整推送 URL 或 key 回填到用户自己的 `Bark Keys`。
4. 如果同一个用户要推到多个终端，就在每个设备上各自复制一次 key，然后一行填一个。
5. 用下面的命令验证推送：

```bash
curl "https://your-node.your-tailnet.ts.net/<key>/测试标题/测试内容"
```

注意：

- 新自建服务器必须重新注册一次设备并重新复制 key。
- 旧 Render 服务器、旧 Bark 实例或旧手机记录里的 key 不能直接复用到新服务器。
- 多端推送不是“一个 key 自动广播”，而是“同一用户保存多个设备各自的 key”。
- 如果 Bark `/ping` 正常但推送返回 `400`，优先检查的就是这里。

## 6. 运行建议

- 不要把 Flask 管理页 `6060` 直接公开到公网。
- 如果修改了 `AUTO_START_BARK_SERVER`、`APP_PORT`、`BARK_BIND_ADDRESS` 这类启动参数，重启 Web 服务再生效。
- 用户自己的单号、Bark Keys 和推送参数都保存在 SQLite，普通用户在自己的设置页就能改。
- 每个账号保存过的不同单号会写进 `tracking_history`，首页和个人页都能看到单号档案。
- 网页中的 Bark 健康检查优先使用 `BARK_SERVER_PUBLIC`，这样更接近手机真实访问路径。
- 使用 systemd 常驻时，建议让服务在 `docker.service` 和 `tailscaled.service` 之后启动，仓库里的示例文件已经按这个顺序处理。
- 某些环境下，树莓派本机访问自己的 `*.ts.net` Funnel 地址会超时；当前版本会在网页健康检查里自动回退到 `BARK_SERVER_INTERNAL`，并明确标注“本机回退”。
- 公开 `6060` 前，至少要把 `ADMIN_PASSWORD_HASH`、`SECRET_KEY` 和 `SESSION_COOKIE_SECURE=1` 配好。
