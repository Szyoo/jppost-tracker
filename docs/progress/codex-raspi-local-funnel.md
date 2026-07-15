# codex/raspi-local-funnel 分支进度

## 2026-07-15

- 完成一轮完整 code review（对比 `origin/main...HEAD`），结论：架构和安全基线整体不错
  （SQL 全参数化、密码用 werkzeug 哈希、`before_request` 默认拒绝式鉴权、IDOR 防护到位），
  但发现两个需要在对外暴露前修复的问题：
  1. **🔴 权限降级不即时生效**：`is_admin()`（[src/app.py:172](../../src/app.py#L172)）只读
     session 里缓存的 `account_role`，管理员被降级后旧会话仍保留管理员权限，直到
     `ADMIN_SESSION_HOURS`（默认 24h）过期或主动登出。需要改成每次请求从 DB 校验角色，
     或在角色变更时使旧 session 失效。
  2. **🟠 登录限流可被绕过**：`get_client_ip()`（[src/app.py:279](../../src/app.py#L279)）无条件信任
     `X-Forwarded-For`/`X-Real-IP`，攻击者直连时可每次请求伪造不同 IP，绕过
     `login_is_rate_limited` 的暴力破解锁定。需要只在明确配置了受信代理时才采信这些 header。
- 次要建议（非阻塞）：
  - `tracker.py` 的多用户追踪是**串行**执行（[src/tracker.py:244](../../src/tracker.py#L244)），
    与 README 里"并行追踪所有用户"的说法不符，一个慢请求会拖慢同批次其他用户的检查间隔。
  - `storage.py` 里 `with _connect() as conn:` 不会自动 `close()` 连接（sqlite3 的
    `__exit__` 只提交/回滚事务），目前靠 CPython 引用计数及时回收，建议显式关闭更稳妥。
  - 主循环即使无到期用户也每秒查一次 DB，量小无影响，但不是必须。
  - `/register` 无限流/验证码，符合"内网自助注册"的设计意图，若之后要对公网暴露需重新评估。

## 待办

- [ ] 是否/何时修复上述两个安全问题，由用户决定优先级。
- [ ] 是否/何时合并回 `main`，尚未讨论。
