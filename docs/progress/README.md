# 进度文档索引

本目录按分支记录持久化的工作进展，规则见 [AGENTS.md](../../AGENTS.md#计划与进度记录规则)。

## 分支文件

- [main.md](main.md) — `main` 分支：单用户版，Render 云端部署。
- [codex-raspi-local-funnel.md](codex-raspi-local-funnel.md) — `codex/raspi-local-funnel` 分支：
  树莓派本地部署改造，多用户账号体系（已合并进 main）。
- [feature-vultr-vps-deploy.md](feature-vultr-vps-deploy.md) — `feature/vultr-vps-deploy` 分支：
  部署到 vultr-jp 公网 VPS（Docker + Caddy 子域名反代，已合并 main 并上线）。
- [feature-vultr-vps-deploy.md](feature-vultr-vps-deploy.md) 之后的重构梳理见
  [../app-logic-review.md](../app-logic-review.md)。
- `refactor/streamline-services` 分支 — 重构第一刀：tracker 自动运行 + 按部署形态裁剪 UI
  （进度记在 [main.md](main.md) 对应日期，改动本身见分支提交）。
- [feature-szyyw-design-package.md](feature-szyyw-design-package.md) — `feature/szyyw-design-package`
  分支：前端接入 @szyyw/design v0.3.0（vendor + 明暗三态），基于 streamline-services。

## 跨分支事项

- 2026-07-15：新建 `AGENTS.md` 与本进度目录约定。
- 2026-07-15：`codex/raspi-local-funnel` 已快进合并进 `main`，两分支历史已统一，
  之前"是否合并"的待定事项已解决（见 [main.md](main.md)）。合并前发现的两个安全问题
  （权限降级不即时生效、登录限流可被绕过）仍未修复，详见
  [codex-raspi-local-funnel.md](codex-raspi-local-funnel.md)。
- 2026-07-15：新增第三种部署目标——Vultr 东京 VPS（公网直连，非 Tailscale/树莓派方案），
  详见 [main.md](main.md)。即将开新分支做相关改动。
