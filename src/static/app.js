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
        const initialUserState = window.initialUserState || { users: [], task_count: 0, active_task_count: 0, login_count: 0 };

        const envDesc = {
            BARK_SERVER_INTERNAL: '追踪脚本访问的 Bark 地址',
            BARK_SERVER_PUBLIC: '手机 Bark 客户端访问的 HTTPS 地址',
            BARK_SERVER: '兼容旧版的单 Bark 地址',
            BARK_HEALTH_PATH: 'Bark 健康检测路径',
            BARK_HEALTH_TIMEOUT: 'Bark 健康检测超时(秒)',
            BARK_BIND_ADDRESS: '本地 Bark 监听地址',
            PUBLIC_URL: '控制台公网地址(保活用)',
            APP_PORT: 'Web 控制台端口(改后需重启 Web)',
            AUTO_START_BARK_SERVER: 'Web 启动时自动拉起本地 Bark(需重启 Web)',
            AUTO_START_TRACKER: 'Web 启动时自动运行追踪脚本,默认开(需重启 Web)',
            LOCAL_BARK_ENABLED: '是否由控制台管理本地 Bark 子进程,容器部署设 0(需重启 Web)'
        };

        const pickDefaultUserId = (list = []) =>
            list.find((user) => user.role !== 'admin')?.id || list[0]?.id || null;

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
        const selectedUserId = ref(pickDefaultUserId(initialUserState.users || []));
        const userForm = ref(
            buildUserForm((initialUserState.users || []).find((user) => user.id === selectedUserId.value) || {})
        );
        const newUserForm = ref(buildNewUserForm());
        const newTaskForm = ref(buildNewTaskForm());
        const taskDrafts = ref({});

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
        const envMessage = ref({ text: '', type: '' });
        const sendingTestPush = ref(false);

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

        const countBarkKeys = (value) =>
            String(value || '')
                .split(/\n+/)
                .map((item) => item.trim())
                .filter(Boolean).length;

        const activeTaskCount = computed(() => Number(taskTotals.value.active_task_count || 0));
        const totalTaskCount = computed(() => Number(taskTotals.value.task_count || 0));
        const loginUsersCount = computed(() => users.value.filter((user) => user.login_enabled).length);

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
        const selectedUserDeviceCount = computed(() => countBarkKeys(selectedUser.value?.bark_keys));

        const selectedUserTrackingState = computed(() => {
            if (!selectedUser.value) {
                return { tone: 'pending', label: '未选择账号' };
            }
            if (!selectedUserTaskCount.value) {
                return { tone: 'pending', label: '待添加任务' };
            }
            if (!selectedUserActiveTaskCount.value) {
                return { tone: 'pending', label: '任务全部停用' };
            }
            if (!script.value.running) {
                return { tone: 'pending', label: '待启动脚本' };
            }
            if (selectedUserErrorTaskCount.value) {
                return { tone: 'error', label: `${selectedUserErrorTaskCount.value} 个任务异常` };
            }
            return { tone: 'ok', label: `追踪中 ${selectedUserActiveTaskCount.value} 个` };
        });

        const selectedUserTaskSummary = computed(() => {
            if (!selectedUser.value) {
                return '请选择一个账号查看它的追踪任务。';
            }
            if (!selectedUserTaskCount.value) {
                return '这个账号还没有追踪任务，用下面的表单添加一个。';
            }
            if (!selectedUserActiveTaskCount.value) {
                return '任务都处于停用状态，启用后才会被轮询。';
            }
            if (!script.value.running) {
                return '任务已就绪，追踪脚本启动后开始轮询。';
            }
            if (selectedUserErrorTaskCount.value) {
                return '有任务最近一次轮询失败，看对应任务里的错误信息。';
            }
            return '任务正在正常轮询。';
        });

        const taskTone = (task) => {
            if (task.archived || !task.enabled) {
                return 'pending';
            }
            return task.last_error ? 'error' : 'ok';
        };

        const taskStateLabel = (task) => {
            if (task.archived) {
                return '已归档';
            }
            if (!task.enabled) {
                return '已停用';
            }
            if (task.last_error) {
                return '轮询异常';
            }
            return script.value.running ? '追踪中' : '待启动脚本';
        };

        // 任务操作后不重建账号表单，否则会把正在编辑的账号字段冲掉
        const syncUserState = (state, { keepUserForm = false } = {}) => {
            users.value = state?.users || [];
            taskTotals.value = {
                task_count: Number(state?.task_count || 0),
                active_task_count: Number(state?.active_task_count || 0),
            };
            rebuildTaskDrafts();
            if (!selectedUserId.value || !users.value.some((user) => user.id === selectedUserId.value)) {
                selectedUserId.value = pickDefaultUserId(users.value);
            }
            if (!keepUserForm) {
                userForm.value = buildUserForm(users.value.find((user) => user.id === selectedUserId.value) || {});
            }
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
                const changed = {};
                for (const [key, value] of Object.entries(envVars.value)) {
                    if (originalEnvVars.value[key] !== value) {
                        changed[key] = value;
                    }
                }
                const response = await fetch('/update_env', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(changed)
                });
                const result = await handleApiResponse(response);
                if (!result) return;
                flashMessage(envMessage, result.message, result.status === 'success' ? 'success' : 'error');
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

        // 任务类请求：成功后就地刷新账号列表，保留正在编辑的账号表单
        const sendTaskRequest = async (url, method, body, errorText) => {
            try {
                const response = await fetch(url, {
                    method,
                    headers: { 'Content-Type': 'application/json' },
                    body: body === undefined ? undefined : JSON.stringify(body)
                });
                const result = await handleApiResponse(response);
                if (!result) return;
                flashMessage(userMessage, result.message, result.status === 'success' ? 'success' : 'error');
                if (result.status === 'success' && result.user_state) {
                    syncUserState(result.user_state, { keepUserForm: true });
                }
                return result;
            } catch (error) {
                flashMessage(userMessage, errorText, 'error');
            }
        };

        const createTask = async () => {
            if (!selectedUserId.value || !newTaskForm.value.tracking_number.trim()) return;
            const result = await sendTaskRequest(
                `/api/users/${selectedUserId.value}/tasks`,
                'POST',
                { ...newTaskForm.value },
                '添加追踪任务时发生错误。'
            );
            if (result?.status === 'success') {
                newTaskForm.value = buildNewTaskForm();
            }
        };

        const saveTask = (task) => {
            const draft = taskDrafts.value[task.id];
            if (!draft) return;
            return sendTaskRequest(`/api/tasks/${task.id}`, 'PUT', { ...draft }, '保存追踪任务时发生错误。');
        };

        const toggleTask = (task) =>
            sendTaskRequest(`/api/tasks/${task.id}`, 'PUT', { enabled: !task.enabled }, '切换任务状态时发生错误。');

        const archiveTask = (task) =>
            sendTaskRequest(`/api/tasks/${task.id}/archive`, 'POST', undefined, '归档追踪任务时发生错误。');

        // 归档任务重新添加同单号 = storage 侧复活，保留首次登记时间与次数
        const restoreTask = (task) => {
            if (!selectedUserId.value) return;
            return sendTaskRequest(
                `/api/users/${selectedUserId.value}/tasks`,
                'POST',
                {
                    tracking_number: task.tracking_number,
                    label: task.label || '',
                    check_interval: Number(task.check_interval || 300),
                    enabled: true,
                },
                '恢复追踪任务时发生错误。'
            );
        };

        const deleteTask = (task) => {
            if (!window.confirm(`确认删除单号 ${task.tracking_number} 的追踪任务？删除后档案也会一起消失。`)) return;
            return sendTaskRequest(`/api/tasks/${task.id}`, 'DELETE', undefined, '删除追踪任务时发生错误。');
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
            envDesc,
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
            selectedUserTaskSummary,
            selectedUserDeviceCount,
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
