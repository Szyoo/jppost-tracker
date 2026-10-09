/* ============================================================
   @szyyw/design · locale-toggle.js
   语言切换——右上角工具位里明暗(10) 与外观(20) 之间的一枚（order 15）。
   按钮显示当前语言的短名（中 / 日 / EN），点开 .glass.corner-panel 列表，
   每种语言用它自己的写法列出（中文 / 日本語 / English），切错了也认得回来。

   本模块只管「选」：选中后调 onChange(locale)，界面文案怎么换由应用决定
   （存账号偏好 / 写 cookie / reload 都在 onChange 里）。mountChrome 会顺带把
   工具位其余部件（切换器、账户菜单、外观）的文案一起换掉。
   与其余工具位弹层同一套规则：claimCornerPanel 互斥、无遮罩、Esc / 点外面关闭。
   ============================================================ */

import { mountCornerTool, claimCornerPanel, CORNER_ORDER } from "./corner.js";
import { chromeText, chromeLocale } from "./chrome-text.js";

const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

/**
 * 挂载语言切换。
 *
 * @param {object}   options
 * @param {string[]} options.locales   可选语言代码，如 ["zh", "ja", "en"]（按此顺序列出）
 * @param {string}   options.current   当前语言；缺省取 locales[0]
 * @param {(locale: string) => void} options.onChange  用户选了另一种语言时调用
 * @param {number}   options.order     工具位位次，缺省 15
 * @param {object}   options.labels    覆盖文案：{ title?, names?: { zh: … }, short?: { zh: … } }
 * @returns {{ el: HTMLButtonElement, readonly current: string, set(locale: string): void, open(): void, close(): void, destroy(): void }}
 */
export function mountLocaleToggle({
  locales = ["zh", "ja", "en"],
  current = null,
  onChange = null,
  order = CORNER_ORDER.locale,
  labels = {}
} = {}) {
  const list = [...new Set((locales || []).map(String))].filter(Boolean);
  if (!list.length) throw new Error("mountLocaleToggle 需要至少一种 locales");
  let cur = list.includes(current) ? current : list[0];

  /** 界面语言跟着当前选中项走（「语言」/「言語」/「Language」），labels 逐键覆盖 */
  const textFor = (loc) => chromeText(chromeLocale(loc), { locale: labels || {} }).locale;
  const nameOf = (T, loc) => T.names[loc] ?? loc;
  const shortOf = (T, loc) => T.short[loc] ?? loc.toUpperCase();

  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "corner-tool locale-toggle";
  btn.setAttribute("aria-haspopup", "true");
  btn.setAttribute("aria-expanded", "false");

  const panel = document.createElement("div");
  panel.className = "glass corner-panel locale-menu";
  panel.hidden = true;
  panel.setAttribute("role", "menu");

  let release = null;

  function render() {
    const T = textFor(cur);
    btn.textContent = shortOf(T, cur);
    const label = `${T.title}: ${nameOf(T, cur)}`;
    btn.title = label;
    btn.setAttribute("aria-label", label);
    panel.setAttribute("aria-label", T.title);
    panel.innerHTML = list
      .map((loc) => {
        const on = loc === cur;
        return (
          `<button type="button" class="locale-menu-item${on ? " active" : ""}" role="menuitemradio"` +
          ` aria-checked="${on}" data-locale="${esc(loc)}" lang="${esc(loc)}">` +
          `<span class="locale-menu-short" aria-hidden="true">${esc(shortOf(T, loc))}</span>` +
          `<span class="locale-menu-name">${esc(nameOf(T, loc))}</span></button>`
        );
      })
      .join("");
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

  function set(loc) {
    if (!list.includes(loc) || loc === cur) return;
    cur = loc;
    render();
  }

  panel.addEventListener("click", (e) => {
    const item = e.target.closest?.(".locale-menu-item");
    if (!item) return;
    const loc = item.dataset.locale;
    close();
    if (loc === cur) return;
    set(loc);
    try {
      onChange?.(loc);
    } catch (err) {
      // 应用侧处理失败不打断界面
      console.error(err);
    }
  });
  btn.addEventListener("click", () => (panel.hidden ? open() : close()));

  render();
  document.body.appendChild(panel);
  const unmount = mountCornerTool(btn, { order });

  return {
    el: btn,
    get current() {
      return cur;
    },
    /** 外部换了语言时同步显示（不触发 onChange） */
    set,
    open,
    close,
    destroy() {
      close();
      panel.remove();
      unmount();
    }
  };
}
