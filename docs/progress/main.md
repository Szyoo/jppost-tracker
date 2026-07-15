# main 分支进度

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
