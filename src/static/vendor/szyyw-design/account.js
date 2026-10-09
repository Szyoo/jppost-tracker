/* ============================================================
   @szyyw/design · account.js
   账户菜单——右上角工具位里紧挨应用切换器的那一枚。
   未登录：「登录」按钮，弹 portal 的登录小窗（被拦就整页跳转）；
   登录后：圆形头像（用户名首字），点开是用户名 + 角色 + 账户设置 / 登出。

   身份来自 portal 的 /api/me（跨源 fetch 带 credentials，cookie 是
   Domain=.szyyw.xyz）。登录小窗成功后 postMessage
   { type: "szyyw-portal:login", ... } 回来，本模块只认 portal 源发来的。
   ============================================================ */

import { mountCornerTool, claimCornerPanel } from "./corner.js";
import { chromeText, roleLabel } from "./chrome-text.js";

/** 账户菜单在工具位里的位次：切换器(5) 右边、明暗切换(10) 左边 */
export const ACCOUNT_ORDER = 6;

const LOGIN_MESSAGE = "szyyw-portal:login";
const RETRY_MS = 3000;

const USER_ICON =
  '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8"' +
  ' stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
  '<circle cx="12" cy="8" r="4"/><path d="M4 21c0-4.4 3.6-8 8-8s8 3.6 8 8"/></svg>';

const ROLE_PILL = { admin: "cyan", user: "green", guest: "violet" };

const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

/** 头像字：首个字符（按码点取，不劈开代理对）；拉丁字母转大写，CJK 原样 */
function initial(name) {
  const first = [...String(name || "?").trim()][0] || "?";
  return first.toUpperCase();
}

/**
 * 挂载账户菜单。
 *
 * @param {object}   options
 * @param {string}   options.portal    门户地址，缺省 https://szyyw.xyz
 * @param {number}   options.order     工具位位次，缺省 6
 * @param {(data: object) => void} options.onChange  登录小窗回报成功时调用（随后整页 reload）
 * @param {string}   options.locale    "zh" | "ja" | "en"（v0.13.0），缺省 "zh"——与旧版文案逐字相同
 * @param {object}   options.labels    逐键覆盖内置文案：{ login, settings, logout, logoutFailed, statusFmt, roles: { admin, user, … } }
 * @returns {{ refresh(): Promise<void>, close(): void, setLocale(locale: string): void, destroy(): void, readonly me: object | null }}
 */
export function mountAccountMenu({ portal = "https://szyyw.xyz", order = ACCOUNT_ORDER, onChange, locale = "zh", labels = {} } = {}) {
  const base = portal.replace(/\/$/, "");
  const portalOrigin = new URL(base).origin;
  // 内置文案（chrome-text.js）打底，labels 逐键覆盖（roles 逐个合并）；setLocale 换底不丢覆盖
  const textFor = (loc) => chromeText(loc, { account: labels || {} }).account;
  let T = textFor(locale);

  let me = null;
  let btn = null;
  let unmount = null;
  let panel = null;
  let release = null;
  let retryTimer = null;
  let destroyed = false;

  /* ---------- 挂/卸按钮 ---------- */

  function clearButton() {
    close();
    panel?.remove();
    panel = null;
    unmount?.();
    unmount = null;
    btn = null;
    window.removeEventListener("message", onMessage);
  }

  function makeButton(cls) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = `corner-tool ${cls}`;
    return b;
  }

  function renderAnonymous() {
    clearButton();
    btn = makeButton("account-login");
    btn.innerHTML = `${USER_ICON}<span>${esc(T.login)}</span>`;
    btn.title = T.login;
    btn.addEventListener("click", login);
    window.addEventListener("message", onMessage);
    unmount = mountCornerTool(btn, { order });
  }

  function renderUser() {
    clearButton();
    btn = makeButton("account-avatar");
    btn.textContent = initial(me.user);
    btn.title = me.user;
    btn.setAttribute("aria-label", me.user);
    btn.setAttribute("aria-haspopup", "true");
    btn.setAttribute("aria-expanded", "false");
    btn.addEventListener("click", () => (panel.hidden ? open() : close()));

    const role = String(me.role || "");
    panel = document.createElement("div");
    panel.className = "glass corner-panel account-menu";
    panel.hidden = true;
    panel.setAttribute("role", "menu");
    panel.innerHTML =
      `<div class="account-menu-head">` +
      `<span class="account-menu-dot" aria-hidden="true">${esc(initial(me.user))}</span>` +
      `<span class="account-menu-name">${esc(me.user)}</span>` +
      (role ? `<span class="pill ${ROLE_PILL[role] || ""}">${esc(roleLabel(role, T.roles))}</span>` : "") +
      `</div>` +
      `<div class="app-switcher-sep"></div>` +
      `<button type="button" class="account-menu-item" role="menuitem" data-act="settings">${esc(T.settings)}</button>` +
      `<button type="button" class="account-menu-item danger" role="menuitem" data-act="logout">${esc(T.logout)}</button>` +
      `<div class="account-menu-err err-text" hidden></div>`;
    panel.querySelector('[data-act="settings"]').addEventListener("click", () => {
      location.assign(base + "/?account=1");
    });
    panel.querySelector('[data-act="logout"]').addEventListener("click", logout);
    document.body.appendChild(panel);
    unmount = mountCornerTool(btn, { order });
  }

  /* ---------- 身份 ---------- */

  async function load(isRetry = false) {
    clearTimeout(retryTimer);
    let r;
    try {
      r = await fetch(base + "/api/me", { credentials: "include" });
    } catch {
      // 网络错误：什么都不显示，3 秒后重试一次
      if (destroyed) return;
      clearButton();
      if (!isRetry) retryTimer = setTimeout(() => load(true), RETRY_MS);
      return;
    }
    if (destroyed) return;
    if (r.status === 401) {
      me = null;
      return renderAnonymous();
    }
    if (!r.ok) {
      // 其余非 2xx 当作暂时故障，同网络错误处理
      clearButton();
      if (!isRetry) retryTimer = setTimeout(() => load(true), RETRY_MS);
      return;
    }
    try {
      const data = await r.json();
      if (destroyed) return;
      me = { id: data.id, user: String(data.user ?? ""), role: data.role };
      renderUser();
    } catch {
      clearButton();
    }
  }

  /* ---------- 登录 / 登出 ---------- */

  function login() {
    let w = null;
    try {
      w = window.open(base + "/login?popup=1", "szyyw-login", "width=420,height=560,popup=yes");
    } catch {
      w = null;
    }
    if (!w) location.assign(base + "/login?rd=" + encodeURIComponent(location.href));
  }

  function onMessage(event) {
    if (event.origin !== portalOrigin) return;
    const data = event.data;
    if (data?.type !== LOGIN_MESSAGE) return;
    try {
      onChange?.(data);
    } finally {
      location.reload();
    }
  }

  async function logout() {
    const btnOut = panel.querySelector('[data-act="logout"]');
    const err = panel.querySelector(".account-menu-err");
    err.hidden = true;
    btnOut.disabled = true;
    try {
      const r = await fetch(base + "/api/logout", { method: "POST", credentials: "include" });
      if (!r.ok) throw new Error(String(r.status));
      location.reload();
    } catch (e) {
      btnOut.disabled = false;
      const code = e?.message && /^\d+$/.test(e.message) ? e.message : "";
      err.textContent = T.logoutFailed.replace("{status}", code ? T.statusFmt.replace("{code}", code) : "");
      err.hidden = false;
    }
  }

  /* ---------- 面板开合 ---------- */

  function onDocClick(e) {
    if (!panel.contains(e.target) && !btn.contains(e.target)) close();
  }
  function onKey(e) {
    if (e.key === "Escape") close();
  }

  function open() {
    if (!panel || !panel.hidden) return;
    release = claimCornerPanel(close);
    panel.hidden = false;
    btn.setAttribute("aria-expanded", "true");
    document.addEventListener("click", onDocClick, true);
    document.addEventListener("keydown", onKey);
  }
  function close() {
    if (!panel || panel.hidden) return;
    release?.();
    release = null;
    panel.hidden = true;
    panel.querySelector(".account-menu-err").hidden = true;
    btn.setAttribute("aria-expanded", "false");
    document.removeEventListener("click", onDocClick, true);
    document.removeEventListener("keydown", onKey);
  }

  load();

  return {
    refresh: () => load(true),
    close,
    /** v0.13.0：换语言。已渲染的按钮 / 面板按当前身份就地重画（开着的面板会关上），不重新请求 /api/me */
    setLocale(next) {
      T = textFor(next);
      if (destroyed || !btn) return;
      if (me) renderUser();
      else renderAnonymous();
    },
    destroy() {
      destroyed = true;
      clearTimeout(retryTimer);
      clearButton();
    },
    get me() {
      return me;
    }
  };
}
