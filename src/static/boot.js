// 设计包初始化入口：mountChrome 一次挂齐右上角工具位（切换器 · 账户 · 🌗 · 外观）+ 点阵背景 + hover 光斑。
// 依赖 @szyyw/design（CDN，版本见 app.py 的 DESIGN_VERSION，由模板 _design_head.html 的 import map 解析）；工具位文案由包按 locale 内置，这里不再抄。
import { mountChrome } from '@szyyw/design/chrome.js';
import { enhanceIcons } from '@szyyw/design/icons.js';
import { autoUpdate, defaultIsBusy } from '@szyyw/design/webapp.js';

const root = document.documentElement;

// 页面里的 <i data-icon> 占位（如匿名页的空状态图标）换成 SVG
enhanceIcons();

// 应用切换器 + 账户菜单只在 portal SSO 开启时挂（服务端按 SZYYW_SSO 在 <html> 上标 data-sso / data-portal）；
// 账户菜单自己向 portal 查身份：未登录显示「登录」（弹 portal 登录小窗），已登录显示头像 / 登出。
// portal: null 表示不挂，未开启时页面与接入前一致
const portal = root.dataset.sso === '1' ? root.dataset.portal || 'https://szyyw.xyz' : null;

// 外观三键（jppost_theme / jppost_palette / jppost_scheme）存本浏览器 localStorage，
// 首帧前已由模板 _appearance_head.html 里的内联脚本写回 <html>，这里只接管持久化与按钮
mountChrome({
  background: document.querySelector('.bg-layer'),
  persist: 'localStorage',
  cookiePrefix: 'jppost_',
  locale: 'zh',
  portal,
  appearance: {
    // 背景参数只存本浏览器；版本检测的升级命令：改 DESIGN_VERSION 并验证 CDN
    dotField: {
      note: true,
      update: { command: (v) => `bash scripts/update-design.sh v${v}` },
    },
  },
});

// 服务端渲染表单（门户页、登录 / 注册）有没有改过还没提交：值与页面加载时的初值（defaultValue / defaultChecked / defaultSelected）不同就算有草稿
const FORM_SKIP_TYPES = new Set(['hidden', 'submit', 'button', 'reset', 'image', 'file']);
const fieldDirty = (el) => {
  if (el.disabled || FORM_SKIP_TYPES.has(el.type)) return false;
  if (el instanceof HTMLSelectElement) return [...el.options].some((o) => o.selected !== o.defaultSelected);
  if (el.type === 'checkbox' || el.type === 'radio') return el.checked !== el.defaultChecked;
  return 'defaultValue' in el && el.value !== el.defaultValue;
};
const formsDirty = () => [...document.forms].some((form) => [...form.elements].some(fieldDirty));

// 新版本上线后自动重载（主屏 App 没有地址栏、常年开着）：<html data-version> 是服务端写的当前版本，/api/version 返回线上版本。
// 有未保存内容时不打断：控制台（Vue，表单值不在 defaultValue 里）由 app.js 提供 window.jppostHasDraft，其余页面看表单是否改过
autoUpdate({
  current: root.dataset.version,
  check: () =>
    fetch('/api/version', { cache: 'no-store', credentials: 'same-origin' })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => d?.version),
  isBusy: () => defaultIsBusy() || (typeof window.jppostHasDraft === 'function' ? window.jppostHasDraft() : formsDirty()),
});
