const { createApp, ref, onMounted, nextTick } = Vue;

const app = createApp({
    setup() {
        // --- 响应式状态定义 ---
        const title = ref('快递追踪器与通知服务控制台 (Vue.js)');
        const socket = ref(null);

        const envVars = ref(window.initialEnvVars || {});
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
        const requestRefresh = () => socket.value.emit('request_refresh');

        const saveEnv = async () => {
            try {
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
                requestRefresh();
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
                await nextTick();
                scrollToBottom(trackerLogOutput.value);
            });
            socket.value.on('bark_log', async (data) => {
                bark.value.logs.push(data.data.replace(/\n/g, '<br>'));
                await nextTick();
                scrollToBottom(barkLogOutput.value);
            });

            // 完整日志 (用于刷新)
            socket.value.on('full_tracker_log', async (data) => {
                script.value.logs = data.data ? data.data.split('\n').map(line => line.replace(/\n/g, '<br>')) : [];
                await nextTick();
                scrollToBottom(trackerLogOutput.value);
            });
            socket.value.on('full_bark_log', async (data) => {
                bark.value.logs = data.data ? data.data.split('\n').map(line => line.replace(/\n/g, '<br>')) : [];
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
            requestRefresh,
            saveEnv,
            trackerLogOutput,
            barkLogOutput,
        };
    }
});

app.config.compilerOptions.delimiters = ['[[', ']]'];
app.mount('#app');
