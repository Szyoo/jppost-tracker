// 设计包初始化入口：mountChrome 一次挂齐右上角工具位（切换器 · 账户 · 🌗 · 外观）+ 点阵背景 + hover 光斑。
// 依赖 @szyyw/design（CDN，版本见 app.py 的 DESIGN_VERSION，由模板 _design_head.html 的 import map 解析）；工具位文案由包按 locale 内置，这里不再抄。
import { mountChrome } from '@szyyw/design/chrome.js';

const root = document.documentElement;

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
