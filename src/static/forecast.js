// 到达预测：段 / 阶段 / 可信度 / 比较依据 / 补录结果的文案映射 + 预测区块渲染 + 补录对话框。
// 管理后台（app.js，Vue）动态 import 本文件复用渲染与对话框；门户页直接以 module 引入，自动挂到 data-task-forecast / data-backfill-open 上。

// 与 forecast.SEGMENT_META 一一对应，顺序即先后。中转段的显示名由 forecast.transit_region（系统设置）决定
export const SEGMENT_META = Object.freeze({
    warehouse: { label: '仓库段' },
    domestic: { label: '国内段' },
    transit: { label: '中转段' },
    japan: { label: '日本段' },
});

// 与 forecast.STAGE_META 一一对应（tests 会比对两边），顺序即时间先后。不提供「未知阶段」兜底：
// 后端新增阶段时这里必须同步补上，漏了就报错而不是悄悄显示原值。
export const STAGE_META = Object.freeze({
    ordered: { label: '下单寄出', segment: 'warehouse' },
    packed: { label: '打包完成', segment: 'warehouse' },
    handed_over: { label: '出库交运', segment: 'warehouse' },
    accepted: { label: '{origin}收寄', segment: 'domestic' },
    left_collection: { label: '离开揽收网点', segment: 'domestic' },
    origin_exchange: { label: '到达{origin}国际互换局', segment: 'domestic' },
    origin_customs: { label: '{origin}出口海关', segment: 'domestic' },
    departed_origin: { label: '离开{origin}（出境）', segment: 'domestic' },
    handed_to_airline: { label: '交航', segment: 'domestic' },
    flight_departed: { label: '{origin}航班起飞', segment: 'domestic' },
    transit_arrived: { label: '到达{transit}', segment: 'transit' },
    transit_departed: { label: '离开{transit}', segment: 'transit' },
    landed_destination: { label: '航班到达日本', segment: 'japan' },
    arrived_japan: { label: '到达日本互换局（入境）', segment: 'japan' },
    japan_customs: { label: '日本进口通关', segment: 'japan' },
    departed_exchange: { label: '离开日本互换局', segment: 'japan' },
    local_arrived: { label: '到达派送局', segment: 'japan' },
    out_for_delivery: { label: '派送中', segment: 'japan' },
    delivered: { label: '签收', segment: 'japan' },
});

// 与 forecast.GENERIC_ORIGIN / GENERIC_TRANSIT 一致：不知道始发国 / 中转地时的通用名
const GENERIC_ORIGIN = '始发国';
const GENERIC_TRANSIT = '中转地';

// 与 route_nodes.NODE_SOURCE_META 一一对应：合并履历节点的来源小标
export const NODE_SOURCE_META = Object.freeze({
    milestone: { label: '手填' },
    cn_ems: { label: '中邮' },
    jp_post: { label: '日邮' },
});

// 与 forecast.CONFIDENCE_META 一一对应
export const CONFIDENCE_META = Object.freeze({
    none: { label: '还没有可参考的历史' },
    borrowed: { label: '还没有同类记录，借鉴其他单' },
    rough: { label: '同类样本较少，仅供粗估' },
    solid: { label: '同类样本充足' },
});

// 与 forecast.BASIS_META 一一对应：「同类」的比较依据
export const BASIS_META = Object.freeze({
    channel: { label: '同渠道' },
    office: { label: '同收寄网点' },
});

// 与 storage.TRACK17_STATUS_META 一一对应：单号在 17TRACK 的登记状态
export const TRACK17_STATUS_META = Object.freeze({
    unregistered: { label: '未登记', tone: 'neutral' },
    pending: { label: '已登记待数据', tone: 'info' },
    synced: { label: '已同步', tone: 'ok' },
    error: { label: '出错', tone: 'err' },
});

// 与 storage.BACKFILL_RESULT_META 一一对应
export const BACKFILL_RESULT_META = Object.freeze({
    added: { label: '已补录', tone: 'ok' },
    updated: { label: '已补充', tone: 'info' },
    no_records: { label: '查不到履历', tone: 'warn' },
    invalid: { label: '格式错误', tone: 'err' },
});

const lookup = (map, key, what) => {
    if (!Object.prototype.hasOwnProperty.call(map, key)) {
        throw new Error(`未定义的${what}：${key}`);
    }
    return map[key];
};

export const stageMeta = (stage) => lookup(STAGE_META, stage, '阶段');
export const segmentMeta = (segment) => lookup(SEGMENT_META, segment, '段');
export const confidenceMeta = (key) => lookup(CONFIDENCE_META, key, '可信度');
export const basisMeta = (key) => lookup(BASIS_META, key, '比较依据');
export const nodeSourceMeta = (key) => lookup(NODE_SOURCE_META, key, '节点来源');
export const track17StatusMeta = (key) => lookup(TRACK17_STATUS_META, key, '17TRACK 登记状态');

// 阶段 / 段的显示名：{origin} 换成始发国名、{transit} 换成中转地名（forecast.origin_name / transit_region），不知道时用通用名
export const stageLabel = (stage, { transit = '', origin = '' } = {}) => stageMeta(stage).label
    .replace('{origin}', String(origin || '').trim() || GENERIC_ORIGIN)
    .replace('{transit}', String(transit || '').trim() || GENERIC_TRANSIT);

export const segmentLabel = (segment, region = '') => {
    const meta = segmentMeta(segment);
    const name = String(region || '').trim();
    return name && segment === 'transit' ? `${name}段` : meta.label;
};
export const backfillResultMeta = (key) => lookup(BACKFILL_RESULT_META, key, '补录结果');

// --- 格式化 ---

const WEEKDAYS = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];

// 'YYYY-MM-DD HH:MM'（日本时间）只按字面拆，不经 Date 的时区换算
const splitTime = (text) => {
    const match = /^(\d{4})-(\d{2})-(\d{2})(?: (\d{2}):(\d{2}))?$/.exec(String(text || ''));
    if (!match) throw new Error(`时间格式不正确：${text}`);
    const [, y, m, d, hh, mm] = match;
    const weekday = new Date(Date.UTC(Number(y), Number(m) - 1, Number(d))).getUTCDay();
    return { md: `${Number(m)}/${Number(d)}`, weekday: WEEKDAYS[weekday], hm: hh ? `${hh}:${mm}` : '' };
};

export const formatDuration = (seconds) => {
    const minutes = Math.max(0, Math.round(Number(seconds || 0) / 60));
    if (minutes < 60) return `${minutes} 分钟`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours} 小时`;
    const days = Math.floor(hours / 24);
    const rest = hours % 24;
    return rest ? `${days} 天 ${rest} 小时` : `${days} 天`;
};

// 一天之内到的给到时刻，更远的只给日期 + 星期
const formatEta = (item) => {
    if (item.median_seconds <= 0) return '随时';
    const eta = splitTime(item.eta);
    return item.median_seconds < 86400 ? `${eta.md} ${eta.weekday} ${eta.hm} 前后` : `${eta.md} ${eta.weekday} 前后`;
};

const formatRange = (item) => {
    const low = splitTime(item.eta_low);
    const high = splitTime(item.eta_high);
    if (low.md === high.md) return item.high_seconds < 86400 && low.hm !== high.hm ? `${low.md} ${low.hm}–${high.hm}` : low.md;
    return `${low.md}–${high.md}`;
};

const ORIGIN_TEXT = Object.freeze({ ordered: '从下单算', handed_over: '从出库算', accepted: '从引受算' });

export const progressText = (forecast) => {
    if (!forecast.current) return '还没有物流记录，抓到第一条履历后开始预测。';
    if (forecast.delivered) {
        return forecast.total_seconds !== null
            ? `已签收 · 全程 ${formatDuration(forecast.total_seconds)}`
            : '已签收';
    }
    const parts = [];
    if (forecast.origin) parts.push(`${lookup(ORIGIN_TEXT, forecast.origin.stage, '起点')}第 ${forecast.day} 天`);
    const stage = forecast.current.stage;
    const names = { transit: forecast.transit_region, origin: forecast.origin_name };
    parts.push(`当前：${segmentLabel(stageMeta(stage).segment, forecast.transit_region)} · ${stageLabel(stage, names)}（已等 ${formatDuration(forecast.current.waited_seconds)}）`);
    return parts.join(' · ');
};

// 「其中 n 单非常规」：非常规件作为样本已降权，单独说明
const atypicalNote = (count) => (count ? `，其中 ${count} 单非常规` : '');

// 依据一行：按段说明同渠道 / 同网点各几单，可信度按剩余第一个阶段所在段的比较依据（forecast.basis）
export const basisText = (forecast) => {
    const { total, same_channel: sameChannel, same_office: sameOffice, similar, similar_atypical: similarAtypical, atypical } = forecast.samples;
    const parts = [];
    if (forecast.basis === 'office') {
        parts.push(`同网点 ${sameOffice} 单${atypicalNote(similarAtypical)}`);
        if (sameChannel) parts.push(`同渠道 ${sameChannel} 单`);
    } else {
        parts.push(`同渠道 ${sameChannel} 单${atypicalNote(similarAtypical)}`);
        if (forecast.collection_office && sameOffice) parts.push(`同网点 ${sameOffice} 单`);
    }
    if (total > similar) parts.push(`其他 ${total - similar} 单参考${atypicalNote(atypical - similarAtypical)}`);
    parts.push(`${confidenceMeta(forecast.confidence).label}（按${basisMeta(forecast.basis).label}计）`);
    return parts.join(' · ');
};

// --- 拉取与渲染 ---

const readJson = async (response, fallback) => {
    let result = null;
    try {
        result = await response.json();
    } catch (error) {
        result = null;
    }
    if (!response.ok || !result || result.status !== 'success') {
        throw new Error(result?.message || `${fallback}（HTTP ${response.status}）。`);
    }
    return result;
};

// 返回 { forecast, summary, history, task, links, track17 }：summary 是任务卡片的摘要行（「当前步骤 · 第 N 天 · 预计…签收」，与推送正文同一写法）；history 是合并履历节点（日本时间，按时间倒序，带 segment / sources）；
// task 是 { archived, atypical }；links 是 { jp_post, track17 }（track17 只有 CN 单号才有）；
// track17 是登记状态 { status, registered_at, last_synced_at, error }，只在 CN 单号且配了密钥时有
export const fetchForecast = async (taskId) => {
    const response = await fetch(`/api/tasks/${taskId}/forecast`, {
        headers: { Accept: 'application/json' },
        credentials: 'same-origin',
    });
    const result = await readJson(response, '加载预测失败');
    return {
        forecast: result.forecast,
        summary: result.summary,
        history: result.history || [],
        task: result.task || {},
        links: result.links || {},
        track17: result.track17 || null,
    };
};

// 「设为该渠道」「非常规件」：走现有的任务编辑接口，只带要改的字段（局部更新，不动其他字段）
const patchTask = async (taskId, fields, fallback) => {
    const response = await fetch(`/api/tasks/${taskId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify(fields),
    });
    return readJson(response, fallback);
};
const setTaskChannel = (taskId, channel) => patchTask(taskId, { channel }, '设置渠道失败');

const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
};

// 收寄网点与渠道推断：任务渠道为空时，网点只对应过一个渠道就建议它（按钮写入），对应多个只提示
const buildOfficeNode = (forecast, { taskId, onUpdated }) => {
    if (!forecast.collection_office) return null;
    const box = el('div');
    box.append(el('p', 'task-meta', `收寄网点：${forecast.collection_office}`));
    if (forecast.suggested_channel) {
        const channel = forecast.suggested_channel;
        const line = el('p', 'hint', `可能是 ${channel}（同收寄网点的历史都是 ${channel}）`);
        if (taskId) {
            const button = el('button', 'btn-ghost btn-small', '设为该渠道');
            button.type = 'button';
            button.addEventListener('click', async () => {
                button.disabled = true;
                try {
                    const result = await setTaskChannel(taskId, channel);
                    onUpdated?.(result);
                } catch (error) {
                    const { toast } = await import('@szyyw/design/toast.js');
                    toast(error.message, { tone: 'err', timeout: 5000 });
                    button.disabled = false;
                }
            });
            line.append(' ', button);
        }
        box.append(line);
    } else if (forecast.office_multi_channel && !forecast.channel) {
        box.append(el('p', 'hint', '该网点有多个渠道，无法推断。'));
    }
    return box;
};

// 按段分组：items 已按时间排好，连续同段的归成一组，每组前加段名（段只前进不回头，所以倒序时也是整段连续）
const groupBySegment = (items, segmentOf) => {
    const groups = [];
    for (const item of items) {
        const segment = segmentOf(item);
        if (!groups.length || groups[groups.length - 1].segment !== segment) groups.push({ segment, items: [] });
        groups[groups.length - 1].items.push(item);
    }
    return groups;
};

// 外链胶囊：a.pill + external-link 图标，新标签页打开
let iconSvgFn = null;
const extLink = (href, text) => {
    const link = el('a', 'pill pill-ext');
    link.href = href;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    if (iconSvgFn) link.insertAdjacentHTML('afterbegin', iconSvgFn('external-link'));
    link.append(text);
    return link;
};

// 17TRACK 登记状态一行：状态胶囊 + 登记 / 同步时间 + 出错原因
const buildTrack17Status = (view) => {
    const meta = track17StatusMeta(view.status);
    const line = el('p', 'task-meta');
    line.append(el('span', `pill ${meta.tone}`, meta.label));
    const parts = [];
    if (view.registered_at) parts.push(`登记 ${view.registered_at}`);
    if (view.last_synced_at) parts.push(`最后同步 ${view.last_synced_at}`);
    if (view.status === 'error' && view.error) parts.push(`原因：${view.error}`);
    if (parts.length) line.append(` ${parts.join(' · ')}`);
    return line;
};

const segmentHeading = (segment, region) => el('h4', 'section-name', segment ? segmentLabel(segment, region) : '其他');

// 合并履历：后端 route_nodes 合好的节点（日本时间，倒序，带 segment），按段分组，默认收起。
// 每个节点标来源小标：两边都报了的显示「中邮·日邮 一致」。
// 头部：日本邮政与 17TRACK 外链胶囊 + 17TRACK 登记状态（只有 CN 单号；没配密钥时只有胶囊）
const buildHistoryNode = (history, region, { links = {}, track17View = null } = {}) => {
    const box = el('details', 'disclosure flush');
    box.append(el('summary', '', `合并履历（${history.length} 个节点）`));
    const body = el('div', 'disclosure-body');
    const head = el('div', 'panel-head');
    head.append(el('h4', 'section-name', '履历来源'));
    const pills = el('span');
    if (links.jp_post) pills.append(extLink(links.jp_post, '日本邮政'));
    if (links.track17) pills.append(' ', extLink(links.track17, '17TRACK'));
    head.append(pills);
    body.append(head);
    if (track17View) body.append(buildTrack17Status(track17View));
    if (!history.length) body.append(el('p', 'hint', '还没有履历。'));
    for (const group of groupBySegment(history, (node) => node.segment || null)) {
        const list = el('ol', 'timeline compact');
        for (const node of group.items) {
            const row = el('li', 'timeline-item');
            row.append(el('span', 'dot idle'));
            const main = el('div', 'timeline-main');
            const top = el('div', 'timeline-head');
            const at = splitTime(node.at);
            top.append(el('span', 'timeline-title', node.name));
            if (node.sources.length > 1) {
                top.append(el('span', 'pill ok', `${node.sources.map((source) => nodeSourceMeta(source).label).join('·')} 一致`));
            } else {
                top.append(el('span', 'pill', nodeSourceMeta(node.sources[0]).label));
            }
            top.append(el('time', 'timeline-time', `${at.md} ${at.weekday} ${at.hm}`.trim()));
            main.append(top);
            const desc = [node.original ? `原文：${node.original}` : '', node.detail || ''].filter(Boolean).join(' · ');
            if (desc) main.append(el('p', 'timeline-desc', desc));
            row.append(main);
            list.append(row);
        }
        body.append(segmentHeading(group.segment, region), list);
    }
    if (history.length) body.append(el('p', 'hint', '时间均为日本时间；每段以主来源为准（国内段、中转段看中国邮政，日本段看日本邮政）。'));
    box.append(body);
    return box;
};

// 非常规件开关：归档任务没有编辑表单，在预测区块里切换（活跃任务在编辑表单里改）
const buildAtypicalNode = (taskId, atypical, onUpdated) => {
    const label = el('label', 'switch-row');
    const input = el('input', 'switch');
    input.type = 'checkbox';
    input.checked = Boolean(atypical);
    input.addEventListener('change', async () => {
        input.disabled = true;
        try {
            const result = await patchTask(taskId, { atypical: input.checked }, '设置非常规件失败');
            onUpdated?.(result);
        } catch (error) {
            input.checked = !input.checked;
            const { toast } = await import('@szyyw/design/toast.js');
            toast(error.message, { tone: 'err', timeout: 5000 });
        } finally {
            input.disabled = false;
        }
    });
    label.append(input, ' 非常规件（如大件电器）：作为预测参考时降低权重');
    return label;
};

// 预测区块：进度一行 → 收寄网点与渠道推断 →（非常规件开关）→ 合并履历 →（已比以往都慢）→ 剩余阶段列表 → 依据一行。
// 全部用 textContent，不拼 HTML。映射查不到的阶段 / 可信度会抛错，由调用方显示成加载失败。
// options：history 合并履历；task { archived, atypical }；links / track17 外链与登记状态；
// taskId + onUpdated(result) 用于「设为该渠道」与非常规件开关。
export const buildForecastNode = (forecast, {
    history = [], task = {}, links = {}, track17 = null, taskId = null, onUpdated = null,
} = {}) => {
    const region = forecast.transit_region;
    const names = { transit: region, origin: forecast.origin_name };
    const root = el('div', 'forecast-body');
    root.append(el('p', 'task-meta', progressText(forecast)));
    const office = buildOfficeNode(forecast, { taskId, onUpdated });
    if (office) root.append(office);
    if (taskId && task.archived) root.append(buildAtypicalNode(taskId, task.atypical, onUpdated));
    root.append(buildHistoryNode(history, region, { links, track17View: track17 }));
    if (!forecast.current || forecast.delivered) return root;

    if (forecast.confidence === 'none') {
        root.append(el('p', 'hint', '还没有可参考的历史，补录几单过去的单号后就能预测。'));
        return root;
    }
    if (forecast.overdue) {
        root.append(el('div', 'callout warn', '已比以往都慢：在这个阶段停留的时间超过了所有历史记录，下面按最快情况估计。'));
    }
    // 剩余阶段按段分组，每组前加段名
    for (const group of groupBySegment(forecast.remaining, (item) => stageMeta(item.stage).segment)) {
        const list = el('ol', 'timeline compact');
        for (const item of group.items) {
            const row = el('li', item.maybe ? 'timeline-item muted' : 'timeline-item');
            row.append(el('span', 'dot idle'));
            const main = el('div', 'timeline-main');
            const head = el('div', 'timeline-head');
            head.append(el('span', 'timeline-title', stageLabel(item.stage, names)));
            if (item.maybe) head.append(el('span', 'pill warn', '可能出现'));
            head.append(el('time', 'timeline-time', formatEta(item)));
            const basis = basisMeta(item.basis).label;
            const atypical = item.samples.atypical ? `，${item.samples.atypical} 单非常规已降权` : '';
            main.append(head, el('p', 'timeline-desc', `区间 ${formatRange(item)} · 参考 ${item.samples.total} 单（${basis} ${item.samples.similar} 单${atypical}）`));
            row.append(main);
            list.append(row);
        }
        root.append(segmentHeading(group.segment, region), list);
    }
    root.append(el('p', 'hint', basisText(forecast)));
    return root;
};

// 图标按需加载一次；失败时外链胶囊只显示文字
const loadIcons = async () => {
    if (iconSvgFn) return;
    try {
        ({ iconSvg: iconSvgFn } = await import('@szyyw/design/icons.js'));
    } catch (error) {
        iconSvgFn = null;
    }
};

// onUpdated(result)：「设为该渠道」成功后回调（管理后台用 result.user_state 刷新列表；门户页整页刷新）；
// onLoaded({ summary })：拉到后回调，任务卡片用它填摘要行；onFailed(error)：拉取或渲染失败（失败原因已显示在区块里）
export const renderForecast = async (container, taskId, { onUpdated = null, onLoaded = null, onFailed = null } = {}) => {
    container.replaceChildren(el('p', 'hint', '预测加载中…'));
    try {
        const [{ forecast, summary, history, task, links, track17 }] = await Promise.all([fetchForecast(taskId), loadIcons()]);
        container.replaceChildren(buildForecastNode(forecast, { history, task, links, track17, taskId, onUpdated }));
        onLoaded?.({ summary });
    } catch (error) {
        container.replaceChildren(el('p', 'hint', error.message || '加载预测失败。'));
        onFailed?.(error);
    }
};

// --- 补录对话框（包的 .overlay > .sheet，mountOverlay 管 Esc / 点遮罩 / 滚动锁 / 下拉关闭）---

const BACKFILL_HELP = '每行一个过去的日本邮政单号：单号 渠道 [下单日期]，用空格分隔，渠道必填、下单日期可选（YYYY-MM-DD）。'
    + '一次最多 30 行。新单号会建成已归档的任务并立即抓一次履历；已有的任务只补渠道和下单日期。';

const buildResultRow = (item) => {
    const meta = backfillResultMeta(item.status);
    const row = el('div', 'list-row multiline');
    const main = el('span', 'row-main');
    main.append(el('span', 'row-title', item.tracking_number || item.line));
    const sub = item.status === 'invalid' ? item.reason
        : item.status === 'added' ? `抓到 ${item.records} 条履历`
            : item.status === 'no_records' ? '日本邮政查不到（可能已过保留期），任务已保留，可以手填发货前节点'
                : '已有任务，渠道为空时已补上，下单日期已更新';
    main.append(el('span', 'row-sub', item.cn_note ? `${sub} · ${item.cn_note}` : sub));
    const end = el('span', 'row-end');
    end.append(el('span', `pill ${meta.tone}`, meta.label));
    row.append(main, end);
    return row;
};

// accountId：管理员替别人补录时传；onDone(result)：提交成功后回调（管理后台用它刷新列表）。
// 返回 Promise，对话框关闭时 resolve 为是否有成功补录 / 补充的行
export const openBackfillDialog = async ({ accountId = null, onDone = null } = {}) => {
    const { mountOverlay } = await import('@szyyw/design/overlay.js');
    const overlay = el('div', 'overlay');
    const sheet = el('div', 'sheet');
    sheet.setAttribute('role', 'dialog');
    sheet.setAttribute('aria-modal', 'true');
    sheet.setAttribute('aria-label', '补录历史单号');
    const grabber = el('div', 'sheet-grabber');
    grabber.setAttribute('aria-hidden', 'true');
    const head = el('div', 'sheet-head');
    const close = el('button', 'close-x');
    close.type = 'button';
    close.setAttribute('aria-label', '关闭');
    head.append(el('h2', 'sheet-title', '补录历史单号'), close);

    const body = el('div', 'sheet-body');
    const textarea = el('textarea', 'field mono');
    textarea.rows = 8;
    textarea.placeholder = 'EB000000001CN 渠道A 2026-09-01\nEB000000002CN 渠道B';
    textarea.setAttribute('aria-label', '要补录的单号，每行一个');
    const results = el('div', 'list divided u-mt-4');
    body.append(el('p', 'hint lead', BACKFILL_HELP), textarea, results);

    const foot = el('div', 'sheet-foot');
    const cancel = el('button', 'btn-ghost', '关闭');
    cancel.type = 'button';
    const submit = el('button', 'btn', '提交');
    submit.type = 'button';
    foot.append(cancel, submit);
    sheet.append(grabber, head, body, foot);
    overlay.append(sheet);

    let changed = false;
    return new Promise((resolve) => {
        let handle = null;
        const finish = () => {
            handle?.destroy();
            overlay.remove();
            resolve(changed);
        };
        handle = mountOverlay(overlay, { onClose: finish, dragClose: true });
        close.addEventListener('click', finish);
        cancel.addEventListener('click', finish);
        submit.addEventListener('click', async () => {
            submit.disabled = true;
            try {
                const response = await fetch('/api/tasks/backfill', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
                    credentials: 'same-origin',
                    body: JSON.stringify({ lines: textarea.value, account_id: accountId }),
                });
                const result = await readJson(response, '补录失败');
                results.replaceChildren(...result.results.map(buildResultRow));
                if (result.results.some((item) => item.status !== 'invalid')) changed = true;
                onDone?.(result);
            } catch (error) {
                const { toast } = await import('@szyyw/design/toast.js');
                toast(error.message, { tone: 'err', timeout: 5000 });
            } finally {
                submit.disabled = false;
            }
        });
        document.body.append(overlay);
        textarea.focus();
    });
};

// --- 门户页（服务端渲染）自动挂载 ---

// 预测区块展开着就立即拉；收起的（已归档的卡片）第一次展开时才拉。拉到后把摘要行填进同一张卡片的 [data-task-summary]
document.querySelectorAll('[data-task-forecast]').forEach((node) => {
    const taskId = node.dataset.taskForecast;
    const summary = document.querySelector(`[data-task-summary="${taskId}"]`);
    const load = () => renderForecast(node, taskId, {
        // 门户页是服务端渲染的，设了渠道就整页刷新，让任务卡上的渠道也更新
        onUpdated: () => window.location.reload(),
        onLoaded: (result) => {
            if (!summary) return;
            summary.querySelector('strong').textContent = result.summary;
            summary.hidden = false;
        },
        // 失败原因已显示在预测区块里，摘要行直接收起
        onFailed: () => {
            if (summary) summary.hidden = true;
        },
    });
    const box = node.closest('details');
    if (!box || box.open) {
        load();
        return;
    }
    const onToggle = () => {
        if (!box.open) return;
        box.removeEventListener('toggle', onToggle);
        load();
    };
    box.addEventListener('toggle', onToggle);
});

document.querySelectorAll('[data-backfill-open]').forEach((button) => {
    button.addEventListener('click', async () => {
        // 门户页的任务列表是服务端渲染的，补录过就整页刷新一次看到新的归档任务
        if (await openBackfillDialog()) window.location.reload();
    });
});
