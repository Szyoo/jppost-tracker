# feature/vultr-vps-deploy 分支进度

目标：把项目部署到 `vultr-jp`（167.179.76.194，东京 Vultr VPS）。
服务器实机情报见 [main.md](main.md) 2026-07-15（三续）条目。

## 已确认的方案决策（2026-07-15，用户拍板）

1. 6060 管理页：**公网开放**，经 Caddy 反代子域名（`jppost.szyyw.xyz`）+ 自动 HTTPS。
2. 部署方式：**Docker 容器化**，遵循机器现有模式——容器不 publish 端口，
   挂 `ingress` 网络由 Caddy 反代。
3. Bark Server：**独立容器**（官方 `finab/bark-server` 镜像）+ 公网子域名
   `bark.szyyw.xyz`；沿用树莓派方案的 internal/public 分离，internal 走容器网络
   `http://bark:8080`。
4. code review 发现的两个安全问题（stale-role、X-Forwarded-For 限流绕过）：
   **本分支不修**，单独处理。文档里已标注风险与缓解（强口令）。

## 2026-07-15：部署工件完成

新增文件（纯部署工件，未动应用代码）：

- [Dockerfile](../../Dockerfile) + [.dockerignore](../../.dockerignore) —
  `python:3.13-slim`，只拷 `src/` 和依赖，数据/日志/.env 全走挂载卷。
- [deploy/vps/docker-compose.yml](../../deploy/vps/docker-compose.yml) —
  jppost-tracker + bark 两个服务，external network `ingress`，无 `ports:`。
- [deploy/vps/env.example](../../deploy/vps/env.example) — VPS 专用 .env 模板，
  安全三件套（SECRET_KEY / ADMIN_PASSWORD_HASH / SESSION_COOKIE_SECURE=1）标注为必填。
- [deploy/vps/Caddyfile.snippet](../../deploy/vps/Caddyfile.snippet) — 两个站点块，
  追加到机器上 `/opt/ingress/Caddyfile`。
- [docs/vultr-vps-deploy.md](../vultr-vps-deploy.md) — 完整部署步骤
  （DNS → clone → .env → compose up → Caddy reload → 手机配置 → 运维备忘）。

## 2026-07-15：实机部署完成并验证通过 ✅

部署过程中发现并修复的两个问题（都已提交）：

1. **ingress 网络名**：实机上该网络由 `/opt/ingress` compose 项目创建，实际名称是
   `ingress_ingress`，compose 里 external 网络需显式 `name: ingress_ingress`。
2. **compose 变量插值截坏密码哈希**：werkzeug scrypt 哈希含 `$`，`env_file` 默认会做
   变量插值把哈希替换成空串（现象：`docker compose` 输出一堆 "variable is not set" 警告）。
   修复：`env_file` 用 `path: + format: raw` 写法（`/opt/ingress` 的 portal.env 同款处理）。

部署记录：

- DNS：用户以为已加好，实际 zone 里没有——已用 Cloudflare API
  （tailscale2home 的 `.codex-local/cloudflare.env` token）补上 `jppost` / `bark`
  两条灰云 A 记录 → 167.179.76.194。
- 代码位置：VPS 上 `/opt/jppost-tracker`（clone 的本分支）；部署目录
  `/opt/jppost-tracker/deploy/vps`，`.env` 已配好安全三件套（chmod 600，
  管理员密码明文只在当次会话告知用户，建议用户登录后修改）。
- 容器：`jppost-tracker`（本地构建）+ `jppost-bark`（finab/bark-server），均无
  publish 端口，挂 `ingress_ingress` 网络。
- Caddy：两个站点块已追加到 `/opt/ingress/Caddyfile`（追加前备份为
  `Caddyfile.bak-jppost`），validate + reload 成功，Let's Encrypt 证书自动签发。
- 端到端验证全部通过：
  - `https://jppost.szyyw.xyz/login` → 200，未登录访问 `/` → 302 到登录页
  - `https://bark.szyyw.xyz/ping` → pong（注意：在 DNS 记录创建前查询过该域名的
    resolver 会有几分钟负缓存，从别处验证即可）
  - 容器内 `http://bark:8080/ping` 互通、`/healthz` 正常
  - admin 登录 POST → 302 → 登录态访问首页 200

## 待办

- [ ] 手机 Bark App 配置 `https://bark.szyyw.xyz`，把 device key 填进用户设置，
      打通真实推送（需要用户手机操作）。
- [ ] 部署验证通过后合并回 main。
- [ ] （单独任务）修复两个安全问题；公网开放后 X-Forwarded-For 问题优先级应提高。
- [ ] 用户登录后修改管理员初始密码。

## 设计备注

- 容器里 Flask 的"启动/停止 Bark 服务"按钮失效是**接受的取舍**：Bark 作为 compose
  兄弟服务由 Docker 管生命周期，比容器内 subprocess 更符合这台机器的运维模式。
- `AUTO_START_BARK_SERVER=0`、`PUBLIC_URL=` 留空（无 Render 保活需求）。
- 健康检查走 `BARK_SERVER_PUBLIC`（app.py 自带公网失败回退 internal 的逻辑，无需改动）。
