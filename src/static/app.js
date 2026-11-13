const { createApp, ref, onMounted, nextTick, computed, watch } = Vue;

const MAX_LOG_LINES = 500;

// 包的 toast 是 ES module，本文件是普通脚本，用动态 import 取（裸说明符由模板里的 import map 解析到 CDN）
const toastReady = import('@szyyw/design/toast.js');
// 时间线的事件类型映射与分页拉取和门户页共用一份（/static/timeline.js，ES module）
const timelineReady = import('/static/timeline.js');
// 到达预测的渲染与补录对话框和门户页共用一份（/static/forecast.js，ES module）
const forecastReady = import('/static/forecast.js');
// 删除确认用包的对话框（danger 样式）
const confirmReady = import('@szyyw/design/confirm.js');

// 日志行用 v-html 渲染（要保留换行），内容来自子进程输出与公网 Bark 日志，可能含任意文本：
// 必须先整行 HTML 转义，再把换行换成 <br>，否则日志里的标签会被当成 HTML 执行（XSS）
const escapeHtml = (text) => String(text ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[ch]));
const logHtml = (text) => escapeHtml(text).replace(/\n/g, '<br>');

// 发货前节点的三个输入框（key 与后端 forecast.PRE_STAGES 一致，写错了后端会 400）
const MILESTONE_INPUTS = [
    { stage: 'ordered', label: '下单' },
    { stage: 'packed', label: '打包' },
    { stage: 'handed_over', label: '出库' },
];
const buildMilestones = (milestones = {}) =>
    Object.fromEntries(MILESTONE_INPUTS.map(({ stage }) => [stage, milestones[stage]?.at || '']));
const milestoneCount = (form) => Object.values(form?.milestones || {}).filter((value) => String(value).trim()).length;

const buildNewTaskForm = () => ({
    tracking_number: '',
    label: '',
    check_interval: 300,
    enabled: true,
    channel: '',
    milestones: buildMilestones(),
});

// 每条任务一份可编辑草稿；启用开关不进草稿，避免"改了单号顺手被一起保存"。
// 节点原样回填，原样保存时后端不会把调用方写的节点改成手填
const buildTaskDraft = (task = {}) => ({
    tracking_number: task.tracking_number || '',
    label: task.label || '',
    check_interval: Number(task.check_interval || 300),
    channel: task.channel || '',
    atypical: Boolean(task.atypical),
    milestones: buildMilestones(task.milestones || {}),
});

const app = createApp({
    setup() {
        const title = ref('快递追踪控制台');
        const socket = ref(null);
        const page = ref('home');
        const logTab = ref('remote');

        const viewer = ref(window.initialViewerState || {});
        const barkHelp = ref(window.initialBarkHelp || {});
        const envVars = ref(window.initialEnvVars || {});
        const originalEnvVars = ref({ ...envVars.value });
        // 分组/文案/生效时机由后端 SYSTEM_ENV_GROUPS 下发，前端不再自带一份说明字典
        const envGroups = ref(window.initialEnvSchema || []);
        const envApplyNote = ref('');
        const initialUserState = window.initialUserState || { users: [], task_count: 0, active_task_count: 0, login_count: 0 };

        // 与后端 _env_enabled 同一套真值；空值落回字段默认值，否则"没写进 .env 但默认开"
        // 的开关（AUTO_START_TRACKER / LOCAL_BARK_ENABLED）在界面上会显示成关闭
        const ENV_TRUTHY = ['1', 'true', 'yes', 'on'];

        const envBoolValue = (field) => {
            const raw = String(envVars.value[field.key] ?? '').trim();
            const effective = raw === '' ? String(field.default ?? '0') : raw;
            return ENV_TRUTHY.includes(effective.toLowerCase());
        };

        const setEnvBool = (field, on) => {
            envVars.value[field.key] = on ? '1' : '0';
        };

        // 按键查字段定义，供跨字段联动用
        const envFields = computed(() => {
            const map = {};
            for (const group of envGroups.value) {
                for (const field of group.fields || []) map[field.key] = field;
            }
            return map;
        });

        // 依赖项关着时本项无效，置灰比只在说明里写一句更不容易误会
        const envFieldLocked = (field) => {
            if (!field.requires) return false;
            const dep = envFields.value[field.requires];
            return dep ? !envBoolValue(dep) : false;
        };

        const envChangedEntries = computed(() => {
            const changed = {};
            for (const [key, value] of Object.entries(envVars.value)) {
                if (originalEnvVars.value[key] !== value) {
                    changed[key] = value;
                }
            }
            return changed;
        });

        const envDirtyCount = computed(() => Object.keys(envChangedEntries.value).length);

        // 17TRACK 区块：切到设置页、或改了密钥后拉一次（额度由服务端缓存 5 分钟）
        const track17Card = ref(null);
        const extLinkIcon = ref('');
        const loadTrack17Card = async () => {
            try {
                const { iconSvg } = await import('@szyyw/design/icons.js');
                extLinkIcon.value = iconSvg('external-link');
            } catch (error) {
                extLinkIcon.value = '';
            }
            try {
                const response = await fetch('/api/admin/track17', { headers: { Accept: 'application/json' }, credentials: 'same-origin' });
                const result = await response.json();
                track17Card.value = response.ok && result.status === 'success'
                    ? result
                    : { configured: true, quota_ok: false, numbers: { registered: 0, pending: 0, synced: 0, error: 0 } };
            } catch (error) {
                track17Card.value = { configured: true, quota_ok: false, numbers: { registered: 0, pending: 0, synced: 0, error: 0 } };
            }
        };
        const track17UsagePct = computed(() => {
            const quota = track17Card.value?.quota;
            if (!quota || !quota.quota_total) return 0;
            return (Number(quota.quota_used || 0) / Number(quota.quota_total)) * 100;
        });

        // 状态规则：切到设置页时拉一次；编辑后立即生效（来源记为手工）
        const statusRules = ref([]);
        const statusRuleOptions = ref({ sources: {}, kinds: {}, stages: {} });
        const statusRuleDrafts = ref({});
        // 规则状态穷举（storage.STATUS_RULE_STATES）+ 来源（ai / manual / code）
        const STATUS_RULE_STATE_META = {
            pending: { label: '待归类', tone: 'cyan' },
            failed: { label: '归类失败', tone: 'err' },
        };
        const STATUS_RULE_ORIGIN_META = {
            ai: { label: '自动归类', tone: 'ok' },
            manual: { label: '手工', tone: 'violet' },
            code: { label: '已并入代码', tone: 'ok' },
        };
        const statusRuleState = (rule) => {
            if (rule.status === 'done') return STATUS_RULE_ORIGIN_META[rule.origin];
            return STATUS_RULE_STATE_META[rule.status];
        };
        const loadStatusRules = async () => {
            try {
                const response = await fetch('/api/admin/status-rules', { headers: { Accept: 'application/json' }, credentials: 'same-origin' });
                const result = await handleApiResponse(response);
                if (!result || result.status !== 'success') return;
                statusRules.value = result.rules;
                statusRuleOptions.value = result.options;
            } catch (error) {
                notify('读取状态规则时发生错误。', 'error');
            }
        };
        const editStatusRule = (rule) => {
            statusRuleDrafts.value = {
                ...statusRuleDrafts.value,
                [rule.id]: { name_zh: rule.name_zh || rule.raw_status, kind: rule.kind || '', stage: rule.stage || '', push: Boolean(rule.push) },
            };
        };
        const cancelStatusRule = (rule) => {
            const drafts = { ...statusRuleDrafts.value };
            delete drafts[rule.id];
            statusRuleDrafts.value = drafts;
        };
        const sendStatusRuleRequest = async (url, method, body, errorText) => {
            try {
                const response = await fetch(url, {
                    method,
                    headers: { 'Content-Type': 'application/json' },
                    body: body === undefined ? undefined : JSON.stringify(body),
                });
                const result = await handleApiResponse(response);
                if (!result) return null;
                notify(result.message, result.status === 'success' ? 'success' : 'error');
                return result.status === 'success' ? result : null;
            } catch (error) {
                notify(errorText, 'error');
                return null;
            }
        };
        const saveStatusRule = async (rule) => {
            const draft = statusRuleDrafts.value[rule.id];
            if (!draft) return;
            const result = await sendStatusRuleRequest(`/api/admin/status-rules/${rule.id}`, 'PUT', {
                name_zh: draft.name_zh, kind: draft.kind || null, stage: draft.stage || null, push: Boolean(draft.push),
            }, '保存状态规则时发生错误。');
            if (!result) return;
            cancelStatusRule(rule);
            await loadStatusRules();
        };
        const reclassifyStatusRule = async (rule) => {
            if (await sendStatusRuleRequest(`/api/admin/status-rules/${rule.id}/reclassify`, 'POST', undefined, '重新归类时发生错误。')) {
                await loadStatusRules();
            }
        };
        const deleteStatusRule = async (rule) => {
            const { confirm } = await confirmReady;
            const confirmed = await confirm({
                title: '删除状态规则',
                message: `确认删除「${rule.raw_status}」的规则？这个状态以后再出现时会重新登记为待归类。`,
                confirmLabel: '删除',
                danger: true,
            });
            if (!confirmed) return;
            if (await sendStatusRuleRequest(`/api/admin/status-rules/${rule.id}`, 'DELETE', undefined, '删除状态规则时发生错误。')) {
                await loadStatusRules();
            }
        };

        const resetEnv = () => {
            envVars.value = { ...originalEnvVars.value };
            envApplyNote.value = '';
        };

        // 账号表单只管身份与 Bark；单号/间隔属于任务，走 taskDrafts
        const buildUserForm = (user = {}) => ({
            id: user.id || null,
            username: user.username || '',
            display_name: user.display_name || user.name || '',
            role: user.role || 'user',
            note: user.note || '',
            bark_keys: user.bark_keys || '',
            bark_query_params: user.bark_query_params || '?sound=minuet&level=timeSensitive',
            bark_url_enabled: Boolean(user.bark_url_enabled),
            login_enabled: user.login_enabled !== false,
            portal_sub: user.portal_sub || '',
            new_password: '',
            task_id: '',
            test_title: '测试推送',
            test_body: '这是 Bark 参数预览消息，用来确认通知样式、声音和链接效果。',
        });

        const buildNewUserForm = () => ({
            username: '',
            display_name: '',
            password: '',
            portal_sub: '',
            role: 'user',
            login_enabled: true,
        });

        const users = ref(initialUserState.users || []);
        const taskTotals = ref({
            task_count: Number(initialUserState.task_count || 0),
            active_task_count: Number(initialUserState.active_task_count || 0),
        });
        // 用户页不默认选中账号——旧版默认选第一个非 admin 用户，
        // 结果管理员一进来看到的"当前账号"是别人，表单改的也是别人的数据
        const selectedUserId = ref(null);
        const userForm = ref(buildUserForm({}));
        const newUserForm = ref(buildNewUserForm());
        const taskDrafts = ref({});

        // 管理员自己的账号：以账号列表里的那条为准（任务操作后它会被刷新），
        // 列表意外缺失时退回 viewer 里的快照
        const viewerAccountId = computed(() => Number(viewer.value?.account?.id || 0) || null);
        const initialViewerAccount = viewer.value?.account || {};
        const meForm = ref(
            buildUserForm(
                (initialUserState.users || []).find((user) => user.id === initialViewerAccount.id) || initialViewerAccount
            )
        );

        const rebuildTaskDrafts = () => {
            const drafts = {};
            users.value.forEach((user) => {
                (user.tasks || []).concat(user.archived_tasks || []).forEach((task) => {
                    drafts[task.id] = buildTaskDraft(task);
                });
            });
            taskDrafts.value = drafts;
        };
        rebuildTaskDrafts();

        const createUserExpanded = ref(false);
        const sendingTestPush = ref(false);
        const sendingMeTestPush = ref(false);

        // 操作的瞬时反馈走包的 toast()（底部居中、成功 3 秒 / 错误 5 秒自动消失），不再各页自写一条淡出行
        const notify = (text, type) => {
            if (!text) return;
            toastReady
                .then(({ toast }) => toast(text, { tone: type === 'error' ? 'err' : 'ok', timeout: type === 'error' ? 5000 : 3000 }))
                .catch(() => {});
        };

        const countBarkKeys = (value) =>
            String(value || '')
                .split(/\n+/)
                .map((item) => item.trim())
                .filter(Boolean).length;

        const activeTaskCount = computed(() => Number(taskTotals.value.active_task_count || 0));
        const totalTaskCount = computed(() => Number(taskTotals.value.task_count || 0));
        const loginUsersCount = computed(() => users.value.filter((user) => user.login_enabled).length);

        // known：收到过一次脚本状态才知道它到底跑没跑，之前不显示「未运行」提示，免得一进页面闪一下
        const script = ref({ running: false, known: false, logs: [] });
        const bark = ref({ running: false, logs: [] });
        const remoteBarkLogs = ref([]);
        const remoteBark = ref({
            loading: false,
            configured: false,
            url: '',
            checked_url: '',
            ok: false,
            status_code: null,
            latency_ms: null,
            error: null,
            fallback_used: false,
            note: null,
            checked_at: ''
        });
        const remoteRefreshMode = ref('manual');
        const remoteAutoMin = ref(0);
        const remoteAutoSec = ref(30);
        const remoteAutoMinDraft = ref(0);
        const remoteAutoSecDraft = ref(30);
        let remoteAutoTimer = null;

        const trackerLogOutput = ref(null);
        const barkLogOutput = ref(null);
        const remoteBarkLogOutput = ref(null);

        const selectedUser = computed(() => users.value.find((user) => user.id === selectedUserId.value) || null);

        // 任务状态的唯一判据：weight 兼作首页排序权重，越小越该被先看到
        const taskState = (task) => {
            if (task.archived) {
                return { key: 'archived', tone: '', label: '已归档', weight: 5 };
            }
            // 停用优先于异常：用户主动停掉的任务不该被当成"出问题了"催着处理，
            // 它留着的 last_error 只是停用前最后一次轮询的残留
            if (!task.enabled) {
                return { key: 'paused', tone: '', label: '已停用', weight: 4 };
            }
            if (task.last_error) {
                return { key: 'error', tone: 'err', label: '轮询异常', weight: 0 };
            }
            if (!script.value.running) {
                return { key: 'idle', tone: 'warn', label: '待启动脚本', weight: 1 };
            }
            if (!task.last_checked_at) {
                return { key: 'fresh', tone: 'warn', label: '尚未检查', weight: 2 };
            }
            return { key: 'ok', tone: 'ok', label: '追踪中', weight: 3 };
        };
        const countErrorTasks = (tasks) => tasks.filter((task) => taskState(task).key === 'error').length;

        // 在途卡片的顺序：异常最前；同状态内按最近检查时间倒序，久没动静的沉到后面
        const sortActive = (tasks) =>
            [...tasks].sort((left, right) => {
                const byState = taskState(left).weight - taskState(right).weight;
                if (byState !== 0) return byState;
                return String(right.last_checked_at || '').localeCompare(String(left.last_checked_at || ''));
            });

        // ---------- 首页：自己的单号在上，其他账号按账号分组 ----------

        const meAccount = computed(
            () => users.value.find((user) => user.id === viewerAccountId.value) || viewer.value?.account || null
        );
        const meTasks = computed(() => meAccount.value?.tasks || []);
        const meErrorTaskCount = computed(() => countErrorTasks(meTasks.value));
        // 设备数跟着输入框实时变，比保存后的账号字段更贴合"我现在填了几个"
        const meDeviceCount = computed(() => countBarkKeys(meForm.value.bark_keys));

        // 账号列表的顺序（id 升序）就是分组顺序，不随状态跳动，方便从用户页跳过来找
        const otherGroups = computed(() =>
            users.value
                .filter((user) => user.id !== viewerAccountId.value)
                .map((user) => ({
                    user,
                    tasks: sortActive(user.tasks || []),
                    errors: countErrorTasks(user.tasks || []),
                }))
        );

        // 已归档：自己的在前，其他账号的跟在后面（卡片上标归属账号）
        const archivedTasks = computed(() => {
            const mine = meAccount.value?.archived_tasks || [];
            const others = users.value
                .filter((user) => user.id !== viewerAccountId.value)
                .flatMap((user) => user.archived_tasks || []);
            return mine.concat(others);
        });
        const archivedOpen = ref(false);
        const ownerName = (task) => {
            const owner = users.value.find((user) => user.id === task.account_id);
            return owner ? owner.display_name || owner.username : '';
        };

        // 其他账号分组里的「添加单号」表单，按账号各自展开
        const groupAddOpen = ref({});
        const toggleGroupAdd = (accountId) => {
            groupAddOpen.value = { ...groupAddOpen.value, [accountId]: !groupAddOpen.value[accountId] };
        };

        // 任务操作后不重建账号表单，否则会把正在编辑的账号字段冲掉
        const syncUserState = (state, { keepUserForm = false } = {}) => {
            users.value = state?.users || [];
            taskTotals.value = {
                task_count: Number(state?.task_count || 0),
                active_task_count: Number(state?.active_task_count || 0),
            };
            rebuildTaskDrafts();
            if (selectedUserId.value && !users.value.some((user) => user.id === selectedUserId.value)) {
                selectedUserId.value = null;
            }
            if (!keepUserForm) {
                userForm.value = buildUserForm(users.value.find((user) => user.id === selectedUserId.value) || {});
            }
        };

        // 用户页「在首页看这个账号的单号」：切到首页并滚到该账号的分组（自己的是「我的单号」）
        const goToAccountTasks = async (accountId) => {
            page.value = 'home';
            await nextTick();
            const anchor = document.getElementById(accountId === viewerAccountId.value ? 'account-me' : `account-${accountId}`);
            anchor?.scrollIntoView({ behavior: 'smooth', block: 'start' });
        };

        const redirectToLogin = () => {
            const next = encodeURIComponent(window.location.pathname + window.location.search);
            window.location.href = `/login?next=${next}`;
        };

        const handleApiResponse = async (response) => {
            if (response.status === 401) {
                redirectToLogin();
                return null;
            }
            return response.json();
        };

        const scrollToBottom = (element) => {
            if (!element) return;
            const shouldScroll = element.scrollHeight - element.clientHeight <= element.scrollTop + 50;
            if (shouldScroll) {
                element.scrollTop = element.scrollHeight;
            }
        };

        const startScript = () => socket.value?.emit('start_script');
        const stopScript = () => socket.value?.emit('stop_script');
        const startBarkServer = () => socket.value?.emit('start_bark_server');
        const stopBarkServer = () => socket.value?.emit('stop_bark_server');

        const fetchRemoteBarkStatus = async () => {
            remoteBark.value.loading = true;
            try {
                const response = await fetch('/remote_bark_status', { cache: 'no-store' });
                const data = await handleApiResponse(response);
                if (!data) return;
                remoteBark.value = {
                    ...remoteBark.value,
                    ...data,
                    loading: false,
                    checked_at: new Date().toLocaleString()
                };
            } catch (error) {
                remoteBark.value = {
                    ...remoteBark.value,
                    loading: false,
                    ok: false,
                    error: String(error),
                    checked_at: new Date().toLocaleString()
                };
            }
        };

        const clearRemoteAutoTimer = () => {
            if (remoteAutoTimer) {
                clearInterval(remoteAutoTimer);
                remoteAutoTimer = null;
            }
        };

        const startRemoteAutoTimer = () => {
            clearRemoteAutoTimer();
            const intervalMs = (Number(remoteAutoMin.value) * 60 + Number(remoteAutoSec.value)) * 1000;
            if (remoteRefreshMode.value !== 'auto' || !intervalMs || intervalMs < 1000) return;
            remoteAutoTimer = setInterval(fetchRemoteBarkStatus, intervalMs);
        };

        const applyRemoteAutoInterval = () => {
            remoteAutoMin.value = Number(remoteAutoMinDraft.value) || 0;
            remoteAutoSec.value = Number(remoteAutoSecDraft.value) || 0;
            startRemoteAutoTimer();
            if (remoteRefreshMode.value === 'auto') {
                fetchRemoteBarkStatus();
            }
        };

        const saveEnv = async () => {
            try {
                // 只提交有改动的字段：整表提交会把留空的可选项显式写成空串
                const changed = { ...envChangedEntries.value };
                envApplyNote.value = '';
                const response = await fetch('/update_env', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(changed)
                });
                const result = await handleApiResponse(response);
                if (!result) return;
                notify(result.message, result.status === 'success' ? 'success' : 'error');
                envApplyNote.value = result.apply_note || '';
                if (result.status === 'success' && result.env_vars) {
                    envVars.value = { ...result.env_vars };
                    originalEnvVars.value = { ...result.env_vars };
                    if ('TRACK17_API_KEY' in changed) loadTrack17Card();
                }
                if (result.status === 'success' && result.bark_help) {
                    barkHelp.value = { ...result.bark_help };
                }
            } catch (error) {
                notify('保存系统设置时发生错误。', 'error');
            }
        };

        const selectUser = (userId) => {
            selectedUserId.value = userId;
        };

        const createUser = async () => {
            try {
                const response = await fetch('/api/users', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ ...newUserForm.value })
                });
                const result = await handleApiResponse(response);
                if (!result) return;
                notify(result.message, result.status === 'success' ? 'success' : 'error');
                if (result.status === 'success') {
                    syncUserState(result.user_state);
                    if (result.user) {
                        selectedUserId.value = result.user.id;
                    }
                    newUserForm.value = buildNewUserForm();
                    createUserExpanded.value = false;
                }
            } catch (error) {
                notify('创建账号时发生错误。', 'error');
            }
        };

        const saveUser = async () => {
            if (!selectedUserId.value) return;
            try {
                const response = await fetch(`/api/users/${selectedUserId.value}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ ...userForm.value })
                });
                const result = await handleApiResponse(response);
                if (!result) return;
                notify(result.message, result.status === 'success' ? 'success' : 'error');
                if (result.status === 'success') {
                    syncUserState(result.user_state);
                    if (result.user) {
                        userForm.value = buildUserForm(result.user);
                    }
                }
            } catch (error) {
                notify('保存账号时发生错误。', 'error');
            }
        };

        const savePortalSub = async () => {
            if (!selectedUserId.value) return;
            try {
                const response = await fetch(`/api/admin/accounts/${selectedUserId.value}/portal-user`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ portal_sub: userForm.value.portal_sub.trim() || null })
                });
                const result = await handleApiResponse(response);
                if (!result) return;
                notify(result.message, result.status === 'success' ? 'success' : 'error');
                if (result.status === 'success') {
                    syncUserState(result.user_state);
                    if (result.user) {
                        userForm.value = { ...userForm.value, portal_sub: result.user.portal_sub || '' };
                    }
                }
            } catch (error) {
                notify('保存 portal 映射时发生错误。', 'error');
            }
        };

        const sendUserTestPush = async () => {
            if (!selectedUserId.value || sendingTestPush.value) return;
            sendingTestPush.value = true;
            try {
                const response = await fetch(`/api/users/${selectedUserId.value}/test_push`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ ...userForm.value })
                });
                const result = await handleApiResponse(response);
                if (!result) return;
                notify(result.message, result.status === 'success' ? 'success' : 'error');
            } catch (error) {
                notify('发送测试推送失败。', 'error');
            } finally {
                sendingTestPush.value = false;
            }
        };

        // 账号列表兼作全局指标来源；/api/me 只回自己那份，所以保存后要单独拉一次
        const refreshUserState = async () => {
            try {
                const response = await fetch('/api/users', { cache: 'no-store' });
                const state = await handleApiResponse(response);
                if (!state) return;
                // 用户页可能正编辑到一半，只有它选中的正是自己时才有必要重建那份表单
                syncUserState(state, { keepUserForm: selectedUserId.value !== viewerAccountId.value });
            } catch (error) {
                /* 刷新失败不影响刚才那次保存的结果，静默忽略 */
            }
        };

        // 只提交资料与 Bark 字段：用户名/角色/登录开关不进这个表单，
        // 免得管理员在自己的页面上把自己降权或关掉登录，把自己锁在门外
        const saveMe = async () => {
            try {
                const response = await fetch('/api/me', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        display_name: meForm.value.display_name,
                        note: meForm.value.note,
                        bark_keys: meForm.value.bark_keys,
                        bark_query_params: meForm.value.bark_query_params,
                        bark_url_enabled: meForm.value.bark_url_enabled,
                        new_password: meForm.value.new_password,
                    }),
                });
                const result = await handleApiResponse(response);
                if (!result) return;
                notify(result.message, result.status === 'success' ? 'success' : 'error');
                if (result.status === 'success' && result.user) {
                    meForm.value = buildUserForm(result.user);
                    viewer.value = {
                        ...viewer.value,
                        account: result.user,
                        display_name: result.user.display_name,
                        username: result.user.username,
                    };
                    await refreshUserState();
                }
            } catch (error) {
                notify('保存个人设置时发生错误。', 'error');
            }
        };

        const sendMeTestPush = async () => {
            if (!viewerAccountId.value || sendingMeTestPush.value) return;
            sendingMeTestPush.value = true;
            try {
                const response = await fetch(`/api/users/${viewerAccountId.value}/test_push`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ ...meForm.value })
                });
                const result = await handleApiResponse(response);
                if (!result) return;
                notify(result.message, result.status === 'success' ? 'success' : 'error');
            } catch (error) {
                notify('发送测试推送失败。', 'error');
            } finally {
                sendingMeTestPush.value = false;
            }
        };

        // 任务类请求：成功后就地刷新账号列表，保留正在编辑的账号表单；返回接口结果（失败时为 undefined）
        const sendTaskRequest = async (url, method, body, errorText) => {
            try {
                const response = await fetch(url, {
                    method,
                    headers: { 'Content-Type': 'application/json' },
                    body: body === undefined ? undefined : JSON.stringify(body)
                });
                const result = await handleApiResponse(response);
                if (!result) return;
                notify(result.message, result.status === 'success' ? 'success' : 'error');
                if (result.status === 'success' && result.user_state) {
                    syncUserState(result.user_state, { keepUserForm: true });
                }
                return result;
            } catch (error) {
                notify(errorText, 'error');
            }
        };

        // 添加单号：落在 accountId 名下（首页顶部 = 自己，分组里 = 那个账号）
        const createTask = (accountId, form) =>
            sendTaskRequest(`/api/users/${accountId}/tasks`, 'POST', { ...form }, '添加追踪任务时发生错误。');

        const saveTask = (task) => {
            const draft = taskDrafts.value[task.id];
            if (!draft) return;
            return sendTaskRequest(`/api/tasks/${task.id}`, 'PUT', { ...draft }, '保存追踪任务时发生错误。');
        };

        const toggleTask = (task) =>
            sendTaskRequest(`/api/tasks/${task.id}`, 'PUT', { enabled: !task.enabled }, '切换任务状态时发生错误。');

        const archiveTask = (task) =>
            sendTaskRequest(`/api/tasks/${task.id}/archive`, 'POST', undefined, '归档追踪任务时发生错误。');

        // 取消归档 = 同单号重新添加到原账号名下（storage 侧复活，保留首次登记时间与次数、渠道与节点）
        const restoreTask = (task) =>
            sendTaskRequest(
                `/api/users/${task.account_id}/tasks`,
                'POST',
                {
                    tracking_number: task.tracking_number,
                    label: task.label || '',
                    check_interval: Number(task.check_interval || 300),
                    enabled: true,
                },
                '恢复追踪任务时发生错误。'
            );

        const deleteTask = async (task) => {
            const { confirm } = await confirmReady;
            const confirmed = await confirm({
                title: '删除追踪任务',
                message: `确认删除单号 ${task.tracking_number} 的追踪任务？删除后档案也会一起消失。`,
                confirmLabel: '删除',
                danger: true,
            });
            if (!confirmed) return;
            return sendTaskRequest(`/api/tasks/${task.id}`, 'DELETE', undefined, '删除追踪任务时发生错误。');
        };

        // 任务卡片的操作统一从这里走（卡片组件只管展示与草稿）
        const taskActions = {
            save: saveTask,
            toggle: toggleTask,
            archive: archiveTask,
            restore: restoreTask,
            remove: deleteTask,
        };

        // 补录历史单号：补到 accountId 名下
        const openBackfill = async (accountId) => {
            if (!accountId) return;
            const { openBackfillDialog } = await forecastReady;
            await openBackfillDialog({
                accountId,
                onDone: (result) => {
                    if (result.user_state) syncUserState(result.user_state, { keepUserForm: true });
                },
            });
        };

        // 切到设置页时刷新 17TRACK 卡片与状态规则（额度在服务端缓存，频繁切换也不会打到 17TRACK）
        watch(page, (next) => {
            if (next === 'settings') {
                loadTrack17Card();
                loadStatusRules();
            }
        });

        window.addEventListener('forecast-task-updated', (event) => {
            const result = event.detail || {};
            if (result.user_state) syncUserState(result.user_state, { keepUserForm: true });
        });

        watch(selectedUserId, (nextUserId) => {
            userForm.value = buildUserForm(users.value.find((user) => user.id === nextUserId) || {});
        });

        watch(remoteRefreshMode, (mode) => {
            if (mode === 'auto') {
                remoteAutoMinDraft.value = remoteAutoMin.value;
                remoteAutoSecDraft.value = remoteAutoSec.value;
                startRemoteAutoTimer();
                fetchRemoteBarkStatus();
            } else {
                clearRemoteAutoTimer();
            }
        });

        onMounted(() => {
            socket.value = io({ withCredentials: true });

            socket.value.on('connect_error', (error) => {
                const message = String(error?.message || error || '').toLowerCase();
                if (message.includes('unauthorized') || message.includes('auth')) {
                    redirectToLogin();
                }
            });
            socket.value.on('auth_error', () => redirectToLogin());

            socket.value.on('script_status', (data) => {
                script.value.running = data.running;
                script.value.known = true;
            });
            socket.value.on('bark_server_status', (data) => {
                bark.value.running = data.running;
            });
            socket.value.on('tracker_log', async (data) => {
                script.value.logs.push(logHtml(data.data));
                if (script.value.logs.length > MAX_LOG_LINES) {
                    script.value.logs.splice(0, script.value.logs.length - MAX_LOG_LINES);
                }
                await nextTick();
                scrollToBottom(trackerLogOutput.value);
            });
            socket.value.on('bark_log', async (data) => {
                bark.value.logs.push(logHtml(data.data));
                if (bark.value.logs.length > MAX_LOG_LINES) {
                    bark.value.logs.splice(0, bark.value.logs.length - MAX_LOG_LINES);
                }
                await nextTick();
                scrollToBottom(barkLogOutput.value);
            });
            socket.value.on('full_tracker_log', async (data) => {
                const lines = data.data ? data.data.split('\n').map(logHtml) : [];
                script.value.logs = lines.length > MAX_LOG_LINES ? lines.slice(-MAX_LOG_LINES) : lines;
                await nextTick();
                scrollToBottom(trackerLogOutput.value);
            });
            socket.value.on('full_bark_log', async (data) => {
                const lines = data.data ? data.data.split('\n').map(logHtml) : [];
                bark.value.logs = lines.length > MAX_LOG_LINES ? lines.slice(-MAX_LOG_LINES) : lines;
                await nextTick();
                scrollToBottom(barkLogOutput.value);
            });
            socket.value.on('remote_bark_log', async (data) => {
                remoteBarkLogs.value.push(logHtml(data.data));
                if (remoteBarkLogs.value.length > MAX_LOG_LINES) {
                    remoteBarkLogs.value.splice(0, remoteBarkLogs.value.length - MAX_LOG_LINES);
                }
                await nextTick();
                scrollToBottom(remoteBarkLogOutput.value);
            });
            socket.value.on('full_remote_bark_log', async (data) => {
                const lines = data.data ? data.data.split('\n').map(logHtml) : [];
                remoteBarkLogs.value = lines.length > MAX_LOG_LINES ? lines.slice(-MAX_LOG_LINES) : lines;
                await nextTick();
                scrollToBottom(remoteBarkLogOutput.value);
            });

            fetchRemoteBarkStatus();
        });

        return {
            title,
            page,
            logTab,
            viewer,
            barkHelp,
            envVars,
            originalEnvVars,
            track17Card,
            track17UsagePct,
            statusRules,
            statusRuleOptions,
            statusRuleDrafts,
            statusRuleState,
            loadStatusRules,
            editStatusRule,
            cancelStatusRule,
            saveStatusRule,
            reclassifyStatusRule,
            deleteStatusRule,
            extLinkIcon,
            envGroups,
            envFieldLocked,
            envBoolValue,
            setEnvBool,
            envDirtyCount,
            envApplyNote,
            resetEnv,
            users,
            selectedUserId,
            selectedUser,
            taskState,
            sortActive,
            taskDrafts,
            taskActions,
            userForm,
            newUserForm,
            createUserExpanded,
            sendingTestPush,
            activeTaskCount,
            totalTaskCount,
            loginUsersCount,
            viewerAccountId,
            meTasks,
            meErrorTaskCount,
            meDeviceCount,
            otherGroups,
            archivedTasks,
            archivedOpen,
            ownerName,
            groupAddOpen,
            toggleGroupAdd,
            goToAccountTasks,
            meForm,
            sendingMeTestPush,
            saveMe,
            sendMeTestPush,
            script,
            bark,
            remoteBark,
            remoteBarkLogs,
            remoteRefreshMode,
            remoteAutoMinDraft,
            remoteAutoSecDraft,
            startScript,
            stopScript,
            startBarkServer,
            stopBarkServer,
            fetchRemoteBarkStatus,
            applyRemoteAutoInterval,
            saveEnv,
            selectUser,
            createUser,
            saveUser,
            savePortalSub,
            sendUserTestPush,
            openBackfill,
            createTask,
            trackerLogOutput,
            barkLogOutput,
            remoteBarkLogOutput,
        };
    }
});

// 推送与查询记录（任务时间线）：折叠展开时才拉取（每次展开都重新拉第一页，保证看到的是最新的），可继续加载更多。
// 映射查不到的事件类型会抛错，显示成加载失败，不悄悄显示原值。
app.component('task-timeline', {
    props: {
        taskId: { type: Number, required: true },
        number: { type: String, default: '' },
    },
    setup(props) {
        const events = ref([]);
        const loading = ref(false);
        const loaded = ref(false);
        const failed = ref('');
        const nextBefore = ref(null);

        const load = async (more = false) => {
            loading.value = true;
            failed.value = '';
            try {
                const { fetchTaskEvents, eventKindMeta } = await timelineReady;
                const result = await fetchTaskEvents(props.taskId, { before: more ? nextBefore.value : null });
                const rows = result.events.map((event) => ({ ...event, meta: eventKindMeta(event.kind) }));
                events.value = more ? events.value.concat(rows) : rows;
                nextBefore.value = result.has_more ? result.next_before : null;
                loaded.value = true;
            } catch (error) {
                failed.value = error.message || '加载时间线失败。';
            } finally {
                loading.value = false;
            }
        };

        const onToggle = (event) => {
            if (event.target.open) load(false);
        };

        const extraText = (event) =>
            [event.detail, event.tracking_number && event.tracking_number !== props.number ? `旧单号 ${event.tracking_number}` : '']
                .filter(Boolean)
                .join(' · ');

        return { events, loading, loaded, failed, nextBefore, load, onToggle, extraText };
    },
    template: `
        <details class="disclosure flush task-timeline" @toggle="onToggle">
            <summary>推送与查询记录</summary>
            <div class="disclosure-body">
            <p v-if="loading && !events.length" class="hint">加载中…</p>
            <div v-else-if="failed && !events.length" class="empty">[[ failed ]]</div>
            <div v-else-if="loaded && !events.length" class="empty">还没有记录。物流状态变化、推送结果和查询出错都会记在这里。</div>
            <ol v-if="events.length" class="timeline">
                <li v-for="event in events" :key="event.id" :class="['timeline-item', { err: event.kind === 'push_fail' }]">
                    <span :class="['dot', event.meta.dot]"></span>
                    <div class="timeline-main">
                        <div class="timeline-head">
                            <span :class="['pill', event.meta.tone]">[[ event.meta.label ]]</span>
                            <span class="timeline-title">[[ event.status_display || event.status || '—' ]]</span>
                            <time class="timeline-time">[[ event.created_at ]]</time>
                        </div>
                        <p v-if="extraText(event)" class="timeline-desc">[[ extraText(event) ]]</p>
                    </div>
                </li>
            </ol>
            <p v-if="failed && events.length" class="hint">[[ failed ]]</p>
            <button v-if="nextBefore" type="button" class="btn-ghost btn-small" :disabled="loading" @click="load(true)">加载更多</button>
            </div>
        </details>
    `,
});

// 到达预测：展开着就在挂载时拉一次，收着的（已归档卡片）第一次展开才拉；任务有更新（stamp 变了）再拉。
// 渲染交给 forecast.js（与门户页同一份 DOM 结构），映射查不到的阶段 / 可信度会抛错，显示成加载失败，不悄悄显示原值。
// 拉到后 emit loaded（带卡片摘要行），失败 emit failed。
app.component('task-forecast', {
    props: {
        taskId: { type: Number, required: true },
        stamp: { type: String, default: '' },
        open: { type: Boolean, default: true },
    },
    emits: ['loaded', 'failed'],
    setup(props, { emit }) {
        const body = ref(null);
        const wanted = ref(props.open);
        const load = async () => {
            if (!body.value || !wanted.value) return;
            const { renderForecast } = await forecastReady;
            await renderForecast(body.value, props.taskId, {
                // 「设为该渠道」成功后交给根组件用 user_state 刷新列表（stamp 变了会再拉一次预测）
                onUpdated: (result) => window.dispatchEvent(new CustomEvent('forecast-task-updated', { detail: result })),
                onLoaded: (result) => emit('loaded', result),
                onFailed: () => emit('failed'),
            });
        };
        const onToggle = (event) => {
            if (!event.target.open || wanted.value) return;
            wanted.value = true;
            load();
        };
        onMounted(load);
        watch(() => [props.taskId, props.stamp], load);
        return { body, onToggle };
    },
    template: `
        <details class="disclosure flush task-forecast" :open="open" @toggle="onToggle">
            <summary>到达预测</summary>
            <div class="disclosure-body" ref="body"></div>
        </details>
    `,
});

// 添加单号：单号 + 备注 + 渠道一行，查询间隔、发货前节点、启用开关和补录收在「更多」里。
// submit(accountId, form) 返回接口结果，成功后清空；backfill(accountId) 打开补录对话框。
app.component('task-add-form', {
    props: {
        accountId: { type: Number, required: true },
        submit: { type: Function, required: true },
        backfill: { type: Function, required: true },
    },
    setup(props) {
        const form = ref(buildNewTaskForm());
        const busy = ref(false);
        const ready = computed(() => Boolean(form.value.tracking_number.trim()) && !busy.value);
        const add = async () => {
            if (!ready.value) return;
            busy.value = true;
            try {
                const result = await props.submit(props.accountId, form.value);
                if (result?.status === 'success') form.value = buildNewTaskForm();
            } finally {
                busy.value = false;
            }
        };
        return { form, busy, ready, add, milestoneInputs: MILESTONE_INPUTS, milestoneCount };
    },
    template: `
        <div>
            <div class="field-row">
                <input type="text" class="field" v-model="form.tracking_number" placeholder="单号" aria-label="单号" @keydown.enter="add" />
                <input type="text" class="field" v-model="form.label" placeholder="备注（如：键盘）" aria-label="备注" @keydown.enter="add" />
                <input type="text" class="field" maxlength="50" :list="'channels-' + accountId" v-model="form.channel" placeholder="渠道" aria-label="渠道" @keydown.enter="add" />
                <button type="button" class="btn" :disabled="!ready" @click="add">添加</button>
            </div>
            <details class="disclosure flush u-mt-2">
                <summary>更多<span class="summary-meta">间隔 [[ form.check_interval ]]s · 节点 [[ milestoneCount(form) ]]/3</span></summary>
                <div class="disclosure-body">
                    <div class="task-fields">
                        <label class="task-field">
                            <span>查询间隔（秒）</span>
                            <input type="number" min="30" max="604800" class="field" v-model.number="form.check_interval" />
                        </label>
                        <label class="task-field" v-for="item in milestoneInputs" :key="item.stage">
                            <span>[[ item.label ]]</span>
                            <input type="text" class="field" inputmode="numeric" placeholder="YYYY-MM-DD HH:MM" v-model="form.milestones[item.stage]" />
                        </label>
                    </div>
                    <p class="hint">下单 / 打包 / 出库是发货前节点：可以只填日期，时间按日本时间，用于预测「从下单算第几天」。</p>
                    <label class="switch-row u-mt-3">
                        <input type="checkbox" class="switch" v-model="form.enabled" />
                        添加后立即启用
                    </label>
                    <p class="hint">再次添加同一个单号会复活它的归档记录，不会新建重复任务。</p>
                    <div class="actions">
                        <button type="button" class="btn-ghost btn-small" @click="backfill(accountId)">补录历史单号</button>
                        <span class="hint">补几单过去的单号和渠道，到达预测就有历史可参考。</span>
                    </div>
                </div>
            </details>
        </div>
    `,
});

// 任务卡片：首页的自己 / 其他账号 / 已归档共用这一份。
// 头部（备注为主标题、单号、渠道与状态胶囊）→ 摘要行（当前步骤 · 第 N 天 · 预计签收，与推送正文同一写法）
// → 到达预测（在途默认展开）→ 推送与查询记录（默认折叠）→ 就地编辑 → 操作。
// draft 是根组件的任务草稿（taskDrafts[task.id]），actions 是根组件的 taskActions；owner 非空时标出归属账号。
app.component('task-card', {
    props: {
        task: { type: Object, required: true },
        draft: { type: Object, default: null },
        state: { type: Object, required: true },
        owner: { type: String, default: '' },
        actions: { type: Object, required: true },
    },
    setup(props) {
        const editing = ref(false);
        // 摘要行：loading → ready（summary 有值）/ failed（预测区块里已经显示了失败原因，这里不再重复）
        const summary = ref('');
        const summaryState = ref('loading');
        const onLoaded = (result) => {
            summary.value = result.summary;
            summaryState.value = 'ready';
        };
        const onFailed = () => {
            summaryState.value = 'failed';
        };
        const save = async () => {
            const result = await props.actions.save(props.task);
            if (result?.status === 'success') editing.value = false;
        };
        return {
            editing, summary, summaryState, onLoaded, onFailed, save,
            milestoneInputs: MILESTONE_INPUTS, milestoneCount,
        };
    },
    template: `
        <article class="task-item inner" :id="'task-' + task.id">
            <div class="task-item-head">
                <div class="task-title">
                    <strong>[[ task.label || task.tracking_number ]]</strong>
                    <span v-if="task.label" class="task-meta num">[[ task.tracking_number ]]</span>
                </div>
                <span class="row wrap u-gap-2">
                    <span v-if="owner" class="pill violet">[[ owner ]]</span>
                    <span v-if="task.channel" class="pill cyan">[[ task.channel ]]</span>
                    <span :class="['pill', state.tone]">[[ state.label ]]</span>
                </span>
            </div>
            <p v-if="summaryState === 'ready'" class="callout info"><strong>[[ summary ]]</strong></p>
            <p v-else-if="summaryState === 'loading' && !task.archived" class="callout info">摘要加载中…</p>
            <template v-if="task.archived">
                <p class="task-meta">首次登记 [[ task.first_seen_at ]] · 登记次数 [[ task.seen_count ]]</p>
            </template>
            <template v-else>
                <p v-if="task.last_error" class="err-text task-meta">查询出错：[[ task.last_error ]]</p>
                <p class="task-meta">最近结果 [[ task.last_status_display || task.last_tracking_info || '暂无记录' ]]</p>
                <p class="task-meta">最近检查 [[ task.last_checked_at || '-' ]] · 最近推送 [[ task.last_push_at || '-' ]] · 间隔 [[ task.check_interval ]]s</p>
            </template>
            <task-forecast :task-id="task.id" :stamp="task.updated_at" :open="!task.archived" @loaded="onLoaded" @failed="onFailed"></task-forecast>
            <task-timeline :task-id="task.id" :number="task.tracking_number"></task-timeline>
            <div class="task-fields" v-if="editing && draft">
                <label class="task-field">
                    <span>日本邮政单号</span>
                    <input type="text" class="field" v-model="draft.tracking_number" />
                </label>
                <label class="task-field">
                    <span>备注名</span>
                    <input type="text" class="field" v-model="draft.label" />
                </label>
                <label class="task-field">
                    <span>查询间隔（秒）</span>
                    <input type="number" min="30" max="604800" class="field" v-model.number="draft.check_interval" />
                </label>
                <label class="task-field">
                    <span>渠道</span>
                    <input type="text" class="field" maxlength="50" :list="'channels-' + task.account_id" v-model="draft.channel" />
                </label>
                <label class="switch-row">
                    <input type="checkbox" class="switch" v-model="draft.atypical" />
                    非常规件（如大件电器）：作为预测参考时降低权重
                </label>
                <details class="disclosure flush" open>
                    <summary>发货前节点<span class="summary-meta">[[ milestoneCount(draft) ]]/3</span></summary>
                    <div class="disclosure-body">
                        <div class="task-fields">
                            <label class="task-field" v-for="item in milestoneInputs" :key="item.stage">
                                <span>[[ item.label ]]</span>
                                <input type="text" class="field" inputmode="numeric" placeholder="YYYY-MM-DD HH:MM" v-model="draft.milestones[item.stage]" />
                            </label>
                        </div>
                        <p class="hint">可以只填日期；时间按日本时间。留空保存即清除。</p>
                    </div>
                </details>
            </div>
            <div class="actions">
                <template v-if="task.archived">
                    <button type="button" class="btn btn-small" @click="actions.restore(task)">取消归档</button>
                    <button type="button" class="btn-ghost btn-small danger" @click="actions.remove(task)">删除</button>
                </template>
                <template v-else>
                    <button v-if="editing" type="button" class="btn btn-small" @click="save">保存</button>
                    <button type="button" class="btn-ghost btn-small" @click="editing = !editing">[[ editing ? '收起编辑' : '编辑' ]]</button>
                    <button type="button" class="btn-ghost btn-small" @click="actions.toggle(task)">[[ task.enabled ? '停用' : '启用' ]]</button>
                    <button type="button" class="btn-ghost btn-small" @click="actions.archive(task)">归档</button>
                    <button type="button" class="btn-ghost btn-small danger" @click="actions.remove(task)">删除</button>
                </template>
            </div>
        </article>
    `,
});

app.config.compilerOptions.delimiters = ['[[', ']]'];
app.mount('#app');
