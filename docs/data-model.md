# 数据模型（v2）

SQLite，库文件 `data/app.db`。schema 定义与迁移都在 [src/storage.py](../src/storage.py)。

## 两张表：身份与任务分离

v1 时代一行 `accounts` 既是登录身份又是唯一的追踪任务，导致"一个人只能追一个包裹"，
换单号只能覆盖（旧单号退到 `tracking_history` 当档案看）。v2 把两件事拆开：

```
accounts —— 谁（登录身份 + 这个人的 Bark 设备）
  id, username, display_name, password_hash, role, note,
  bark_keys, bark_query_params, bark_url_enabled, login_enabled,
  created_at, updated_at

tracking_tasks —— 追什么（一个账号 N 条）
  id, account_id →accounts.id (ON DELETE CASCADE),
  tracking_number, label, check_interval,
  enabled, archived,
  last_tracking_info, last_checked_at, last_error, last_push_at,
  first_seen_at, seen_count,
  created_at, updated_at
  UNIQUE(account_id, tracking_number)
```

### 字段归属的判断依据

- **Bark keys / query_params / url_enabled 属于账号**：key 对应一台已注册设备，
  推送风格是个人偏好，一个人的多个包裹共用同一批设备。
- **check_interval 属于任务**：等急件可以调密，慢件可以调稀。
- `label` 是给人看的备注（"键盘"、"给妈妈的礼物"），可空。

### enabled 与 archived 的区别

| | enabled | archived | 是否轮询 | 语义 |
|---|---|---|---|---|
| 正在追 | 1 | 0 | ✅ | 默认状态 |
| 暂停 | 0 | 0 | ❌ | 临时不查，还留在活跃列表里 |
| 归档 | 0 | 1 | ❌ | 已送达/不再关心，收进归档区 |

归档任务取代了 v1 的 `tracking_history` 表——「历史单号档案」现在就是「已归档的任务」，
概念统一了，而且归档任务保留完整的轮询历史（最后状态、推送时间）。

`UNIQUE(account_id, tracking_number)` + 复活语义：同一账号重复添加同一单号不会新建行，
而是把那条任务 `archived=0` 并 `seen_count += 1`。所以"这个单号我追过几次"这个信息还在。

## v1 → v2 迁移

`ensure_storage()` 启动时自动跑，幂等（判据：`accounts` 是否还有 `tracking_number` 列）。

1. 每个账号的当前单号 → 一条活跃任务，继承原有 `check_interval` / `tracking_enabled` /
   `last_*` 全部状态
2. `tracking_history` 里的其他单号 → 归档任务，保留 `first_seen_at` / `seen_count`；
   若某条就是当前单号，把档案里的首次时间与计数补回那条活跃任务
3. 重建 `accounts` 去掉已搬走的列，删除 `tracking_history`

### ⚠️ 迁移里踩过的坑（改这段代码前必读）

`ALTER TABLE accounts RENAME TO accounts_v1` 会**自动把子表已有的外键引用一起重定向**
到改名后的表。如果此时 `tracking_tasks` 已经存在，它的
`FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE` 会变成指向
`accounts_v1`；随后 `DROP TABLE accounts_v1` 就会顺着级联把**所有任务删干净**——
迁移"成功"但数据全丢，而且不报错。

因此迁移的步骤顺序是刻意的：**先把数据读进内存 → 重建 accounts → 之后才创建
`tracking_tasks` 并写入**。`ensure_storage` 另外用 `PRAGMA foreign_keys=OFF`
包住整个建表/迁移过程作为第二层保护（PRAGMA 必须在事务外执行才生效）。
