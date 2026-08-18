const { createApp, ref, onMounted, nextTick, computed, watch } = Vue;

const MAX_LOG_LINES = 500;

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

        const resetEnv = () => {
            envVars.value = { ...originalEnvVars.value };
            envMessage.value = { text: '', type: '' };
            envApplyNote.value = '';
        };

        // 账号表单只管身份与 Bark；单号/间隔属于任务，走 taskDrafts
        const buildUserForm = (user = {}) => ({
            id: user.id || null,
            username: user.username || '',
            display_name: user.display_name || user.name || '',
            role: user.role || 'user',
            note: user.note || '',
            bark_keys: user.bark_keys || user.bark_key || '',
            bark_query_params: user.bark_query_params || '?sound=minuet&level=timeSensitive',
            bark_url_enabled: Boolean(user.bark_url_enabled),
            login_enabled: user.login_enabled !== false,
            new_password: '',
            task_id: '',
            test_title: '测试推送',
            test_body: '这是 Bark 参数预览消息，用来确认通知样式、声音和链接效果。',
        });

        const buildNewUserForm = () => ({
            username: '',
            display_name: '',
            password: '',
            role: 'user',
            login_enabled: true,
        });

        const buildNewTaskForm = () => ({
            tracking_number: '',
            label: '',
            check_interval: 300,
            enabled: true,
        });

        // 每条任务一份可编辑草稿；启用开关不进草稿，避免"改了单号顺手被一起保存"
        const buildTaskDraft = (task = {}) => ({
            tracking_number: task.tracking_number || '',
            label: task.label || '',
            check_interval: Number(task.check_interval || 300),
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
        const userMessage = ref({ text: '', type: '' });
        const meMessage = ref({ text: '', type: '' });
        const envMessage = ref({ text: '', type: '' });
        const sendingTestPush = ref(false);
        const sendingMeTestPush = ref(false);

        // 行内反馈：成功 3 秒、错误 5 秒后自动消失（DESIGN.md §9）
        const messageTimers = new WeakMap();
        const flashMessage = (target, text, type) => {
            target.value = { text, type };
            clearTimeout(messageTimers.get(target));
            messageTimers.set(
                target,
                setTimeout(() => {
                    target.value = { text: '', type: '' };
                }, type === 'error' ? 5000 : 3000)
            );
        };

        // 用户管理页与「我的设置」页各有一条反馈行，任务操作的提示要落回发起它的那一页
        const messageScopes = { user: userMessage, me: meMessage };
        const scopedMessage = (scope) => messageScopes[scope] || userMessage;

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
        const keepalive = ref({
            running: false,
            configured: false,
            url: '',
            state: 'idle',
            last_code: null,
            last_error: null,
            last_at: ''
        });
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
        const selectedUserErrorTaskCount = computed(
            () => selectedUserTasks.value.filter((task) => task.last_error).length
        );

        const selectedUserTrackingState = computed(() => {
            if (!selectedUser.value) {
                return { tone: 'pending', label: '未选择账号' };
            }
            if (!selectedUserTaskCount.value) {
                return { tone: 'pending', label: '待添加任务' };
            }
            // 判定顺序与 taskState 保持一致：异常最优先，脚本没跑只是"还没开始"
            if (selectedUserErrorTaskCount.value) {
                return { tone: 'error', label: `${selectedUserErrorTaskCount.value} 个任务异常` };
            }
            if (!selectedUserActiveTaskCount.value) {
                return { tone: 'pending', label: '任务全部停用' };
            }
            if (!script.value.running) {
                return { tone: 'warn', label: '追踪脚本未运行' };
            }
            return { tone: 'ok', label: `追踪中 ${selectedUserActiveTaskCount.value} 个` };
        });

        // 任务状态的唯一判据：weight 兼作首页排序权重，越小越该被先看到
        const taskState = (task) => {
            if (task.archived) {
                return { key: 'archived', tone: 'pending', label: '已归档', weight: 5 };
            }
            // 停用优先于异常：用户主动停掉的任务不该被当成"出问题了"催着处理，
            // 它留着的 last_error 只是停用前最后一次轮询的残留
            if (!task.enabled) {
                return { key: 'paused', tone: 'pending', label: '已停用', weight: 4 };
            }
            if (task.last_error) {
                return { key: 'error', tone: 'error', label: '轮询异常', weight: 0 };
            }
            if (!script.value.running) {
                return { key: 'idle', tone: 'warn', label: '待启动脚本', weight: 1 };
            }
            if (!task.last_checked_at) {
                return { key: 'fresh', tone: 'warn', label: '尚未检查', weight: 2 };
            }
            return { key: 'ok', tone: 'ok', label: '追踪中', weight: 3 };
        };

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

        const globalTrackingState = computed(() => {
            if (!totalTaskCount.value) {
                return { tone: 'pending', label: '还没有任务' };
            }
            if (globalErrorTaskCount.value) {
                return { tone: 'error', label: `${globalErrorTaskCount.value} 个任务异常` };
            }
            if (!activeTaskCount.value) {
                return { tone: 'pending', label: '任务全部停用' };
            }
            if (!script.value.running) {
                return { tone: 'warn', label: '追踪脚本未运行' };
            }
            return { tone: 'ok', label: `正常追踪 ${activeTaskCount.value} 个` };
        });

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
        const meErrorTaskCount = computed(() => meTasks.value.filter((task) => task.last_error).length);
        // 设备数跟着输入框实时变，比保存后的账号字段更贴合"我现在填了几个"
        const meDeviceCount = computed(() => countBarkKeys(meForm.value.bark_keys));

        const meTrackingState = computed(() => {
            if (!meTaskCount.value) {
                return { tone: 'pending', label: '待添加任务' };
            }
            if (meErrorTaskCount.value) {
                return { tone: 'error', label: `${meErrorTaskCount.value} 个任务异常` };
            }
            if (!meActiveTaskCount.value) {
                return { tone: 'pending', label: '任务全部停用' };
            }
            if (!script.value.running) {
                return { tone: 'warn', label: '追踪脚本未运行' };
            }
            return { tone: 'ok', label: `追踪中 ${meActiveTaskCount.value} 个` };
        });

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
                userMessage.value = { text: '', type: '' };
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
                flashMessage(envMessage, result.message, result.status === 'success' ? 'success' : 'error');
                envApplyNote.value = result.apply_note || '';
                if (result.status === 'success' && result.env_vars) {
                    envVars.value = { ...result.env_vars };
                    originalEnvVars.value = { ...result.env_vars };
                }
                if (result.status === 'success' && result.bark_help) {
                    barkHelp.value = { ...result.bark_help };
                }
            } catch (error) {
                flashMessage(envMessage, '保存系统设置时发生错误。', 'error');
            }
        };

        const selectUser = (userId) => {
            selectedUserId.value = userId;
            userMessage.value = { text: '', type: '' };
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
                flashMessage(userMessage, result.message, result.status === 'success' ? 'success' : 'error');
                if (result.status === 'success') {
                    syncUserState(result.user_state);
                    if (result.user) {
                        selectedUserId.value = result.user.id;
                    }
                    newUserForm.value = buildNewUserForm();
                    createUserExpanded.value = false;
                }
            } catch (error) {
                flashMessage(userMessage, '创建账号时发生错误。', 'error');
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
                flashMessage(userMessage, result.message, result.status === 'success' ? 'success' : 'error');
                if (result.status === 'success') {
                    syncUserState(result.user_state);
                    if (result.user) {
                        userForm.value = buildUserForm(result.user);
                    }
                }
            } catch (error) {
                flashMessage(userMessage, '保存账号时发生错误。', 'error');
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
                flashMessage(userMessage, result.message, result.status === 'success' ? 'success' : 'error');
            } catch (error) {
                flashMessage(userMessage, '发送测试推送失败。', 'error');
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
                flashMessage(meMessage, result.message, result.status === 'success' ? 'success' : 'error');
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
                flashMessage(meMessage, '保存个人设置时发生错误。', 'error');
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
                flashMessage(meMessage, result.message, result.status === 'success' ? 'success' : 'error');
            } catch (error) {
                flashMessage(meMessage, '发送测试推送失败。', 'error');
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
                flashMessage(scopedMessage(scope), result.message, result.status === 'success' ? 'success' : 'error');
                if (result.status === 'success' && result.user_state) {
                    syncUserState(result.user_state, { keepUserForm: true });
                }
                return result;
            } catch (error) {
                flashMessage(scopedMessage(scope), errorText, 'error');
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

        const deleteTask = (task, scope = 'user') => {
            if (!window.confirm(`确认删除单号 ${task.tracking_number} 的追踪任务？删除后档案也会一起消失。`)) return;
            return sendTaskRequest(`/api/tasks/${task.id}`, 'DELETE', undefined, '删除追踪任务时发生错误。', scope);
        };

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
            socket.value.on('keepalive_status', (data) => {
                keepalive.value = { ...keepalive.value, ...data };
            });
            socket.value.on('bark_server_status', (data) => {
                bark.value.running = data.running;
            });
            socket.value.on('tracker_log', async (data) => {
                script.value.logs.push(data.data.replace(/\n/g, '<br>'));
                if (script.value.logs.length > MAX_LOG_LINES) {
                    script.value.logs.splice(0, script.value.logs.length - MAX_LOG_LINES);
                }
                await nextTick();
                scrollToBottom(trackerLogOutput.value);
            });
            socket.value.on('bark_log', async (data) => {
                bark.value.logs.push(data.data.replace(/\n/g, '<br>'));
                if (bark.value.logs.length > MAX_LOG_LINES) {
                    bark.value.logs.splice(0, bark.value.logs.length - MAX_LOG_LINES);
                }
                await nextTick();
                scrollToBottom(barkLogOutput.value);
            });
            socket.value.on('full_tracker_log', async (data) => {
                const lines = data.data ? data.data.split('\n').map((line) => line.replace(/\n/g, '<br>')) : [];
                script.value.logs = lines.length > MAX_LOG_LINES ? lines.slice(-MAX_LOG_LINES) : lines;
                await nextTick();
                scrollToBottom(trackerLogOutput.value);
            });
            socket.value.on('full_bark_log', async (data) => {
                const lines = data.data ? data.data.split('\n').map((line) => line.replace(/\n/g, '<br>')) : [];
                bark.value.logs = lines.length > MAX_LOG_LINES ? lines.slice(-MAX_LOG_LINES) : lines;
                await nextTick();
                scrollToBottom(barkLogOutput.value);
            });
            socket.value.on('remote_bark_log', async (data) => {
                remoteBarkLogs.value.push(data.data.replace(/\n/g, '<br>'));
                if (remoteBarkLogs.value.length > MAX_LOG_LINES) {
                    remoteBarkLogs.value.splice(0, remoteBarkLogs.value.length - MAX_LOG_LINES);
                }
                await nextTick();
                scrollToBottom(remoteBarkLogOutput.value);
            });
            socket.value.on('full_remote_bark_log', async (data) => {
                const lines = data.data ? data.data.split('\n').map((line) => line.replace(/\n/g, '<br>')) : [];
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
            envGroups,
            envFieldLocked,
            envBoolValue,
            setEnvBool,
            envDirtyCount,
            envApplyNote,
            resetEnv,
            envMessage,
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
            taskStateLabel,
            taskDrafts,
            newTaskForm,
            userForm,
            newUserForm,
            createUserExpanded,
            userMessage,
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
            meMessage,
            sendingMeTestPush,
            saveMe,
            sendMeTestPush,
            script,
            keepalive,
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
            sendUserTestPush,
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

app.config.compilerOptions.delimiters = ['[[', ']]'];
app.mount('#app');
