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
  created_at, updated_at,
  channel                       -- 渠道名，空串 = 未标注（到达预测时 ADD COLUMN 补上）
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
只有已归档的任务会被这样复活；单号仍在追踪中时重复添加会直接报错，不会静默覆盖原有备注和间隔。

`check_interval` 后端强制 30 秒到 7 天，超出范围报错。

## 任务时间线 task_events

```
task_events —— 发生过什么（一个任务 N 条）
  id, task_id →tracking_tasks.id (ON DELETE CASCADE), account_id,
  tracking_number, kind, status, detail, created_at
  INDEX(task_id, created_at), INDEX(created_at)
```

- `kind` 穷举：`status_change` 状态变化 / `push_ok` 推送成功 / `push_fail` 推送失败 /
  `query_error` 查询出错 / `query_recovered` 查询恢复 / `auto_archived` 签收自动归档。定义在 `storage.EVENT_KIND_META`，
  写入时校验；前端映射在 `src/static/timeline.js`，测试比对两边必须一致。
- 写入都在 `update_task_state` 的同一事务里，且带抓取时的单号做校验：抓取期间单号被改了，整次回写作废，防止串号。
  `tracking_number` 记事件发生时的单号，任务改单号后旧事件仍留在这条任务下。
- 去重：推送失败时 `last_tracking_info` 不落库，重试会带着同一批记录回来——`status_change` 以本单号最近一条为界只追加之后的；
  同一状态连续推送失败只记第一次；查询出错只在「正常→出错」「出错→恢复」切换时各记一条。
- tracker 解析履历表的全部记录，两次轮询之间走过的中间状态都会记成 `status_change`（首次抓取记下全部历史）。
- 签收自动归档（系统设置 `AUTO_ARCHIVE_DELIVERED`，默认开）：最新记录或本轮新增记录的状态精确等于
  `お届け先にお届け済み` / `お届け済み` / `窓口でお渡し`（`tracker.is_delivered`）时，推送成功后在同一次 `update_task_state`
  事务里归档（`archived=1`、`enabled=0`）并记 `auto_archived`，顺序为 `status_change → push_ok → auto_archived`。
  推送失败不归档、保持启用按退避重试，推送成功那轮再归档；账号没配 Bark Keys 时不推送、直接归档。
  最新记录已是签收但本单号从没记过 `auto_archived`（开关后来才打开、功能上线前已签收）的任务，下一轮补归档一次；
  自动归档后被用户取消归档的任务不会再被自动归档。
- 保留：每个任务最多 500 条，全表只留 180 天，写入时顺手清理。
- 分页接口 `GET /api/tasks/<id>/events?limit=&before=`，按 id 倒序（id 与写入时间同序），本人或管理员可看。

建表在 `ensure_storage` 里排在 v1→v2 迁移之后（`CREATE TABLE IF NOT EXISTS`，已有库启动时自动补表）。
以后若要重建 `tracking_tasks`，下面那个外键坑同样适用于 `task_events`。

## 到达预测：渠道、carrier_records 与 pre_milestones

```
tracking_tasks.channel TEXT NOT NULL DEFAULT ''   -- ALTER TABLE ADD COLUMN，幂等；去首尾空白，最长 50

carrier_records —— 承运方履历（长期保留，不清理）
  id, task_id →tracking_tasks.id (ON DELETE CASCADE), tracking_number,
  source CHECK IN ('jp_post'),   -- 承运方来源，穷举，见 forecast.CARRIER_SOURCES
  record_at,                     -- 'YYYY-MM-DD HH:MM'；履历只有日期时存 'YYYY-MM-DD'
  status, detail, created_at
  UNIQUE(task_id, tracking_number, source, record_at, status, detail), INDEX(tracking_number)

pre_milestones —— 发货前节点
  id, task_id →tracking_tasks.id (ON DELETE CASCADE),
  stage CHECK IN ('ordered', 'packed', 'handed_over'), at,
  source CHECK IN ('caller', 'manual'), updated_at
  UNIQUE(task_id, stage)
```

- **为什么另建 carrier_records 而不用 task_events**：时间线有 180 天 / 每任务 500 条的清理，不能当经验库；
  而且时间线只记「本轮新增」，履历表是事实全集。tracker 每次成功抓到履历都把**全部**记录 `INSERT OR IGNORE` 进来，
  在 `update_task_state` 的同一事务里、受同一个「抓取时单号」校验保护（单号被改了整次作废）；推送成功、失败、无变化都照写——
  履历是事实，与推送无关。补录走 `record_carrier_records`，校验相同但不动任务状态。
- **存原始状态，阶段在读取时算**（`forecast.record_stage` 按 `source` 分派到各承运方的映射）：以后改映射规则，对历史数据自动生效；
  以后接入始发国承运方，只要在 `CARRIER_SOURCES` 加一项（CHECK 约束由它派生，已有库需要另做表迁移——别重建 `tracking_tasks`）。
- 回填：启动时 `carrier_records` 为空而 `task_events` 里有 `status_change`，就把那些事件解析回填一次（`INSERT OR IGNORE`，重复执行无害）。
  info 没有日期前缀的记录放不进时间轴，不入库。
- `pre_milestones` 的优先级：**manual 优先**。调用方（`/api/v1` 的 `milestones`）以 `caller` 写入，不覆盖 `manual` 行、也不能删除；
  用户在网页手填以 `manual` 写入，覆盖 `caller` 行；值与现有相同时不动（表单原样保存不会把调用方写的变成手填）；手填清空即删除该行。
- `channel`：网页创建 / 编辑可改；调用方只在任务渠道为空时写入；补录对已有任务也只在为空时补。
- 补录新建的任务是 `archived=1, enabled=0`（label 空），直接 INSERT、不走 `_insert_task`，不轮询不推送。
  `api_refs` 的四个触发器只管 UPDATE / DELETE，插入一条已归档任务不会产生或改动来源记录。

三者在 `ensure_storage` 里排在最后（`_ensure_forecast_schema`，只加列、只 `CREATE TABLE IF NOT EXISTS`），已有库启动时自动补。
外键都指向 `tracking_tasks`：以后若要重建它，同 `task_events` 一样会踩下面的外键坑。

## 机器接口：api_callers 与 api_refs

```
api_callers —— 谁在调（外部调用方，一个令牌一行）
  id, name UNIQUE, account_id →accounts.id (ON DELETE CASCADE),
  token_sha256 UNIQUE,            -- 只存令牌的 SHA-256 十六进制，不存明文
  created_at, last_used_at,       -- last_used_at 节流写入（距上次超过 60 秒才写）
  revoked (0/1), revoked_at

api_refs —— 调用方登记过哪些单号（来源记录 + 墓碑）
  id, caller_id →api_callers.id (ON DELETE CASCADE), account_id →accounts.id (ON DELETE CASCADE),
  tracking_number, task_id（可空，无外键）, external_ref,
  state CHECK IN (active, archived_by_api, user_archived, delivered_archived, user_deleted),
  created_at, updated_at
  UNIQUE(caller_id, tracking_number), INDEX(task_id)
```

令牌绑定一个账号，接口只能碰这个账号下的任务；同名调用方只能以同一账号重新签发，所以 `caller_id` 对应的账号不会变。

**为什么是新表而不是给 `tracking_tasks` 加列**：一条任务可以被多个调用方登记，各自有 `external_ref`；
更关键的是任务被删除后来源信息还要留下来当墓碑，挂在任务行上的列会随行一起消失。新表也不需要重建 `tracking_tasks`。

### state 穷举

| state | 含义 | 再 PUT 同单号 |
|---|---|---|
| `active` | 已登记，任务在追踪（含暂停） | 200 `existing` |
| `archived_by_api` | 调用方用 archive 接口归档 | 视为新一轮，直接恢复（200 `revived`） |
| `user_archived` | 用户在网页里归档 | 409，带 `revive` 才恢复 |
| `delivered_archived` | 追踪脚本检测到签收后自动归档 | 200 `delivered`，不恢复；带 `revive` 才恢复（200 `revived`） |
| `user_deleted` | 用户删除了任务或改走了单号；`task_id` 已置空（墓碑） | 409，带 `revive` 才重建；用户自己加回了就直接关联 |

### 墓碑由触发器维护

`task_id` 刻意不设外键（上面那个「RENAME 重定向外键 + DROP 级联」的坑会把来源记录一起弄丢），
状态由建在 `tracking_tasks` 上的四个触发器维护，对任何删除/归档路径（网页、管理员、账号级联删除、以后新增的代码）都生效：

- `trg_api_refs_task_delete`（BEFORE DELETE）：引用该任务的记录 → `task_id = NULL`、`user_deleted`。必须是 BEFORE，删除后就找不到引用了。
- `trg_api_refs_task_renumber`（改 `tracking_number`）：同上——原单号已不由这条任务追踪。
- `trg_api_refs_task_archive`（`archived` 0→1）：仍为 `active` 的记录 → `user_archived`。
  接口自己归档时先把记录改成 `archived_by_api`、签收自动归档时先改成 `delivered_archived`，再在同一事务里归档任务，
  触发器只动 `active` 的记录，就不会误记成用户归档（这个触发器本身没改）。
- `trg_api_refs_task_unarchive`（`archived` 1→0）：`user_archived` / `archived_by_api` / `delivered_archived` → `active`（不论谁取消的归档）。

PUT 判断「签收自动归档」：本调用方有关联记录时看它是否为 `delivered_archived`；没有（例如网页建的任务）时看任务本单号下
最近一条事件是否为 `auto_archived`（自动归档后不再轮询，不会再有新事件），是则补一条 `delivered_archived` 记录并返回 `delivered`。

**触发器与约束的升级**：触发器定义集中在 `storage._API_REF_TRIGGERS`，`ensure_storage` 把 `sqlite_master` 里的定义按空白规整后比对，
不同的 `DROP` 后按新定义重建（新增 `delivered_archived` 时只有 unarchive 那个变了）。`state` 的 CHECK 约束无法 ALTER，
`_migrate_api_refs_states` 发现建表语句里的状态集合不是当前的 `API_REF_STATES` 时，只重建 `api_refs` 这一张表
（新表 → 复制 → 删旧表 → 改名，先删掉引用它的四个触发器再重建，否则改名会报错）。`api_refs` 不被任何外键引用，
不会触发下面的级联坑；`tracking_tasks` 与 `task_events` 不动。

读取时另有一层防御：记录的 `task_id` 指向的任务不存在或单号对不上，按 `user_deleted` 处理。

接口的写入不走 `_insert_task`：网页侧「已归档就复活并覆盖备注、间隔」的语义不适合调用方；
接口新建任务只用 `label`，关联或恢复已有任务时不改备注、间隔、暂停状态（恢复会把 `enabled` 置 1）。
写入用 `BEGIN IMMEDIATE`，同单号的并发 PUT 串行执行。

两张表在 `ensure_storage` 里排在最后建（`CREATE TABLE / TRIGGER IF NOT EXISTS`），已有库启动时自动补上，不动已有数据。
以后若要重建 `tracking_tasks`，触发器会随旧表一起被删，重建后需重新创建（再跑一次 `ensure_storage` 即可）。

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
