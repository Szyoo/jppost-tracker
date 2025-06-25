const { createApp, ref, onMounted, nextTick } = Vue;

// 限制前端保存的日志条数，避免大量日志导致页面阻塞
const MAX_LOG_LINES = 500;

const app = createApp({
    setup() {
        // --- 响应式状态定义 ---
        const title = ref('快递追踪器与通知服务控制台 (Vue.js)');
        const socket = ref(null);

        const envVars = ref(window.initialEnvVars || {});
        // 将 BARK_QUERY_PARAMS 解析为对象便于编辑
        const parseQuery = (str) => {
            const q = {};
            if (!str) return q;
            str.replace(/^\?/, '').split('&').forEach(part => {
                if (!part) return;
                const [k, v = ''] = part.split('=');
                q[decodeURIComponent(k)] = decodeURIComponent(v);
            });
            return q;
        };
        const buildQuery = (obj) => {
            const parts = Object.keys(obj).map(k => `${encodeURIComponent(k)}=${encodeURIComponent(obj[k])}`);
            return parts.length ? '?' + parts.join('&') : '';
        };
        const barkParams = ref(parseQuery(envVars.value.BARK_QUERY_PARAMS));
        const newParam = ref('');
        const paramOptions = {
            sound: { description: '通知铃声', values: ['alarm','anticipate','bell','birdsong','bloom','calypso','chime','choo','descent','electronic','fanfare','glass','gotosleep','healthnotification','horn','ladder','mailsent','minuet','multiwayinvitation','newmail','newsflash','noir','paymentsuccess','shake','sherwoodforest','silence','spell','suspense','telegraph','tiptoes','typewriters','update'] },
            level: { description: '推送级别', values: ['active','timeSensitive','passive','critical'] },
            badge: { description: '角标数字', values: [] },
            autoCopy: { description: '自动复制(1开启)', values: ['1'] },
            copy: { description: '复制的内容', values: [] },
            group: { description: '消息分组', values: [] },
            url: { description: '点击跳转的URL', values: [] },
            icon: { description: '自定义图标URL', values: [] },
            isArchive: { description: '是否保存(1保存)', values: ['1'] },
            call: { description: '重复铃声(1开启)', values: ['1'] },
            action: { description: '点击无弹窗', values: ['none'] },
            volume: { description: '重要警告音量(0-10)', values: [] }
        };
        const availableParams = Vue.computed(() => {
            const result = {};
            for (const k in paramOptions) {
                if (!(k in barkParams.value)) {
                    result[k] = paramOptions[k];
                }
            }
            return result;
        });
        const envMessage = ref({ text: '', type: '' });

        const script = ref({ running: false, logs: [] });
        const bark = ref({ running: false, logs: [] });

        const trackerLogOutput = ref(null);
        const barkLogOutput = ref(null);

        // --- 方法 ---
        const startScript = () => socket.value.emit('start_script');
        const stopScript = () => socket.value.emit('stop_script');
        const startBarkServer = () => socket.value.emit('start_bark_server');
        const stopBarkServer = () => socket.value.emit('stop_bark_server');

        const saveEnv = async () => {
            try {
                envVars.value.BARK_QUERY_PARAMS = buildQuery(barkParams.value);
                const response = await fetch('/update_env', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(envVars.value)
                });
                const result = await response.json();
                envMessage.value = {
                    text: result.message,
                    type: result.status === 'success' ? 'success' : 'error'
                };
            } catch (error) {
                console.error('Error updating env:', error);
                envMessage.value = {
                    text: '保存环境变量时发生错误。',
                    type: 'error'
                };
            }
        };

        const addParam = () => {
            if (newParam.value && !(newParam.value in barkParams.value)) {
                const opt = paramOptions[newParam.value];
                barkParams.value[newParam.value] = opt.values[0] || '';
                newParam.value = '';
            }
        };

        const removeParam = (param) => {
            delete barkParams.value[param];
        };

        const scrollToBottom = (element) => {
            if (element) {
                // 滚动前检查是否需要滚动，给用户自己向上查看日志的机会
                const shouldScroll = element.scrollHeight - element.clientHeight <= element.scrollTop + 50;
                if (shouldScroll) {
                    element.scrollTop = element.scrollHeight;
                }
            }
        };

        // --- 生命周期钩子 ---
        onMounted(() => {
            socket.value = io();

            socket.value.on('connect', () => {
                console.log('Connected to WebSocket server via Vue app.');
            });

            socket.value.on('script_status', (data) => {
                script.value.running = data.running;
            });
            socket.value.on('bark_server_status', (data) => {
                bark.value.running = data.running;
            });

            // 增量日志
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

            // 完整日志 (用于刷新)
            socket.value.on('full_tracker_log', async (data) => {
                const lines = data.data ? data.data.split('\n').map(line => line.replace(/\n/g, '<br>')) : [];
                if (lines.length > MAX_LOG_LINES) {
                    script.value.logs = lines.slice(-MAX_LOG_LINES);
                } else {
                    script.value.logs = lines;
                }
                await nextTick();
                scrollToBottom(trackerLogOutput.value);
            });
            socket.value.on('full_bark_log', async (data) => {
                const lines = data.data ? data.data.split('\n').map(line => line.replace(/\n/g, '<br>')) : [];
                if (lines.length > MAX_LOG_LINES) {
                    bark.value.logs = lines.slice(-MAX_LOG_LINES);
                } else {
                    bark.value.logs = lines;
                }
                await nextTick();
                scrollToBottom(barkLogOutput.value);
            });
        });

        // 返回给模板使用的所有变量和方法
        return {
            title,
            envVars,
            envMessage,
            script,
            bark,
            startScript,
            stopScript,
            startBarkServer,
            stopBarkServer,
            saveEnv,
            trackerLogOutput,
            barkLogOutput,
            barkParams,
            paramOptions,
            availableParams,
            newParam,
            addParam,
            removeParam
        };
    }
});

app.config.compilerOptions.delimiters = ['[[', ']]'];
app.mount('#app');
