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
 * @param {object}  options.labels     文案
 * @returns {{ open(): void, close(): void, refresh(): Promise<void>, destroy(): void }}
 */
export function mountAppSwitcher({
  portal = "https://szyyw.xyz",
  endpoint = "/api/apps",
  order = SWITCHER_ORDER,
  current = typeof location !== "undefined" ? location.host : "",
  cacheMs = 60_000,
  labels = {}
} = {}) {
  const L = {
    open: "应用",
    portal: "回到门户",
    loading: "加载中…",
    empty: "没有可打开的应用",
    unauth: "未登录",
    error: "加载失败",
    ...labels
  };

  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "corner-tool app-switcher-toggle";
  btn.innerHTML = GRID_ICON;
  btn.title = L.open;
  btn.setAttribute("aria-label", L.open);
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

  function renderMessage(text) {
    panel.innerHTML = `<div class="app-switcher-msg">${esc(text)}</div>`;
  }

  function renderList() {
    if (!apps) return renderMessage(L.loading);
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
          renderMessage(L.unauth);
          return;
        }
        if (!r.ok) throw new Error(String(r.status));
        const data = await r.json();
        apps = Array.isArray(data.apps) ? data.apps : [];
        loadedAt = Date.now();
        renderList();
      } catch {
        apps = null;
        renderMessage(L.error);
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
    destroy() {
      close();
      panel.remove();
      unmount();
    }
  };
}
