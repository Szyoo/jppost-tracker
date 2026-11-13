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
tracking_tasks.atypical INTEGER NOT NULL DEFAULT 0 -- 非常规件（如大件电器），作为预测样本时降权；ALTER TABLE ADD COLUMN，幂等

carrier_records —— 承运方履历（长期保留，不清理）
  id, task_id →tracking_tasks.id (ON DELETE CASCADE), tracking_number,
  source CHECK IN ('jp_post', 'cn_ems'),   -- 承运方来源，穷举，见 forecast.CARRIER_SOURCES
  record_at,                     -- 'YYYY-MM-DD HH:MM'；履历只有日期时存 'YYYY-MM-DD'
                                 --   jp_post 存页面原值（始发国行是当地时间，读取时换算）；cn_ems 存换算好的日本时间
  status, detail, created_at     -- cn_ems：status = 事件描述，detail = 地点（可空）
  UNIQUE(task_id, tracking_number, source, record_at, status, detail), INDEX(tracking_number)

track17_numbers —— 17TRACK 登记状态（按单号，不按任务；不设外键）
  tracking_number PRIMARY KEY,
  registered_at,                 -- 登记成功（含 17TRACK 回「已登记」）的时间；NULL = 还没登记成功
  last_synced_at,                -- 最近一次查到中国邮政事件的时间
  last_attempt_at,               -- 最近一次请求（登记或查询）的时间，补抓时按它轮转限频
  last_error, created_at

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
  接入新承运方只要在 `CARRIER_SOURCES` 加一项，CHECK 约束由它派生，已有库由 `_migrate_carrier_sources` 自动放宽（见下）。
- 回填：启动时 `carrier_records` 为空而 `task_events` 里有 `status_change`，就把那些事件解析回填一次（`INSERT OR IGNORE`，重复执行无害）。
  info 没有日期前缀的记录放不进时间轴，不入库。
- `pre_milestones` 的优先级：**manual 优先**。调用方（`/api/v1` 的 `milestones`）以 `caller` 写入，不覆盖 `manual` 行、也不能删除；
  用户在网页手填以 `manual` 写入，覆盖 `caller` 行；值与现有相同时不动（表单原样保存不会把调用方写的变成手填）；手填清空即删除该行。
- `channel`：网页创建 / 编辑可改；调用方只在任务渠道为空时写入；补录对已有任务也只在为空时补。
- 补录新建的任务是 `archived=1, enabled=0`（label 空），直接 INSERT、不走 `_insert_task`，不轮询不推送。
  `api_refs` 的四个触发器只管 UPDATE / DELETE，插入一条已归档任务不会产生或改动来源记录。

三者在 `ensure_storage` 里排在最后（`_ensure_forecast_schema`，只加列、只 `CREATE TABLE IF NOT EXISTS`），已有库启动时自动补。
外键都指向 `tracking_tasks`：以后若要重建它，同 `task_events` 一样会踩下面的外键坑。

### carrier_records 的 CHECK 迁移

`source` 的 CHECK 约束无法 ALTER。`_migrate_carrier_sources` 发现建表语句里的来源集合不是当前的 `CARRIER_SOURCES` 时，
**只重建 `carrier_records` 这一张表**：建 `carrier_records_new` → 按列复制（保留 id 与 created_at）→ `DROP` 旧表 → 改名；
集合一致就跳过，重复执行无害。索引随旧表删除，紧接着由 `CREATE INDEX IF NOT EXISTS` 补回。

不会踩下面那个外键坑的理由：那个坑是**改名被引用的父表**（`accounts`）时，子表的外键被自动重定向到改名后的表，
随后 `DROP` 顺着级联删光子表。`carrier_records` 是子表——它引用 `tracking_tasks`，但没有任何表引用它：
`DROP` 旧表只删它自己的行，改名时也没有别的外键需要重定向，新表自带的外键原样指向 `tracking_tasks`。
`tracking_tasks` 本身不动。整个过程照例在 `ensure_storage` 的 `PRAGMA foreign_keys=OFF` 下进行。
测试 `test_carrier_check_migration_keeps_data_and_is_idempotent` 造旧 CHECK 的库验证：数据不丢、幂等、外键仍在且级联有效。

### 始发国段：cn_ems 与 track17_numbers

只对单号后两位为 `CN` 的件、且配置了 `TRACK17_API_KEY` 时启用（没配时整个功能关闭）。

- **登记只做一次**：17TRACK 按登记的单号扣额度。`track17_numbers` 按单号记状态，同单号多个任务只登记一次；
  登记成功或 17TRACK 回「已登记」都记 `registered_at`，之后只查轨迹（不扣额度）。登记失败记 `last_error`，下轮再试。
- **tracker**：每次 `process_task` 抓日本邮政前先同步一次（`tracker.sync_cn_leg`）：没登记 → 登记；已登记 → 查轨迹，
  中国邮政（provider 国家 `CN`）的全部事件 `INSERT OR IGNORE` 进 `carrier_records`（`source='cn_ems'`），与 jp_post 一样带抓取时单号校验。
  17TRACK 的任何错误只记日志与 `last_error`，不影响日本邮政追踪、推送与 `last_error`；始发国段不推送。签收自动归档后自然不再查。
- **已归档单号补抓**：补录的件建出来就是归档的，不会被轮询。tracker 主循环每 30 分钟最多补一批（40 个，17TRACK 单次上限）：
  已登记且登记不满 3 天、该单号的任务全部已归档、还没查到过事件或还没出现「到达寄达地」、距上次请求超过 30 分钟的单号，只查不登记。
- **补录**：CN 单号补录时也登记并立刻试查一次；刚登记多半还没数据，结果里注明「国内段稍后补上」，之后由上面的补抓拿到。

阶段映射（`forecast._cn_ems_stage`）：收取 → `accepted`；离开揽投部 / 完成分拣 → `left_collection`；出口海关 → `origin_customs`；
国际互换局出口直封 / 离开国际互换局 → `departed_origin`；已交承运商运输 / 航空公司接收 → `handed_to_airline`；
航空公司启运 → `flight_departed`；到达寄达地 → `landed_destination`；到达寄达地处理中心 → `arrived_japan`；其余（日本段的中文事件）不映射，
日本段以 jp_post 为准。同一阶段两个来源取最早。收寄网点（`forecast.collection_office`）取最早一条
「邮件离开【X】，正在发往」/「邮件已在【X】完成分拣」里的 X（互换局除外），用于显示与渠道推断（`suggested_channel`，不写库）。

### 线路分段与按段加权

阶段归属的段（`forecast.STAGE_META` 的 `segment`，穷举 `SEGMENT_META`，前后端各一份并有一致性测试）：
仓库段 `warehouse`（ordered / packed / handed_over）→ 国内段 `domestic`（accepted … flight_departed）→
中转段 `transit`（transit_arrived / transit_departed）→ 日本段 `japan`（landed_destination 及之后）。段沿阶段顺序只前进，导入时自检。

- **中转段**要看事件序列，不能逐条映射（`forecast.transit_stages`）：第一条「航空公司启运」之后的第二条启运 = `transit_departed`，
  两者之间的第一条「飞机进港」= `transit_arrived`；只有一条启运（一段航班）就没有这两个阶段。同单号多任务的重复事件按时刻去重。
- **履历按段切分**：每条 cn_ems 事件落在「时间不晚于它的最晚已到阶段」所在的段（`forecast.segment_at`）。
- **显示名**：中转段用系统设置 `TRANSIT_REGION_LABEL`（未设置 =「韩国」，显示「韩国段 / 到达韩国 / 离开韩国」；显式清空 = 通用名「中转段 / 到达中转地 / 离开中转地」）。
  属 live 生效，Web 每次读进程环境变量；系统设置页在键从未出现过时显示默认值。
- **按段加权**（`forecast.build_forecast`）：剩余阶段在仓库段时同渠道 1.0 / 其他 0.3；在国内 / 中转 / 日本段时同收寄网点 1.0 / 其他 0.3。
  理由：仓库段的耗时取决于发货渠道自己的打包与交运；揽收之后同一网点走的是同一条邮政线路（同一互换局、同一航线），
  耗时由邮政决定、与发货渠道无关，网点相同而渠道不同的样本同样有代表性。当前单没有收寄网点（没有始发国段数据）时全部按渠道比；
  当前单有而某个样本没有时，这一对按渠道比。
- **可信度**按剩余阶段里第一个阶段所在段的比较依据（`basis`）数「同类」合格样本（没有剩余阶段时看当前阶段之后的那个阶段）。
- **非常规件**（`tracking_tasks.atypical`）：样本权重再乘 `forecast.ATYPICAL_WEIGHT`（0.3）；同单号多个任务合并时任一任务标了就算；
  当前单自己是否非常规不影响别的样本。只在网页编辑（`PUT /api/tasks/<id>` 的 `atypical` 布尔、门户表单的复选框、归档任务在预测区块里切换），
  `/api/v1` 不暴露。

### 17TRACK 登记状态（网页显示）

`storage.TRACK17_STATUS_META` 穷举（前端 `forecast.js` 一份一致的映射）：`unregistered` 未登记 / `pending` 已登记待数据 /
`synced` 已同步 / `error` 出错（`last_error` 非空时优先）。任务详情只对 CN 单号显示（没配密钥时只显示跳转胶囊）；
管理员卡片 `GET /api/admin/track17` 统计已登记单号的各状态个数，额度在 Web 进程内缓存 5 分钟（只缓存成功结果，换密钥即失效）。

### ⚠️ 两个时区坑

1. **17TRACK 的中国邮政事件**：境内事件与航班事件（航空公司接收 / 启运、飞机进港）的 `+08:00` 是对的，换算成日本时间入库；
   但「到达寄达地」及之后的事件（描述含「寄达地」「境外」「【日本】」）其实是**日本当地时间却被标成 `+08:00`**
   （对照日本邮政：「到达寄达地处理中心 22:53」=「国際交換局に到着 22:53」），取字面时刻当日本时间入库（`forecast.track17_event_time`）。
2. **日本邮政页面上始发国那几行**（引受、始发国互换局、始发国海关、离开始发国）的时刻其实是**始发国当地时间**
   （「引受 11:11」=「中国邮政已收取邮件 11:11:50 +08:00」）。库里保留原值，**读取 / 算阶段时**按单号国家码换算成日本时间
   （`forecast.record_time`，`ORIGIN_UTC_OFFSETS` 目前只有 `CN` → UTC+8；没有映射的国家码按日本时间，国家码是外部数据无法穷举）。
   只有日期没有时刻的记录不换算。

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
