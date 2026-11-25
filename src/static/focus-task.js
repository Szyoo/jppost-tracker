// 深链 /?tn=<单号>：打开页面后滚到这个单号的卡片并短暂高亮（集运助手等外部应用从订单跳过来用）。
// 两个页面的卡片都带 data-tn；控制台页的「已归档」是展开时才渲染卡片，所以找不到时先展开归档区再等一会。
import { toast } from '@szyyw/design/toast.js';

// 和后端的单号规则一致：去空白、转大写
const normalize = (value) => String(value || '').replace(/\s+/g, '').toUpperCase();
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function focusTaskFromUrl() {
    const url = new URL(window.location.href);
    const tn = normalize(url.searchParams.get('tn'));
    if (!tn) return;
    // 用过就从地址栏去掉，刷新或自动更新重载时不再反复跳
    url.searchParams.delete('tn');
    window.history.replaceState(window.history.state, '', url.pathname + url.search + url.hash);

    const selector = `article[data-tn="${CSS.escape(tn)}"]`;
    for (let i = 0; i < 30; i += 1) {
        const card = document.querySelector(selector);
        if (card) {
            for (let node = card.parentElement; node; node = node.parentElement) {
                if (node.tagName === 'DETAILS') node.open = true;
            }
            await sleep(50);
            card.scrollIntoView({ behavior: 'smooth', block: 'center' });
            card.classList.add('is-focus');
            setTimeout(() => card.classList.remove('is-focus'), 4000);
            return;
        }
        const archived = document.querySelector('details[data-archived-panel]:not([open])');
        if (archived) archived.open = true;
        await sleep(100);
    }
    toast(`没找到单号 ${tn}：可能不在你能看到的账号下，或已被删除`, { tone: 'warn', timeout: 5000 });
}

focusTaskFromUrl();
