/* ============================================================
   @szyyw/design · switcher.js
   应用切换器——右上角工具位最左边的「九宫格」按钮，点开列出当前账号
   能进的所有站点，外加「回到门户」。列表来自 portal 的 /api/apps，
   由 portal 的权限矩阵决定；本模块只负责拿来显示。

   任何 *.szyyw.xyz 站点挂上它，就有了谷歌那种跨服务导航。
   跨源 fetch 带 credentials：portal 的会话 cookie 是 Domain=.szyyw.xyz，
   同站请求会带上；portal 侧要回 CORS 头（它已经这么做了）。
   ============================================================ */

import { mountCornerTool, claimCornerPanel } from "./corner.js";
import { chromeText } from "./chrome-text.js";

// 账户菜单与切换器同属一族「工具位面板」，从这里也能拿到
export { mountAccountMenu, ACCOUNT_ORDER } from "./account.js";

/** 切换器在工具位里的位次：比明暗切换(10)更靠左 */
export const SWITCHER_ORDER = 5;

const GRID_ICON =
  '<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor" aria-hidden="true">' +
  '<circle cx="5" cy="5" r="2"/><circle cx="12" cy="5" r="2"/><circle cx="19" cy="5" r="2"/>' +
  '<circle cx="5" cy="12" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="19" cy="12" r="2"/>' +
  '<circle cx="5" cy="19" r="2"/><circle cx="12" cy="19" r="2"/><circle cx="19" cy="19" r="2"/></svg>';

const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

/**
 * 挂载应用切换器。
 *
 * @param {object}  options
 * @param {string}  options.portal     门户地址（也是 /api/apps 的来源），缺省 https://szyyw.xyz
 * @param {string}  options.endpoint   相对门户的列表接口，缺省 /api/apps
 * @param {number}  options.order      工具位位次，缺省 5（最左）
 * @param {string}  options.current    当前站点 host，用来高亮；缺省 location.host
 * @param {number}  options.cacheMs    列表缓存时长，缺省 60s；0 = 每次打开都拉
 * @param {object}  options.labels     文案（逐键覆盖 locale 的内置文案）
 * @param {string}  options.locale     "zh" | "ja" | "en"（v0.13.0），缺省 "zh"——与旧版文案逐字相同
 * @returns {{ open(): void, close(): void, refresh(): Promise<void>, setLocale(locale: string): void, destroy(): void }}
 */
export function mountAppSwitcher({
  portal = "https://szyyw.xyz",
  endpoint = "/api/apps",
  order = SWITCHER_ORDER,
  current = typeof location !== "undefined" ? location.host : "",
  cacheMs = 60_000,
  labels = {},
  locale = "zh"
} = {}) {
  // 内置文案（chrome-text.js）打底，labels 逐键覆盖；setLocale 换底不丢覆盖
  const textFor = (loc) => ({ ...chromeText(loc).switcher, ...(labels || {}) });
  let L = textFor(locale);

  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "corner-tool app-switcher-toggle";
  btn.innerHTML = GRID_ICON;
  applyButtonText();
  btn.setAttribute("aria-haspopup", "true");
  btn.setAttribute("aria-expanded", "false");

  const panel = document.createElement("div");
  panel.className = "glass corner-panel app-switcher";
  panel.hidden = true;
  panel.setAttribute("role", "menu");

  let apps = null;
  let loadedAt = 0;
  let inflight = null;
  let release = null;
  /** 当前显示的提示（文案键），切语言时按键重画；null = 列表 */
  let shownMessage = null;

  function renderMessage(key) {
    shownMessage = key;
    panel.innerHTML = `<div class="app-switcher-msg">${esc(L[key])}</div>`;
  }

  function applyButtonText() {
    btn.title = L.open;
    btn.setAttribute("aria-label", L.open);
  }

  function renderList() {
    if (!apps) return renderMessage("loading");
    shownMessage = null;
    const portalEntry = apps.find((a) => a.portal) || { host: new URL(portal).host, url: portal, title: L.portal };
    const others = apps.filter((a) => !a.portal);
    const item = (a, cls = "") => {
      const here = a.host === current;
      return (
        `<a class="app-switcher-item${cls}${here ? " is-current" : ""}" href="${esc(a.url)}" role="menuitem"` +
        `${here ? ' aria-current="page"' : ""}>` +
        `<span class="app-switcher-dot" aria-hidden="true">${esc((a.title || a.host).slice(0, 1))}</span>` +
        `<span class="app-switcher-text"><span class="app-switcher-title">${esc(a.title || a.host)}</span>` +
        `<span class="app-switcher-host">${esc(a.host)}</span></span></a>`
      );
    };
    panel.innerHTML =
      item({ ...portalEntry, title: L.portal }, " app-switcher-portal") +
      (others.length ? `<div class="app-switcher-sep"></div>${others.map((a) => item(a)).join("")}` : `<div class="app-switcher-msg">${esc(L.empty)}</div>`);
  }

  async function load(force = false) {
    if (!force && apps && cacheMs > 0 && Date.now() - loadedAt < cacheMs) return;
    if (inflight) return inflight;
    inflight = (async () => {
      try {
        const r = await fetch(portal.replace(/\/$/, "") + endpoint, { credentials: "include" });
        if (r.status === 401) {
          apps = null;
          renderMessage("unauth");
          return;
        }
        if (!r.ok) throw new Error(String(r.status));
        const data = await r.json();
        apps = Array.isArray(data.apps) ? data.apps : [];
        loadedAt = Date.now();
        renderList();
      } catch {
        apps = null;
        renderMessage("error");
      } finally {
        inflight = null;
      }
    })();
    return inflight;
  }

  function onDocClick(e) {
    if (!panel.contains(e.target) && !btn.contains(e.target)) close();
  }
  function onKey(e) {
    if (e.key === "Escape") close();
  }

  function open() {
    if (!panel.hidden) return;
    release = claimCornerPanel(close);
    panel.hidden = false;
    btn.setAttribute("aria-expanded", "true");
    renderList();
    load();
    document.addEventListener("click", onDocClick, true);
    document.addEventListener("keydown", onKey);
  }
  function close() {
    if (panel.hidden) return;
    release?.();
    release = null;
    panel.hidden = true;
    btn.setAttribute("aria-expanded", "false");
    document.removeEventListener("click", onDocClick, true);
    document.removeEventListener("keydown", onKey);
  }

  btn.addEventListener("click", () => (panel.hidden ? open() : close()));
  document.body.appendChild(panel);
  const unmount = mountCornerTool(btn, { order });

  return {
    open,
    close,
    refresh: () => load(true),
    /** v0.13.0：换语言，按钮提示与面板（开着也算）就地重画，不重新拉列表 */
    setLocale(next) {
      L = textFor(next);
      applyButtonText();
      if (shownMessage) renderMessage(shownMessage);
      else if (apps) renderList();
    },
    destroy() {
      close();
      panel.remove();
      unmount();
    }
  };
}
