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
        const initialUserState = window.initialUserState || { users: [], tracked_count: 0, login_count: 0 };

        const envDesc = {
            BARK_SERVER_INTERNAL: '追踪脚本访问的 Bark 地址',
            BARK_SERVER_PUBLIC: '手机 Bark 客户端访问的 HTTPS 地址',
            BARK_SERVER: '兼容旧版的单 Bark 地址',
            BARK_HEALTH_PATH: 'Bark 健康检测路径',
            BARK_HEALTH_TIMEOUT: 'Bark 健康检测超时(秒)',
            BARK_BIND_ADDRESS: '本地 Bark 监听地址',
            PUBLIC_URL: '控制台公网地址(保活用)',
            APP_PORT: 'Web 控制台端口',
            AUTO_START_BARK_SERVER: 'Web 启动时自动拉起本地 Bark'
        };

        const pickDefaultUserId = (list = []) =>
            list.find((user) => user.role !== 'admin')?.id || list[0]?.id || null;

        const buildUserForm = (user = {}) => ({
            id: user.id || null,
            username: user.username || '',
            display_name: user.display_name || user.name || '',
            role: user.role || 'user',
            note: user.note || '',
            tracking_number: user.tracking_number || '',
            check_interval: Number(user.check_interval || 300),
            bark_keys: user.bark_keys || user.bark_key || '',
            bark_query_params: user.bark_query_params || '?sound=minuet&level=timeSensitive',
            bark_url_enabled: Boolean(user.bark_url_enabled),
            login_enabled: user.login_enabled !== false,
            tracking_enabled: user.tracking_enabled !== false,
            new_password: '',
            test_title: '测试推送',
            test_body: '这是 Bark 参数预览消息，用来确认通知样式、声音和链接效果。',
        });

        const users = ref(initialUserState.users || []);
        const selectedUserId = ref(pickDefaultUserId(initialUserState.users || []));
        const userForm = ref(
            buildUserForm((initialUserState.users || []).find((user) => user.id === selectedUserId.value) || {})
        );
        const newUserForm = ref({
            username: '',
            display_name: '',
            password: '',
            role: 'user',
            login_enabled: true,
            tracking_enabled: true,
        });
        const createUserExpanded = ref(false);
        const userMessage = ref({ text: '', type: '' });
        const envMessage = ref({ text: '', type: '' });
        const sendingTestPush = ref(false);

        const countBarkKeys = (value) =>
            String(value || '')
                .split(/\n+/)
                .map((item) => item.trim())
                .filter(Boolean).length;

        const trackedUsersCount = computed(() => users.value.filter((user) => user.tracking_enabled).length);
        const loginUsersCount = computed(() => users.value.filter((user) => user.login_enabled).length);
        const trackedNumbersCount = computed(() =>
            users.value.reduce((total, user) => total + Number(user.tracking_history_count || 0), 0)
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
        const selectedUserTrackingState = computed(() => {
            const user = selectedUser.value;
            if (!user) {
                return { tone: 'pending', label: '未选择账号' };
            }
            if (user.role === 'admin' && !user.tracking_enabled && !user.tracking_number) {
                return { tone: 'pending', label: '管理账号' };
            }
            if (!user.tracking_number) {
                return { tone: 'pending', label: '待填写单号' };
            }
            if (!user.tracking_enabled) {
                return { tone: 'pending', label: '追踪已关闭' };
            }
            if (!script.value.running) {
                return { tone: 'pending', label: '待启动脚本' };
            }
            if (user.last_error) {
                return { tone: 'error', label: '最近轮询异常' };
            }
            return { tone: 'ok', label: '追踪中' };
        });
        const selectedUserDeviceCount = computed(() => countBarkKeys(selectedUser.value?.bark_keys));
        const selectedUserHistoryCount = computed(() => Number(selectedUser.value?.tracking_history_count || 0));
        const selectedUserHistory = computed(() => selectedUser.value?.tracking_history_preview || []);
        const selectedUserLatestStatus = computed(() => {
            const user = selectedUser.value;
            if (!user) {
                return '请选择一个账号开始编辑追踪信息。';
            }
            if (user.last_error) {
                return user.last_error;
            }
            if (user.last_tracking_info) {
                return user.last_tracking_info;
            }
            if (!user.tracking_number) {
                return '还没有填写日本邮政单号。';
            }
            if (!user.tracking_enabled) {
                return '单号已保存，但当前账号没有启用追踪。';
            }
            if (!script.value.running) {
                return '单号已保存，等你手动启动追踪脚本后就会开始轮询。';
            }
            return '单号已保存，等待下一次轮询结果。';
        });

        const syncUserState = (state) => {
            users.value = state?.users || [];
            if (!selectedUserId.value || !users.value.some((user) => user.id === selectedUserId.value)) {
                selectedUserId.value = pickDefaultUserId(users.value);
            }
            userForm.value = buildUserForm(users.value.find((user) => user.id === selectedUserId.value) || {});
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
                envMessage.value = {
                    text: result.message,
                    type: result.status === 'success' ? 'success' : 'error'
                };
                if (result.status === 'success' && result.env_vars) {
                    envVars.value = { ...result.env_vars };
                    originalEnvVars.value = { ...result.env_vars };
                }
                if (result.status === 'success' && result.bark_help) {
                    barkHelp.value = { ...result.bark_help };
                }
            } catch (error) {
                envMessage.value = { text: '保存系统设置时发生错误。', type: 'error' };
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
                userMessage.value = {
                    text: result.message,
                    type: result.status === 'success' ? 'success' : 'error'
                };
                if (result.status === 'success') {
                    syncUserState(result.user_state);
                    if (result.user) {
                        selectedUserId.value = result.user.id;
                    }
                    newUserForm.value = {
                        username: '',
                        display_name: '',
                        password: '',
                        role: 'user',
                        login_enabled: true,
                        tracking_enabled: true,
                    };
                    createUserExpanded.value = false;
                }
            } catch (error) {
                userMessage.value = { text: '创建账号时发生错误。', type: 'error' };
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
                userMessage.value = {
                    text: result.message,
                    type: result.status === 'success' ? 'success' : 'error'
                };
                if (result.status === 'success') {
                    syncUserState(result.user_state);
                    if (result.user) {
                        userForm.value = buildUserForm(result.user);
                    }
                }
            } catch (error) {
                userMessage.value = { text: '保存账号时发生错误。', type: 'error' };
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
                userMessage.value = {
                    text: result.message,
                    type: result.status === 'success' ? 'success' : 'error'
                };
            } catch (error) {
                userMessage.value = { text: '发送测试推送失败。', type: 'error' };
            } finally {
                sendingTestPush.value = false;
            }
        };

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
            selectedUserTrackingState,
            selectedUserDeviceCount,
            selectedUserHistoryCount,
            selectedUserHistory,
            selectedUserLatestStatus,
            userForm,
            newUserForm,
            createUserExpanded,
            userMessage,
            sendingTestPush,
            trackedUsersCount,
            loginUsersCount,
            trackedNumbersCount,
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
            trackerLogOutput,
            barkLogOutput,
            remoteBarkLogOutput,
        };
    }
});

app.config.compilerOptions.delimiters = ['[[', ']]'];
app.mount('#app');
