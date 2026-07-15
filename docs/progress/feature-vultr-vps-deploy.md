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

## 待办

- [ ] DNS：Cloudflare 加 `jppost` / `bark` 两条 A 记录（灰云）——需要用户或
      tailscale2home 侧 agent 用 `.codex-local/cloudflare.env` 的 token 操作。
- [ ] 实机部署验证（clone → compose up → Caddy reload → curl 验证 → 手机推送打通）。
- [ ] 部署验证通过后合并回 main。
- [ ] （单独任务）修复两个安全问题；公网开放后 X-Forwarded-For 问题优先级应提高。

## 设计备注

- 容器里 Flask 的"启动/停止 Bark 服务"按钮失效是**接受的取舍**：Bark 作为 compose
  兄弟服务由 Docker 管生命周期，比容器内 subprocess 更符合这台机器的运维模式。
- `AUTO_START_BARK_SERVER=0`、`PUBLIC_URL=` 留空（无 Render 保活需求）。
- 健康检查走 `BARK_SERVER_PUBLIC`（app.py 自带公网失败回退 internal 的逻辑，无需改动）。
