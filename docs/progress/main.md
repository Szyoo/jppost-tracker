# main 分支进度

## 2026-07-15（三续）：vultr-jp 实机情报核实（覆盖 provision 时快照）

向 tailscale2home 一侧的 agent 核实了 `vultr-jp`（167.179.76.194）现状，比下面
"新增 VPS 部署目标"那段里的 provision 时快照（2026-07-06，"未部署任何服务"）更新更准确：

- **运行时**：Python 3.14.4、pip 25.1.1、python3-venv 可用、Docker 29.6.1 + Compose v5.3.1、
  systemd 259。资源：1 vCPU/2G 内存（现约 1.0G 可用）、磁盘 47G（已用 23%）。
- **端口现状**：22 (sshd)、80/443 (Caddy 容器)、41641/udp (tailscaled)、3000 (portal 容器，
  仅 docker 内网)。**6060、8080 均空闲**。
- **已在跑的服务**：Caddy（`caddy:2-alpine`，配置 `/opt/ingress/Caddyfile`，自动 Let's Encrypt）
  + portal（自建门户，`szyyw.xyz` → portal:3000）。两者都在同一个 docker network "ingress" 里，
  **portal 容器不 publish 端口到宿主机，靠 Caddy 反代**——这是这台机器现有的标准部署模式。
- **安全模型的关键点**：`ufw` 只放行了 22/tcp，但 **Docker 的 iptables 规则会绕过 ufw**，
  任何 `docker compose` 里 publish 到 `0.0.0.0` 的端口都会直接对公网开放，ufw 管不到。
  也就是说：只要新服务不 publish 端口（只挂 ingress 网络走 Caddy 反代），就不会暴露在公网；
  一旦 publish 了，ufw 挡不住。无 fail2ban/sshguard。
- **域名/TLS**：`szyyw.xyz` 已解析到这台机器并在用；Cloudflare DNS API token 可用
  （`.codex-local/cloudflare.env`），加子域名（如 `jppost.szyyw.xyz`）操作路径已就绪，
  子域名怎么起 → 待定。Caddy 自动签证书已验证可用。
- **Tailscale**：这台机器也在 tailnet 上（节点 `vultr`，`100.65.101.16`），所以"仅 tailnet 可访问"
  和"经 Caddy 公网可访问"两条路都现成，选哪条 → 待定。
- **Vultr API key**：Access Control 仍是全 IP 放开，未收紧；但对本次部署无影响
  （防火墙走本机 ufw/docker，不经 Vultr 平台层）。
- **需要我们决定的开放项**（详见下方对话中的规划讨论）：
  1. 6060 管理页走 tailnet-only 还是公网+Caddy 子域名
  2. 部署方式：跟随现有 docker-compose + Caddy ingress 模式容器化，还是复用
     `deploy/systemd/jppost-tracker-web.service.example` 裸机跑
  3. Bark Server 是否也通过 Caddy 开子域名给手机 App 用（沿用树莓派方案的
     internal/public 分离思路，只是把 "Tailscale Funnel" 换成 "Caddy 反代"）
  4. 是否顺带修掉 code review 里发现的两个安全问题（尤其 X-Forwarded-For 信任问题——
     如果新容器不 publish 端口、只能被同网络的 Caddy 访问到，这个问题的实际风险会大幅降低，
     但仍建议只信任来自 Caddy 的转发头，而不是无条件信任）

## 2026-07-15（续）：合并 codex/raspi-local-funnel + 新增 VPS 部署目标

- 已将 `codex/raspi-local-funnel` 快进合并进 `main`（合并前已做过一轮 code review，
  详见 [codex-raspi-local-funnel.md](codex-raspi-local-funnel.md)，两个安全问题届时尚未修复）。
  合并后 `main` 已是多用户账号体系 + 树莓派/Tailscale Funnel 部署版本。
- **新增部署目标：Vultr VPS（东京）**。用户后续计划把本项目架设到这台服务器上，
  作为 Render 云端版 / 树莓派本地版之外的第三种部署形态。服务器背景（来自
  `tailscale2home` 仓库的 `.codex-local/router-tail629834-intel.md`，2026-07-06 provision，
  commit `95d9710`）：
  - 提供商/规格：Vultr `vhp-1c-2gb-amd`，区域 `nrt`（东京），Ubuntu 26.04
  - IPv4: `167.179.76.194`；IPv6: `2401:c080:1000:1d44:5400:06ff:fe59:0234`
  - 带宽额度：3072 GB/月
  - SSH：已切换为纯密钥登录（`PasswordAuthentication no`），专用密钥
    `~/.ssh/tailscale2home_vultr_ed25519`，Mac 上别名 `ssh vultr-jp`
  - 定位：这台 VPS 原计划替代已退役的 Cloudflare Tunnel，作为**公网 IP 反向代理入口**
    （不是替代 Tailscale）；截至该记录时机器上除 SSH 加固外未部署任何服务，是一台干净的
    公网直连 VPS —— 与树莓派方案的关键区别是**有真实公网 IP，不需要 Tailscale Funnel**。
  - 待办（来自 tailscale2home 那边的记录，非本项目待办）：Vultr API key 的 Access Control
    目前对所有 IP 开放，尚未收紧到家庭 IP。
- 下一步：基于以上前提开新分支，做"部署到公网 VPS"相关的改动（具体范围见
  [codex-raspi-local-funnel.md](codex-raspi-local-funnel.md) 的规划，或另开的新分支进度文件）。

## 2026-07-15

- 建立 `AGENTS.md` 与本进度记录约定（对话/注释/git 使用中文，按分支记录进度）。
- 当时 `main` 状态：单用户版，面向 Render 云端部署，无登录鉴权，最新提交
  `55b3366 页面修改为tab分页式，增加log查阅分页，适配手机尺寸`。
