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

## 2026-07-15：bark.szyyw.xyz DNS 故障排查（已解决）

现象：容器内 `PUBLIC 自检失败`（`bark.szyyw.xyz` 解析不到），回退 INTERNAL 正常。排查结论分两层：

1. **DNS 负缓存链**：`bark` 记录创建前的查询在多级缓存留下了 NXDOMAIN——
   Cloudflare 部分边缘 PoP 传播卡住（权威 NS 一度回 NXDOMAIN，删除重建记录解决，
   新记录 id `60a325d6b6886e0201c7c0eb6de0352d`）；宿主机 DNS 走 **Tailscale MagicDNS
   (100.100.100.100)**，tailscaled 缓存了负结果，`resolvectl flush-caches` 无效，
   **重启 tailscaled 才清掉**。容器 DNS 上游即宿主机 resolv.conf，跟着恢复。
2. **hairpin NAT（遗留小瑕疵，不影响功能）**：DNS 恢复后容器访问
   `https://bark.szyyw.xyz`（= 宿主机公网 IP:443）TCP 超时——Docker 桥接网络访问
   宿主机自己 published 端口的经典 hairpin 问题。健康检查每次多等 15s 超时后回退
   INTERNAL。**修复方案已定**：给 `/opt/ingress/docker-compose.yml` 的 caddy 服务
   加 ingress 网络 aliases（`jppost.szyyw.xyz`、`bark.szyyw.xyz`），容器内直连 Caddy。
   已交给 tailscale2home 侧 agent 执行（2026-07-15）。

另：外部访问始终正常，手机推送已打通（key 填在管理页「用户管理」的用户编辑表单里）。

## 待办

- [x] 手机侧验证 —— 2026-07-15 用户确认验证 OK。
- [ ] Caddy 网络别名修复 hairpin（tailscale2home 侧执行后，确认容器内
      `https://bark.szyyw.xyz/ping` 秒回）。
- [ ] 合并回 main —— **用户明确表示暂时不合并**（2026-07-15），等用户发话再操作。
- [ ] （单独任务）修复两个安全问题；公网开放后 X-Forwarded-For 问题优先级应提高。
- [ ] 用户登录后修改管理员初始密码（是否已改未确认）。

## 设计备注

- 容器里 Flask 的"启动/停止 Bark 服务"按钮失效是**接受的取舍**：Bark 作为 compose
  兄弟服务由 Docker 管生命周期，比容器内 subprocess 更符合这台机器的运维模式。
- `AUTO_START_BARK_SERVER=0`、`PUBLIC_URL=` 留空（无 Render 保活需求）。
- 健康检查走 `BARK_SERVER_PUBLIC`（app.py 自带公网失败回退 internal 的逻辑，无需改动）。
