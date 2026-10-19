// 任务时间线：事件类型的文案/配色映射 + 分页拉取 + 门户页（服务端渲染）的「加载更多」。
// 管理后台（app.js，Vue）动态 import 本文件复用映射与拉取；门户页直接以 module 引入。

// 与 storage.EVENT_KIND_META 一一对应（tests 会比对两边），不提供「未知类型」兜底：
// 后端新增类型时这里必须同步补上，漏了就在控制台报错而不是悄悄显示原值。
export const EVENT_KIND_META = Object.freeze({
    status_change: { label: '状态变化', tone: 'violet' },
    push_ok: { label: '推送成功', tone: 'green' },
    push_fail: { label: '推送失败', tone: 'red' },
    query_error: { label: '查询出错', tone: 'amber' },
    query_recovered: { label: '查询恢复', tone: 'cyan' },
});

export const eventKindMeta = (kind) => {
    if (!Object.prototype.hasOwnProperty.call(EVENT_KIND_META, kind)) {
        throw new Error(`未定义的事件类型：${kind}`);
    }
    return EVENT_KIND_META[kind];
};

export const EVENTS_PAGE_SIZE = 20;

// 返回 { events, has_more, next_before }；未登录 / 无权 / 出错时抛错，由调用方显示
export const fetchTaskEvents = async (taskId, { before = null, limit = EVENTS_PAGE_SIZE } = {}) => {
    const params = new URLSearchParams({ limit: String(limit) });
    if (before) params.set('before', String(before));
    const response = await fetch(`/api/tasks/${taskId}/events?${params}`, {
        headers: { Accept: 'application/json' },
        credentials: 'same-origin',
    });
    let result = null;
    try {
        result = await response.json();
    } catch (error) {
        result = null;
    }
    if (!response.ok || !result || result.status !== 'success') {
        throw new Error(result?.message || `加载时间线失败（HTTP ${response.status}）。`);
    }
    for (const event of result.events) eventKindMeta(event.kind);
    return result;
};

// 门户页的一行，结构与 user_portal.html 里服务端渲染的那一行保持一致；全部用 textContent，不拼 HTML
const buildRow = (event, currentNumber) => {
    const meta = eventKindMeta(event.kind);
    const row = document.createElement('li');
    row.className = event.kind === 'push_fail' ? 'timeline-row is-fail' : 'timeline-row';

    const time = document.createElement('time');
    time.className = 'timeline-time';
    time.textContent = event.created_at;

    const pill = document.createElement('span');
    pill.className = `pill ${meta.tone}`;
    pill.textContent = meta.label;

    const body = document.createElement('div');
    body.className = 'timeline-body';
    const status = document.createElement('span');
    status.className = 'timeline-status';
    status.textContent = event.status || '—';
    body.append(status);
    const extra = [event.detail, event.tracking_number && event.tracking_number !== currentNumber ? `旧单号 ${event.tracking_number}` : '']
        .filter(Boolean)
        .join(' · ');
    if (extra) {
        const detail = document.createElement('span');
        detail.className = 'timeline-detail';
        detail.textContent = extra;
        body.append(detail);
    }

    row.append(time, pill, body);
    return row;
};

const bindPortalTimeline = (root) => {
    const button = root.querySelector('[data-timeline-more]');
    const list = root.querySelector('.timeline-list');
    if (!button || !list) return;
    const taskId = root.dataset.taskTimeline;
    const currentNumber = root.dataset.number || '';
    button.addEventListener('click', async () => {
        button.disabled = true;
        try {
            const result = await fetchTaskEvents(taskId, { before: root.dataset.nextBefore });
            for (const event of result.events) list.append(buildRow(event, currentNumber));
            if (result.has_more && result.next_before) {
                root.dataset.nextBefore = String(result.next_before);
                button.disabled = false;
            } else {
                button.remove();
            }
        } catch (error) {
            button.disabled = false;
            const { toast } = await import('@szyyw/design/toast.js');
            toast(error.message, { tone: 'err', timeout: 5000 });
        }
    });
};

document.querySelectorAll('[data-task-timeline]').forEach(bindPortalTimeline);
