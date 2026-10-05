# vultr-jp 公网 VPS 部署说明

把 jppost-tracker 部署到 `vultr-jp`（167.179.76.194，东京，Ubuntu 26.04）的说明。

> **日常发布不用看下面的步骤：推送到 `main` 就上线。** VPS 上的 `szyyw-autodeploy.timer` 每 2 分钟
> 检查一次，由平台仓库 szyyw-platform 的 `/opt/ingress/deploy/deploy-app.sh` 拉取 `main` →
> `docker compose build` → `up -d --no-deps` → 健康检查，失败自动回滚；只改 `docs/`、`*.md`、`.github/`
> 的提交不触发重建。下面的 §1-§4 是**首次搭建**的记录（该机器上已经完成）。
与 [树莓派方案](raspberry-pi-funnel.md) 的区别：这台机器有真实公网 IP 和现成的
Caddy 反代入口，**不需要 Tailscale Funnel**；管理页和 Bark 都走 Caddy 子域名 + 自动 HTTPS。

## 架构

```
手机 Bark App ──HTTPS──> bark.szyyw.xyz ──┐
浏览器管理页 ──HTTPS──> jppost.szyyw.xyz ──┤ Caddy 容器（80/443，自动证书）
                                          │      │ docker network "ingress"
                                          │      ├──> jppost-tracker:6060（Flask+SocketIO）
                                          │      └──> jppost-bark:8080（finab/bark-server）
                                          └─ 容器一律不 publish 端口
```

- `tracker.py` 推送走容器网络：`BARK_SERVER_INTERNAL=http://bark:8080`
- 手机 Bark App 配置公网地址：`BARK_SERVER_PUBLIC=https://bark.szyyw.xyz`
- Bark Server 由 compose 独立跑，**不再**由网页控制台按钮启动/停止

## ⚠️ 这台机器的安全模型（务必先读）

`ufw` 只放行了 22/tcp，但 **Docker 的 iptables 规则会绕过 ufw**：
compose 里任何 publish 到 `0.0.0.0` 的端口都会直接对公网开放，ufw 拦不住。
因此本部署的 compose 文件**刻意不写任何 `ports:`**，对外只经 Caddy。
改动 compose 时保持这一点。

## 1. DNS

在 Cloudflare 给 `szyyw.xyz` zone 添加两条 A 记录（**灰云/DNS-only**，
Caddy 要用 HTTP-01 验证签证书）：

| 名称 | 值 |
|---|---|
| `jppost` | `167.179.76.194` |
| `bark` | `167.179.76.194` |

## 2. 拉取代码并准备配置（首次搭建）

```bash
ssh vultr-jp
sudo mkdir -p /opt/jppost-tracker && sudo chown $USER /opt/jppost-tracker
git clone https://github.com/Szyoo/jppost-tracker.git /opt/jppost-tracker
cd /opt/jppost-tracker/deploy/vps
cp env.example app.env   # 注意文件名是 app.env，不是 .env（见 docker-compose.yml 注释）
```

编辑 `app.env`，填好**安全三件套**（公网开放的前提，缺一不可）：

```bash
# SECRET_KEY
python3 -c "import secrets; print(secrets.token_hex(32))"
# ADMIN_PASSWORD_HASH
python3 -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('你的强密码'))"
```

确认 `SESSION_COOKIE_SECURE=1`。

## 3. 启动容器（首次搭建）

```bash
cd /opt/jppost-tracker/deploy/vps
docker compose up -d --build
docker compose ps          # 两个服务都应为 running
docker compose logs -f jppost-tracker   # 确认 Flask 正常监听 6060
```

> 所有应用 compose 的项目名都是 `vps`：在应用目录里手动操作只用 `up -d --no-deps <服务>`，
> 绝不 `docker compose down`，也不要在共享 VPS 上跑任何 `docker … prune`。日常更新交给 `deploy-app.sh`（见 §6）。

## 4. 接入 Caddy

Caddyfile **不在本仓库、也不要再手改 VPS 上的 `/opt/ingress/Caddyfile`**：它在平台仓库
[szyyw-platform](https://github.com/Szyoo/szyyw-platform) 的 `caddy/Caddyfile`，提交并推送到该仓库
即自动 validate + reload 生效。`jppost.szyyw.xyz` 已在那里接好（带 `import sso` 门卫，别名由
`sync-caddy-aliases.sh` 自动补）；新增站点/改路由请去平台仓库改。
[deploy/vps/Caddyfile.snippet](../deploy/vps/Caddyfile.snippet) 只是历史参考，不再追加到 VPS。

验证：

```bash
curl https://bark.szyyw.xyz/ping        # 应返回 bark-server 的 pong JSON
curl -I https://jppost.szyyw.xyz/login  # 应返回 200
```

## 5. 配置手机 Bark 与账号

1. 手机 Bark App 添加服务器 `https://bark.szyyw.xyz`，复制生成的 device key。
2. 浏览器打开 `https://jppost.szyyw.xyz`，用 `ADMIN_USERNAME` + 密码登录管理后台。
3. 建用户/或用户自助注册，把 device key 填进用户的 Bark Keys，填单号。
4. 管理后台启动追踪脚本，或让用户在自己门户页操作。
5. 测试推送：用户页/管理页都有"测试推送"按钮。

## 6. 运维备忘

- **更新部署 = 推送 `main`**：VPS 每 2 分钟自动拉取并重建（`deploy-app.sh jppost`，失败自动回滚）。
  发布前先确认在 `main`、工作区干净、已推送。
- **立即部署**（不想等 2 分钟）：`ssh vultr-jp /opt/ingress/deploy/deploy-app.sh jppost`
- **回滚**：`ssh vultr-jp /opt/ingress/deploy/deploy-app.sh jppost --ref <提交或tag>`（固定到该版本；
  之后自动部署在不在 `main` 上时会跳过，修好后推 `main` 并再跑一次不带 `--ref` 的部署即恢复）。
  查看状态：`... jppost --status`。
- **共享包自动升级**：`.github/workflows/upgrade-shared.yml` 每 6 小时检查 szyyw-auth / szyyw-design
  的最新正式 tag，有新版就升级、跑测试，通过后以 github-actions[bot] 直接推 `main`（随后自动部署）。
  手动触发：`gh workflow run upgrade-shared.yml --repo Szyoo/jppost-tracker --ref main`；
  本地也可直接跑 `scripts/upgrade-shared.sh`。
- **备份**：`tar czf jppost-backup.tgz deploy/vps/app.env deploy/vps/data deploy/vps/bark-data`
- **资源**：机器只有 2G 内存（现约 1.0G 可用），本项目两个容器合计占用预计 <200M，
  但再往这台机器加服务时留意。
- `.env` 设 `LOCAL_BARK_ENABLED=0` 后，控制台不再显示"Bark 服务"启停卡片
  （Bark 在独立容器里，由 compose 管理）；健康检查用的是 `BARK_SERVER_PUBLIC`，正常工作。
- `.env` 设 `AUTO_START_TRACKER=1`（默认即开）后，Web 容器启动会自动运行追踪脚本，
  脚本意外退出 10 秒后自动重启；只有在控制台手动点"停止"才会保持停止。
- **已知未修问题**（详见 [进度记录](progress/codex-raspi-local-funnel.md)）：
  1. 登录限流信任 `X-Forwarded-For` 首值——即使经过 Caddy，攻击者也能自带伪造头绕过限流；
     强口令是当前的实际防线。
  2. 管理员被降级后旧 session 最长保留权限 24h。
  两者已列入单独修复计划，公网开放期间尤其注意用强密码。
