# 进度文档索引

本目录按分支记录持久化的工作进展，规则见 [AGENTS.md](../../AGENTS.md#计划与进度记录规则)。

## 分支文件

- [main.md](main.md) — `main` 分支：单用户版，Render 云端部署。
- [codex-raspi-local-funnel.md](codex-raspi-local-funnel.md) — `codex/raspi-local-funnel` 分支：
  树莓派本地部署改造，多用户账号体系。

## 跨分支事项

- 2026-07-15：新建 `AGENTS.md` 与本进度目录约定。此前两个分支各自独立演进，尚未讨论合并计划；
  `codex/raspi-local-funnel` 领先 main 3 个提交（多用户账号、登录鉴权、树莓派部署文档），
  已完成一轮 code review（见 [codex-raspi-local-funnel.md](codex-raspi-local-funnel.md)），
  是否/何时合并回 main 待用户决定。
