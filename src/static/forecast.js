// 到达预测：阶段 / 可信度 / 补录结果的文案映射 + 预测区块渲染 + 补录对话框。
// 管理后台（app.js，Vue）动态 import 本文件复用渲染与对话框；门户页直接以 module 引入，自动挂到 data-task-forecast / data-backfill-open 上。

// 与 forecast.STAGE_META 一一对应（tests 会比对两边），顺序即时间先后。不提供「未知阶段」兜底：
// 后端新增阶段时这里必须同步补上，漏了就报错而不是悄悄显示原值。
export const STAGE_META = Object.freeze({
    ordered: { label: '下单寄出' },
    packed: { label: '打包完成' },
    handed_over: { label: '出库交运' },
    accepted: { label: '引受' },
    origin_exchange: { label: '到达始发互换局' },
    origin_customs: { label: '始发国海关' },
    departed_origin: { label: '离开始发国' },
    arrived_japan: { label: '到达日本' },
    japan_customs: { label: '日本通关' },
    departed_exchange: { label: '离开日本互换局' },
    local_arrived: { label: '到达派送局' },
    out_for_delivery: { label: '派送中' },
    delivered: { label: '签收' },
});

// 与 forecast.CONFIDENCE_META 一一对应
export const CONFIDENCE_META = Object.freeze({
    none: { label: '还没有可参考的历史' },
    borrowed: { label: '同渠道还没有记录，借鉴其他渠道' },
    rough: { label: '同渠道样本较少，仅供粗估' },
    solid: { label: '同渠道样本充足' },
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
export const confidenceMeta = (key) => lookup(CONFIDENCE_META, key, '可信度');
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
    parts.push(`当前：${stageMeta(forecast.current.stage).label}（已等 ${formatDuration(forecast.current.waited_seconds)}）`);
    return parts.join(' · ');
};

export const basisText = (forecast) => {
    const { same_channel: same, other } = forecast.samples;
    const parts = [];
    if (same) parts.push(`同渠道 ${same} 单`);
    if (other) parts.push(`其他渠道 ${other} 单参考`);
    parts.push(confidenceMeta(forecast.confidence).label);
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

export const fetchForecast = async (taskId) => {
    const response = await fetch(`/api/tasks/${taskId}/forecast`, {
        headers: { Accept: 'application/json' },
        credentials: 'same-origin',
    });
    return (await readJson(response, '加载预测失败')).forecast;
};

const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
};

// 预测区块：进度一行 →（已比以往都慢）→ 剩余阶段列表 → 依据一行。全部用 textContent，不拼 HTML。
// 映射查不到的阶段 / 可信度会抛错，由调用方显示成加载失败。
export const buildForecastNode = (forecast) => {
    const root = el('div', 'forecast-body');
    root.append(el('p', 'task-meta', progressText(forecast)));
    if (!forecast.current || forecast.delivered) return root;

    if (forecast.confidence === 'none') {
        root.append(el('p', 'hint', '还没有可参考的历史，补录几单过去的单号后就能预测。'));
        return root;
    }
    if (forecast.overdue) {
        root.append(el('div', 'callout warn', '已比以往都慢：在这个阶段停留的时间超过了所有历史记录，下面按最快情况估计。'));
    }
    if (forecast.remaining.length) {
        const list = el('ol', 'timeline compact');
        for (const item of forecast.remaining) {
            const row = el('li', item.maybe ? 'timeline-item muted' : 'timeline-item');
            row.append(el('span', 'dot idle'));
            const main = el('div', 'timeline-main');
            const head = el('div', 'timeline-head');
            head.append(el('span', 'timeline-title', stageMeta(item.stage).label));
            if (item.maybe) head.append(el('span', 'pill warn', '可能出现'));
            head.append(el('time', 'timeline-time', formatEta(item)));
            main.append(head, el('p', 'timeline-desc', `区间 ${formatRange(item)}`));
            row.append(main);
            list.append(row);
        }
        root.append(list);
    }
    root.append(el('p', 'hint', basisText(forecast)));
    return root;
};

export const renderForecast = async (container, taskId) => {
    container.replaceChildren(el('p', 'hint', '预测加载中…'));
    try {
        const forecast = await fetchForecast(taskId);
        container.replaceChildren(buildForecastNode(forecast));
    } catch (error) {
        container.replaceChildren(el('p', 'hint', error.message || '加载预测失败。'));
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
    main.append(el('span', 'row-sub', sub));
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

document.querySelectorAll('[data-task-forecast]').forEach((node) => {
    renderForecast(node, node.dataset.taskForecast);
});

document.querySelectorAll('[data-backfill-open]').forEach((button) => {
    button.addEventListener('click', async () => {
        // 门户页的任务列表是服务端渲染的，补录过就整页刷新一次看到新的归档任务
        if (await openBackfillDialog()) window.location.reload();
    });
});
