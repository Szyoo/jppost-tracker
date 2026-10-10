# 日本邮政快递追踪与 Bark 通知

基于 Flask + Flask-SocketIO 的日本邮政（Japan Post）快递追踪服务：后台定时查询物流状态，有变化时通过 [Bark](https://github.com/Finb/bark-server) 推送到手机，并提供网页管理后台与用户自助门户。

## 功能

- 多用户：本地 SQLite 账号，管理员后台 + 普通用户自助注册；每个账号可追踪多个单号、绑定多台设备的 Bark Key
- 追踪脚本并行处理所有启用的任务，推送失败自动重试；可随 Web 启动自动运行、意外退出自动重启
- 管理后台：启动/停止追踪脚本与本地 Bark Server、实时查看日志（Socket.IO）、在线编辑系统配置（`.env` 白名单键）
- Bark 地址内外分离：脚本走内网地址推送，手机使用公网 HTTPS 地址
- 安全：登录限流、`/healthz` 无鉴权健康检查；可选对接上游反代注入身份头的统一登录（见 [docs/portal-sso.md](docs/portal-sso.md)）

## 安装

需要 Python 3.10+（镜像使用 3.13）。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# 可选：下载本机 Bark Server（无官方二进制的平台会退回 Docker 包装器）
bash install_bark.sh
```

## 运行

### 本地

1. 在项目根目录创建 `.env`，最少配置：

   ```ini
   BARK_SERVER_INTERNAL=http://127.0.0.1:8080
   BARK_SERVER_PUBLIC=https://你的-bark-地址
   SECRET_KEY=一段足够长的随机字符串
   ADMIN_USERNAME=admin
   ADMIN_PASSWORD_HASH=生成后的密码哈希
   ```

   生成密码哈希：`python3 -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('你的强密码'))"`

2. 启动：`.venv/bin/python src/app.py`，访问 `http://localhost:6060/login`。管理员进入后台，普通用户注册后进入个人页。
3. 手机 Bark App 把服务器指向 `BARK_SERVER_PUBLIC`，复制 Key（或完整推送 URL）填到个人页的 Bark Keys，一行一个设备。换 Bark Server 后必须重新复制 Key。

测试：`.venv/bin/pip install pytest && .venv/bin/python -m pytest tests -q`

### Docker / 服务器

- `Dockerfile` 构建 Web 控制台镜像（`python src/app.py`，端口 6060，运行数据在 `/app/data`、`/app/logs`）。
- `deploy/vps/docker-compose.yml` 是 Web + 官方 `finab/bark-server` 的编排示例，配置模板见 `deploy/vps/env.example`；对外经反向代理提供 HTTPS，容器不直接 publish 端口。详见 [docs/vultr-vps-deploy.md](docs/vultr-vps-deploy.md)。
- 树莓派 + Tailscale Funnel + systemd 的本地部署见 [docs/raspberry-pi-funnel.md](docs/raspberry-pi-funnel.md)。
- 没有自建 Bark Server 时，也可以把 `bark-server` 以 `./bark-server -serverless true` 部署到任意 PaaS，再把地址填进 `BARK_SERVER_PUBLIC`。

## 接口说明

### HTTP

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/healthz` | 无 | 健康检查，返回 `{"status":"ok"}` |
| GET/POST | `/login`、`/register` | 无 | 登录、自助注册（启用统一登录时关闭） |
| POST | `/logout` | 无 | 退出登录 |
| GET | `/` | 登录 | 管理员显示后台，普通用户显示个人页 |
| POST | `/update_env` | 管理员 | 更新系统配置（仅白名单键） |
| GET | `/remote_bark_status` | 管理员 | 检查 Bark 地址连通性 |
| GET/POST | `/api/users` | 管理员 | 列出 / 创建账号 |
| GET/PUT | `/api/users/<id>` | 管理员 | 查看 / 修改账号 |
| POST | `/api/users/<id>/test_push` | 管理员 | 向该账号的设备发测试推送 |
| POST | `/api/users/<id>/tasks` | 管理员 | 为账号添加追踪任务 |
| POST | `/api/admin/accounts/<id>/portal-user` | 管理员 | 设置 / 清除账号的统一登录映射 |
| PUT/DELETE | `/api/tasks/<id>` | 登录（本人或管理员） | 修改 / 删除任务 |
| POST | `/api/tasks/<id>/archive` | 登录（本人或管理员） | 归档任务 |
| GET/PUT | `/api/me` | 登录 | 查看 / 修改自己的资料与 Bark 设置 |
| POST | `/me/update`、`/me/tasks`、`/me/tasks/<id>/update`、`/me/tasks/<id>/archive`、`/me/tasks/<id>/delete`、`/me/test-push` | 登录 | 个人页的表单提交版本 |

JSON 接口统一返回 `{"status": "success" | "error", "message": …}`，未登录返回 401。

### Socket.IO（仅管理员可连接）

- 客户端发送：`start_script`、`stop_script`、`start_bark_server`、`stop_bark_server`
- 服务端推送：`script_status`、`bark_server_status`、`keepalive_status`（`{running: bool}` 等）；日志增量 `tracker_log`、`bark_log`、`remote_bark_log` 与连接时的全量 `full_tracker_log`、`full_bark_log`、`full_remote_bark_log`（`{data: str}`）；鉴权失败时 `auth_error`

### 环境变量（`.env`）

| 变量 | 默认 | 说明 |
|---|---|---|
| `BARK_SERVER_INTERNAL` / `BARK_SERVER_PUBLIC` | 空 | 推送用内网地址 / 手机与健康检查用公网地址，互为回退 |
| `BARK_SERVER` | 空 | 旧版单一地址，仅作兼容 |
| `BARK_HEALTH_PATH` / `BARK_HEALTH_TIMEOUT` | `/ping` / `15` | Bark 健康检查路径与超时（秒） |
| `BARK_BIND_ADDRESS` / `BARK_EXECUTABLE` | `0.0.0.0:8080` / 自动查找 | 本地 Bark Server 监听地址与可执行文件 |
| `LOCAL_BARK_ENABLED` | `1` | `0` 时不管理本地 Bark 子进程（Bark 独立部署时） |
| `AUTO_START_BARK_SERVER` | `0` | Web 启动时自动拉起本地 Bark |
| `AUTO_START_TRACKER` | `1` | Web 启动时自动运行追踪脚本 |
| `APP_PORT` / `FLASK_DEBUG` | `6060` / `0` | Web 端口、调试模式 |
| `SECRET_KEY` | 随机 | 会话签名密钥，不设则每次重启失效 |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD_HASH` | `admin` / 空 | 首次启动时引导出的管理员账号 |
| `SESSION_COOKIE_SECURE` / `ADMIN_SESSION_HOURS` | `0` / `24` | HTTPS-only cookie、登录有效期（小时） |
| `ADMIN_LOGIN_MAX_ATTEMPTS` / `ADMIN_LOGIN_WINDOW_SECONDS` | `5` / `600` | 登录限流 |
| `TRUSTED_PROXY_COUNT` | `0` | 受信反代层数；大于 0 才采信 `X-Forwarded-For`（取右数第 N 个） |
| `PUBLIC_URL` / `KEEPALIVE_INTERVAL` | 空 / `600` | 自 ping 保活地址与间隔（秒），留空禁用 |
| `SZYYW_SSO` / `SZYYW_SSO_AUTOCREATE` / `PORTAL_ORIGIN` | `0` / `0` / — | 统一登录开关，见 [docs/portal-sso.md](docs/portal-sso.md) |
| `DESIGN_BASE` | CDN | 覆盖前端设计包的加载地址（本地离线开发用） |

单号、检查间隔、Bark Keys 等按账号保存在 SQLite（`data/app.db`，表结构见 [docs/data-model.md](docs/data-model.md)），不在 `.env` 里。账号映射也可用命令行维护：`python src/manage.py list-accounts | map-account | unmap-account`。备份请一并保存 `.env`、`data/`、`bark-data/`。

## 许可

MIT
