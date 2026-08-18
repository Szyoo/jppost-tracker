/* ============================================================
   @szyyw/design · scheme.js
   明暗模式（dark / light / auto）的状态管理与常驻切换按钮。

   状态唯一存放处是 <html data-scheme>，tokens.css 的 color-scheme
   由它驱动；本模块只负责「改属性 + 持久化 + 同步浏览器 chrome 着色」。
   框架无关：React/Vue/Flask 静态页都能用。
   ============================================================ */

import { mountCornerTool, CORNER_ORDER } from "./corner.js";

const SCHEMES = ["auto", "light", "dark"];
const ICONS = { auto: "🌗", light: "☀️", dark: "🌙" };
const EVENT = "szyyw:schemechange";

const config = {
  /** "cookie" | "localStorage" | "none"。SSR 项目必须用 cookie，服务端要读它避免首屏闪烁 */
  persist: "cookie",
  storageKey: "scheme",
  cookieDays: 365,
  /** 同步 <meta name="theme-color">（手机地址栏/状态栏着色） */
  themeColor: true
};

function readStored() {
  try {
    if (config.persist === "cookie") {
      const m = document.cookie.match(new RegExp(`(?:^|;\\s*)${config.storageKey}=([^;]*)`));
      return m ? decodeURIComponent(m[1]) : null;
    }
    if (config.persist === "localStorage") return localStorage.getItem(config.storageKey);
  } catch {
    // 隐私模式下 localStorage 可能抛错——退化为不持久化，功能仍可用
  }
  return null;
}

function writeStored(value) {
  try {
    if (config.persist === "cookie") {
      document.cookie = `${config.storageKey}=${encodeURIComponent(value)}; path=/; max-age=${
        60 * 60 * 24 * config.cookieDays
      }; samesite=lax`;
    } else if (config.persist === "localStorage") {
      localStorage.setItem(config.storageKey, value);
    }
  } catch {
    // 同上
  }
}

/**
 * 把当前实际底色写进 theme-color。
 * 不能直读 --bg：它的值是未求值的 light-dark(...)，自定义属性不做条件求值。
 * body 的 background-color 是浏览器算完的结果，才是真实颜色。
 */
function syncThemeColor() {
  if (!config.themeColor || !document.body) return;
  const bg = getComputedStyle(document.body).backgroundColor;
  if (!bg || bg === "transparent" || bg === "rgba(0, 0, 0, 0)") return;
  let meta = document.head.querySelector('meta[name="theme-color"][data-szyyw]');
  if (!meta) {
    meta = document.createElement("meta");
    meta.setAttribute("name", "theme-color");
    meta.setAttribute("data-szyyw", "");
    // 多个 theme-color 时首个匹配者生效——插到最前面，压过服务端渲染的那些
    document.head.insertBefore(meta, document.head.firstChild);
  }
  meta.setAttribute("content", bg);
}

export function getScheme() {
  const v = document.documentElement.dataset.scheme;
  return SCHEMES.includes(v) ? v : "dark";
}

export function setScheme(next, { persist = true } = {}) {
  const scheme = SCHEMES.includes(next) ? next : "dark";
  document.documentElement.dataset.scheme = scheme;
  if (persist) writeStored(scheme);
  syncThemeColor();
  document.dispatchEvent(new CustomEvent(EVENT, { detail: { scheme } }));
  return scheme;
}

/** auto → light → dark → auto */
export function cycleScheme() {
  const i = SCHEMES.indexOf(getScheme());
  return setScheme(SCHEMES[(i + 1) % SCHEMES.length]);
}

/* auto 模式下系统切换时底色会变，theme-color 也得跟着走。
   监听只绑一次：configureScheme 可能被重复调用（SPA 热更新、多入口），
   每次都绑会叠出一堆同样的监听器。 */
let systemMql = null;

function bindSystemListener() {
  if (systemMql) return;
  systemMql = window.matchMedia("(prefers-color-scheme: dark)");
  systemMql.addEventListener("change", syncThemeColor);
}

/**
 * 配置持久化方式并对齐初始状态。
 * 服务端已经渲染了 data-scheme（SSR 项目）时不覆盖它——那份才是无闪烁的真相；
 * 静态页没有服务端参与，则从存储里恢复。
 */
export function configureScheme(options = {}) {
  Object.assign(config, options);
  const stored = readStored();
  const attr = document.documentElement.dataset.scheme;
  if (!SCHEMES.includes(attr) && stored) setScheme(stored, { persist: false });
  else syncThemeColor();
  bindSystemListener();
  return getScheme();
}

/** 解绑系统明暗监听（SPA 卸载时用；不改 data-scheme，配色保持现状） */
export function destroyScheme() {
  if (!systemMql) return;
  systemMql.removeEventListener("change", syncThemeColor);
  systemMql = null;
}

/** 订阅变化（返回解绑函数），用于让框架内的 UI 与按钮状态保持同步 */
export function onSchemeChange(handler) {
  const listener = (e) => handler(e.detail.scheme);
  document.addEventListener(EVENT, listener);
  return () => document.removeEventListener(EVENT, listener);
}

/**
 * 挂载常驻切换按钮（缺省进右上角工具位，背景参数按钮排在它右边）。
 * labels 用于 title/aria-label，传当前语言的文案即可。
 * container 传了就挂到那里，不进工具位——设置页里内嵌一枚时用。
 */
export function mountSchemeToggle({
  container = null,
  labels = { auto: "Follow system", light: "Light", dark: "Dark" }
} = {}) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "corner-tool scheme-toggle";

  const render = () => {
    const s = getScheme();
    btn.textContent = ICONS[s];
    btn.dataset.scheme = s;
    const text = labels[s] ?? s;
    btn.title = text;
    btn.setAttribute("aria-label", text);
  };

  btn.addEventListener("click", () => cycleScheme());
  const off = onSchemeChange(render);
  render();

  let unmount;
  if (container) {
    container.appendChild(btn);
    unmount = () => btn.remove();
  } else {
    unmount = mountCornerTool(btn, { order: CORNER_ORDER.scheme });
  }

  return {
    destroy() {
      off();
      unmount();
    },
    update: render
  };
}
