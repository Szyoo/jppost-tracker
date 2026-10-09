/* ============================================================
   @szyyw/design · menu.js
   锚定按钮的轻量下拉（.menu / .menu-item）的开合与定位——表格行操作「⋯」这类。

   纯 CSS 也能用（.menu-wrap 里 .menu 绝对定位在按钮下方），但 .table-wrap 的横向滚动
   会把它裁掉、.glass 的 backdrop-filter 又会困住 fixed 定位。所以打开时本模块把菜单
   暂时搬到 <body> 下、按按钮的实测位置 fixed 定位（下方放不下就翻到上方），关闭时搬回原处。

     <div class="menu-wrap">
       <button class="btn-ghost btn-small" aria-label="操作">⋯</button>
       <div class="menu" hidden>
         <button class="menu-item">编辑</button>
         <button class="menu-item danger">删除</button>
       </div>
     </div>
     attachMenu(trigger, trigger.nextElementSibling);

   点菜单项 / 点外面 / Esc / 页面滚动与缩放都会关闭；同一时刻只开一个。
   ============================================================ */

const GAP = 4;
const EDGE = 8;

let openClose = null;

/**
 * @param {HTMLElement} trigger  触发按钮
 * @param {HTMLElement} menu     .menu 元素（初始 hidden）
 * @param {object} options
 * @param {"end"|"start"} options.align  与按钮右缘（end，缺省）还是左缘对齐
 * @param {boolean} options.closeOnSelect  点 .menu-item 后关闭，缺省 true
 * @returns {{ open(): void, close(): void, toggle(): void, readonly isOpen: boolean, destroy(): void }}
 */
export function attachMenu(trigger, menu, { align = "end", closeOnSelect = true } = {}) {
  let open = false;
  let home = null;
  let next = null;

  trigger.setAttribute("aria-haspopup", "menu");
  trigger.setAttribute("aria-expanded", "false");
  menu.setAttribute("role", menu.getAttribute("role") || "menu");
  menu.hidden = true;

  function place() {
    const r = trigger.getBoundingClientRect();
    const vw = document.documentElement.clientWidth;
    const vh = window.innerHeight;
    const m = menu.getBoundingClientRect();
    let left = align === "start" ? r.left : r.right - m.width;
    left = Math.min(Math.max(EDGE, left), vw - m.width - EDGE);
    let top = r.bottom + GAP;
    if (top + m.height > vh - EDGE && r.top - GAP - m.height >= EDGE) top = r.top - GAP - m.height;
    menu.style.left = `${Math.round(left)}px`;
    menu.style.top = `${Math.round(top)}px`;
  }

  function onDocClick(e) {
    if (!menu.contains(e.target) && !trigger.contains(e.target)) close();
  }
  function onKey(e) {
    if (e.key === "Escape") {
      close();
      trigger.focus({ preventScroll: true });
    }
  }
  // 页面（含 .table-wrap 等任意祖先）滚动时 fixed 菜单会和按钮脱节，直接关；菜单自身内部滚动不算
  function onScroll(e) {
    if (!menu.contains(e.target)) close();
  }
  function onMenuClick(e) {
    if (closeOnSelect && e.target.closest?.(".menu-item")) close();
  }

  function doOpen() {
    if (open) return;
    if (openClose) openClose();
    open = true;
    openClose = close;
    home = menu.parentNode;
    next = menu.nextSibling;
    document.body.appendChild(menu);
    menu.classList.add("menu-floating");
    menu.hidden = false;
    place();
    trigger.setAttribute("aria-expanded", "true");
    document.addEventListener("click", onDocClick, true);
    document.addEventListener("keydown", onKey);
    window.addEventListener("scroll", onScroll, { capture: true, passive: true });
    window.addEventListener("resize", close, { passive: true });
  }

  function close() {
    if (!open) return;
    open = false;
    if (openClose === close) openClose = null;
    menu.hidden = true;
    menu.classList.remove("menu-floating");
    menu.style.left = "";
    menu.style.top = "";
    if (home?.isConnected) home.insertBefore(menu, next?.parentNode === home ? next : null);
    else menu.remove();
    trigger.setAttribute("aria-expanded", "false");
    document.removeEventListener("click", onDocClick, true);
    document.removeEventListener("keydown", onKey);
    window.removeEventListener("scroll", onScroll, { capture: true });
    window.removeEventListener("resize", close);
  }

  const toggle = () => (open ? close() : doOpen());
  trigger.addEventListener("click", toggle);
  menu.addEventListener("click", onMenuClick);

  return {
    open: doOpen,
    close,
    toggle,
    get isOpen() {
      return open;
    },
    destroy() {
      close();
      trigger.removeEventListener("click", toggle);
      menu.removeEventListener("click", onMenuClick);
    }
  };
}
