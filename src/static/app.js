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

        // 17TRACK 卡片：切到系统设置页、或改了密钥后拉一次（额度由服务端缓存 5 分钟）
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

        // 发货前节点的三个输入框（key 与后端 forecast.PRE_STAGES 一致，写错了后端会 400）
        const milestoneInputs = [
            { stage: 'ordered', label: '下单' },
            { stage: 'packed', label: '打包' },
            { stage: 'handed_over', label: '出库' },
        ];
        const buildMilestones = (milestones = {}) =>
            Object.fromEntries(milestoneInputs.map(({ stage }) => [stage, milestones[stage]?.at || '']));
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

        const users = ref(initialUserState.users || []);
        const taskTotals = ref({
            task_count: Number(initialUserState.task_count || 0),
            active_task_count: Number(initialUserState.active_task_count || 0),
        });
        // 首页是只读仪表盘，不再默认选中账号——旧版默认选第一个非 admin 用户，
        // 结果管理员一进来看到的"当前账号"是别人，表单改的也是别人的数据
        const selectedUserId = ref(null);
        const userForm = ref(buildUserForm({}));
        const newUserForm = ref(buildNewUserForm());
        const newTaskForm = ref(buildNewTaskForm());
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
        const myTaskForm = ref(buildNewTaskForm());

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
        const globalBarkDeviceCount = computed(() =>
            users.value.reduce((sum, user) => sum + Number(user.bark_device_count || 0), 0)
        );

        const script = ref({ running: false, logs: [] });
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
        const selectedUserTasks = computed(() => selectedUser.value?.tasks || []);
        const selectedUserArchivedTasks = computed(() => selectedUser.value?.archived_tasks || []);
        const selectedUserTaskCount = computed(() => Number(selectedUser.value?.task_count || 0));
        const selectedUserActiveTaskCount = computed(() => Number(selectedUser.value?.active_task_count || 0));
        const selectedUserErrorTaskCount = computed(() => countErrorTasks(selectedUserTasks.value));

        // 三处"追踪状态"徽章（首页 / 选中账号 / 我的设置）共用的判定，顺序与 taskState 一致：
        // 异常最优先，脚本没跑只是"还没开始"。各处只提供自己的数量与文案。
        const summarizeTracking = ({ total, active, errors, emptyLabel, okLabel }) => {
            if (!total) {
                return { tone: '', label: emptyLabel };
            }
            if (errors) {
                return { tone: 'err', label: `${errors} 个任务异常` };
            }
            if (!active) {
                return { tone: '', label: '任务全部停用' };
            }
            if (!script.value.running) {
                return { tone: 'warn', label: '追踪脚本未运行' };
            }
            return { tone: 'ok', label: `${okLabel} ${active} 个` };
        };
        const countErrorTasks = (tasks) => tasks.filter((task) => taskState(task).key === 'error').length;

        const selectedUserTrackingState = computed(() =>
            selectedUser.value
                ? summarizeTracking({
                      total: selectedUserTaskCount.value,
                      active: selectedUserActiveTaskCount.value,
                      errors: selectedUserErrorTaskCount.value,
                      emptyLabel: '待添加任务',
                      okLabel: '追踪中',
                  })
                : { tone: '', label: '未选择账号' }
        );

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

        // 首页总览行的状态色条（包的 tr.ok/.warn/.err/.idle），与 taskState 的 key 一一对应；未列出的 key 取不到值，会在渲染时暴露
        const OVERVIEW_ROW_TONE = { error: 'err', idle: 'warn', fresh: 'warn', ok: 'ok', archived: 'idle', paused: 'idle' };
        const overviewRowTone = (task) => OVERVIEW_ROW_TONE[taskState(task).key];

        const taskTone = (task) => taskState(task).tone;
        const taskStateLabel = (task) => taskState(task).label;

        // ---------- 首页仪表盘：跨账号的全局任务流 ----------

        // 拉平成一维：首页要回答的是"现在所有包裹什么状态"，不是"每个账号有什么"
        const globalTasks = computed(() =>
            users.value.flatMap((user) =>
                (user.tasks || []).concat(user.archived_tasks || []).map((task) => ({
                    ...task,
                    account_display_name: user.display_name,
                    account_username: user.username,
                }))
            )
        );

        const globalErrorTaskCount = computed(
            () => globalTasks.value.filter((task) => taskState(task).key === 'error').length
        );

        const globalTrackingState = computed(() =>
            summarizeTracking({
                total: totalTaskCount.value,
                active: activeTaskCount.value,
                errors: globalErrorTaskCount.value,
                emptyLabel: '还没有任务',
                okLabel: '正常追踪',
            })
        );

        const overviewFilter = ref('all');

        const matchesOverviewFilter = (task, key) => {
            const stateKey = taskState(task).key;
            if (key === 'archived') return stateKey === 'archived';
            if (key === 'alert') return stateKey === 'error';
            if (key === 'paused') return stateKey === 'paused';
            if (key === 'tracking') return ['ok', 'idle', 'fresh'].includes(stateKey);
            return stateKey !== 'archived';
        };

        const overviewFilters = computed(() =>
            [
                { key: 'all', label: '全部' },
                { key: 'alert', label: '异常' },
                // 叫"正常"而不是"启用中"：异常任务其实也在启用状态、也在轮询，
                // 若这一项叫"启用中"，全部报错时会显示"启用中 0"，看着像没任务在跑
                { key: 'tracking', label: '正常' },
                { key: 'paused', label: '已停用' },
                { key: 'archived', label: '已归档' },
            ].map((item) => ({
                ...item,
                count: globalTasks.value.filter((task) => matchesOverviewFilter(task, item.key)).length,
            }))
        );

        // 异常排最前；同状态内按最近检查时间倒序，久没动静的沉到后面
        const overviewTasks = computed(() =>
            globalTasks.value
                .filter((task) => matchesOverviewFilter(task, overviewFilter.value))
                .sort((left, right) => {
                    const byState = taskState(left).weight - taskState(right).weight;
                    if (byState !== 0) return byState;
                    return String(right.last_checked_at || '').localeCompare(String(left.last_checked_at || ''));
                })
        );

        // ---------- 我的设置：管理员自己那份 ----------

        const meAccount = computed(
            () => users.value.find((user) => user.id === viewerAccountId.value) || viewer.value?.account || null
        );
        const meTasks = computed(() => meAccount.value?.tasks || []);
        const meArchivedTasks = computed(() => meAccount.value?.archived_tasks || []);
        const meTaskCount = computed(() => Number(meAccount.value?.task_count || 0));
        const meActiveTaskCount = computed(() => Number(meAccount.value?.active_task_count || 0));
        const meErrorTaskCount = computed(() => countErrorTasks(meTasks.value));
        // 设备数跟着输入框实时变，比保存后的账号字段更贴合"我现在填了几个"
        const meDeviceCount = computed(() => countBarkKeys(meForm.value.bark_keys));

        const meTrackingState = computed(() =>
            summarizeTracking({
                total: meTaskCount.value,
                active: meActiveTaskCount.value,
                errors: meErrorTaskCount.value,
                emptyLabel: '待添加任务',
                okLabel: '追踪中',
            })
        );

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

        // 首页只读，编辑一律去编辑页：自己的任务进「我的设置」，别人的进「用户管理」并选中该账号
        const openTaskEditor = (task) => {
            if (viewerAccountId.value && Number(task.account_id) === viewerAccountId.value) {
                page.value = 'me';
            } else {
                selectedUserId.value = task.account_id;
                page.value = 'users';
            }
            window.scrollTo({ top: 0, behavior: 'smooth' });
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
                // 用户管理页可能正编辑到一半，只有它选中的正是自己时才有必要重建那份表单
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

        // 任务类请求：成功后就地刷新账号列表，保留正在编辑的账号表单
        const sendTaskRequest = async (url, method, body, errorText, scope = 'user') => {
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

        // scope 决定任务落在谁名下、提示显示在哪一页：'me' = 管理员自己，'user' = 用户管理里选中的账号
        const taskOwnerId = (scope) => (scope === 'me' ? viewerAccountId.value : selectedUserId.value);

        const createTask = async (scope = 'user') => {
            const accountId = taskOwnerId(scope);
            const form = scope === 'me' ? myTaskForm : newTaskForm;
            if (!accountId || !form.value.tracking_number.trim()) return;
            const result = await sendTaskRequest(
                `/api/users/${accountId}/tasks`,
                'POST',
                { ...form.value },
                '添加追踪任务时发生错误。',
                scope
            );
            if (result?.status === 'success') {
                form.value = buildNewTaskForm();
            }
        };

        const saveTask = (task, scope = 'user') => {
            const draft = taskDrafts.value[task.id];
            if (!draft) return;
            return sendTaskRequest(`/api/tasks/${task.id}`, 'PUT', { ...draft }, '保存追踪任务时发生错误。', scope);
        };

        const toggleTask = (task, scope = 'user') =>
            sendTaskRequest(`/api/tasks/${task.id}`, 'PUT', { enabled: !task.enabled }, '切换任务状态时发生错误。', scope);

        const archiveTask = (task, scope = 'user') =>
            sendTaskRequest(`/api/tasks/${task.id}/archive`, 'POST', undefined, '归档追踪任务时发生错误。', scope);

        // 归档任务重新添加同单号 = storage 侧复活，保留首次登记时间与次数
        const restoreTask = (task, scope = 'user') => {
            const accountId = taskOwnerId(scope);
            if (!accountId) return;
            return sendTaskRequest(
                `/api/users/${accountId}/tasks`,
                'POST',
                {
                    tracking_number: task.tracking_number,
                    label: task.label || '',
                    check_interval: Number(task.check_interval || 300),
                    enabled: true,
                },
                '恢复追踪任务时发生错误。',
                scope
            );
        };

        const deleteTask = async (task, scope = 'user') => {
            const { confirm } = await confirmReady;
            const confirmed = await confirm({
                title: '删除追踪任务',
                message: `确认删除单号 ${task.tracking_number} 的追踪任务？删除后档案也会一起消失。`,
                confirmLabel: '删除',
                danger: true,
            });
            if (!confirmed) return;
            return sendTaskRequest(`/api/tasks/${task.id}`, 'DELETE', undefined, '删除追踪任务时发生错误。', scope);
        };

        // 补录历史单号：'me' 补到管理员自己名下，'user' 补到用户管理里选中的账号
        const openBackfill = async (scope = 'user') => {
            const accountId = taskOwnerId(scope);
            if (!accountId) return;
            const { openBackfillDialog } = await forecastReady;
            await openBackfillDialog({
                accountId,
                onDone: (result) => {
                    if (result.user_state) syncUserState(result.user_state, { keepUserForm: true });
                },
            });
        };

        // 切到系统设置页时刷新 17TRACK 卡片（额度在服务端缓存，频繁切换也不会打到 17TRACK）
        watch(page, (next) => {
            if (next === 'env') loadTrack17Card();
        });

        window.addEventListener('forecast-task-updated', (event) => {
            const result = event.detail || {};
            if (result.user_state) syncUserState(result.user_state, { keepUserForm: true });
        });

        watch(selectedUserId, (nextUserId) => {
            userForm.value = buildUserForm(users.value.find((user) => user.id === nextUserId) || {});
            newTaskForm.value = buildNewTaskForm();
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
            selectedUserTasks,
            selectedUserArchivedTasks,
            selectedUserTaskCount,
            selectedUserActiveTaskCount,
            selectedUserErrorTaskCount,
            selectedUserTrackingState,
            taskState,
            taskTone,
            overviewRowTone,
            taskStateLabel,
            taskDrafts,
            newTaskForm,
            userForm,
            newUserForm,
            createUserExpanded,
            sendingTestPush,
            activeTaskCount,
            totalTaskCount,
            loginUsersCount,
            globalBarkDeviceCount,
            globalErrorTaskCount,
            globalTrackingState,
            overviewFilter,
            overviewFilters,
            overviewTasks,
            openTaskEditor,
            viewerAccountId,
            meAccount,
            meTasks,
            meArchivedTasks,
            meTaskCount,
            meActiveTaskCount,
            meErrorTaskCount,
            meDeviceCount,
            meTrackingState,
            meForm,
            myTaskForm,
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
            milestoneInputs,
            milestoneCount,
            openBackfill,
            createTask,
            saveTask,
            toggleTask,
            archiveTask,
            restoreTask,
            deleteTask,
            trackerLogOutput,
            barkLogOutput,
            remoteBarkLogOutput,
        };
    }
});

// 任务时间线：折叠展开时才拉取（每次展开都重新拉第一页，保证看到的是最新的），可继续加载更多。
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
            <summary>时间线</summary>
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

// 到达预测：挂载时拉一次，任务有更新（stamp 变了）再拉；渲染交给 forecast.js（与门户页同一份 DOM 结构），
// 映射查不到的阶段 / 可信度会抛错，显示成加载失败，不悄悄显示原值。
app.component('task-forecast', {
    props: {
        taskId: { type: Number, required: true },
        stamp: { type: String, default: '' },
    },
    setup(props) {
        const body = ref(null);
        const load = async () => {
            if (!body.value) return;
            const { renderForecast } = await forecastReady;
            // 「设为该渠道」成功后交给根组件用 user_state 刷新列表（stamp 变了会再拉一次预测）
            await renderForecast(body.value, props.taskId, {
                onUpdated: (result) => window.dispatchEvent(new CustomEvent('forecast-task-updated', { detail: result })),
            });
        };
        onMounted(load);
        watch(() => [props.taskId, props.stamp], load);
        return { body };
    },
    template: `
        <details class="disclosure flush task-forecast" open>
            <summary>到达预测</summary>
            <div class="disclosure-body" ref="body"></div>
        </details>
    `,
});

app.config.compilerOptions.delimiters = ['[[', ']]'];
app.mount('#app');
