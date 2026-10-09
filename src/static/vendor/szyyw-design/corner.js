/* ============================================================
   @szyyw/design · corner.js
   右上角常驻工具位 —— 全局开关（明暗切换、背景参数…）的共同容器。

   自身管定位与间距，工具按钮只管自己长什么样。order 小的排左边，
   所以「新工具挂在右边」＝ 给一个更大的 order。

   工具位还把自己的实测高度发布成 --corner-rail-h，供需要「让开这条」
   的浮层（背景参数抽屉）计算偏移——横竖排列、几枚按钮都对得上，
   不用在别处再写一遍魔数。v0.13.0 起再发布 --corner-rail-w（视口右缘到
   工具位左缘的距离），顶栏用 .corner-clear 让位，不再写死 px。

   项目自己的按钮优先用 createCornerButton（图标 + 无障碍文案 + 角标一次给齐），
   只想塞一个现成元素时才用 mountCornerTool。
   ============================================================ */

/** 内置工具的位次，留出间隔给项目自己的按钮 */
export const CORNER_ORDER = {
  scheme: 10,
  /** 语言切换（locale-toggle.js，v0.13.0）：明暗与外观之间 */
  locale: 15,
  settings: 20
};

const RAIL_HEIGHT_VAR = "--corner-rail-h";
const RAIL_WIDTH_VAR = "--corner-rail-w";

let rail = null;
let railObserver = null;
let railMutations = null;

const PANEL_TOP_VAR = "--corner-panel-top";
const PANEL_RIGHT_VAR = "--corner-panel-right";
/** 弹层与工具位之间的空隙 */
const PANEL_GAP = 8;

// 只在真变了才写：这些属性写在 <html> 上，DotField 的 observer 盯着 style，
// 无谓的重复写会让它白白重算一次颜色
function setVar(name, value) {
  const style = document.documentElement.style;
  if (style.getPropertyValue(name) !== value) style.setProperty(name, value);
}

/**
 * 发布工具位的实测尺寸与「弹层锚点」：
 * - --corner-rail-h：工具位高度（老浮层用它让开这条）
 * - --corner-panel-top / --corner-panel-right：所有工具位弹层（.corner-panel）的位置。
 *   横排：贴在工具位正下方、右缘对齐；纵排：贴在工具位左侧、顶端对齐——
 *   纵排时挂在下方会压住下面那几枚按钮。用实测值而不是按方向写死，
 *   未登录时「登录」按钮比一枚图标宽，写死的偏移会对不上。
 */
function publishRailHeight() {
  if (!rail?.isConnected) return;
  setVar(RAIL_HEIGHT_VAR, `${rail.offsetHeight}px`);
  const rect = rail.getBoundingClientRect();
  // fixed 元素的 right 从视口内容区右缘量起（不含滚动条），所以用 clientWidth
  const viewport = document.documentElement.clientWidth;
  // 顶栏要让开的宽度：视口右缘 → 工具位左缘（含右边距与安全区；横排 / 纵排都按实测）
  setVar(RAIL_WIDTH_VAR, `${Math.max(0, Math.round(viewport - rect.left))}px`);
  const column = getComputedStyle(rail).flexDirection.startsWith("column");
  const top = column ? rect.top : rect.bottom + PANEL_GAP;
  const right = column ? viewport - rect.left + PANEL_GAP : viewport - rect.right;
  setVar(PANEL_TOP_VAR, `${Math.round(top)}px`);
  setVar(PANEL_RIGHT_VAR, `${Math.round(right)}px`);
}

/**
 * 立刻重新实测并发布工具位高度。
 * ResizeObserver 只在页面渲染时投递回调——页面隐藏期间改了布局、
 * 或掉了通知，值就会是旧的。浮层弹出前调一次，保证那一刻的偏移是准的。
 */
export function syncCornerRail() {
  publishRailHeight();
}

/** 取得（必要时创建）右上角工具位容器 */
export function cornerRail() {
  if (rail?.isConnected) return rail;
  rail = document.querySelector(".corner-tools");
  if (!rail) {
    rail = document.createElement("div");
    rail.className = "corner-tools";
    document.body.appendChild(rail);
  }
  if (typeof ResizeObserver !== "undefined") {
    railObserver = new ResizeObserver(publishRailHeight);
    railObserver.observe(rail);
  }
  // 按钮增减 / 显隐（含项目直接 append 进 .corner-tools 的）也要重算；
  // 只写 <html> 的 style，不碰工具位自身，不会自激
  if (typeof MutationObserver !== "undefined") {
    railMutations?.disconnect();
    railMutations = new MutationObserver(publishRailHeight);
    railMutations.observe(rail, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ["hidden", "class", "style"]
    });
  }
  // 窗口变宽变窄时工具位自身尺寸不变（观察不到），但 right 的像素值要跟着重算
  window.addEventListener("resize", publishRailHeight, { passive: true });
  publishRailHeight();
  return rail;
}

/**
 * 把按钮放进右上角工具位，返回卸载函数。
 * 容器在最后一个工具卸载后自动移除，不留空壳挡住底下的点击。
 */
export function mountCornerTool(el, { order = 50 } = {}) {
  const host = cornerRail();
  el.style.order = String(order);
  host.appendChild(el);
  // 没有 ResizeObserver 的老浏览器靠这里兜底
  publishRailHeight();
  return () => {
    el.remove();
    if (host.childElementCount === 0) {
      host.remove();
      railObserver?.disconnect();
      railObserver = null;
      railMutations?.disconnect();
      railMutations = null;
      document.documentElement.style.removeProperty(RAIL_HEIGHT_VAR);
      document.documentElement.style.removeProperty(RAIL_WIDTH_VAR);
      document.documentElement.style.removeProperty(PANEL_TOP_VAR);
      document.documentElement.style.removeProperty(PANEL_RIGHT_VAR);
      window.removeEventListener("resize", publishRailHeight);
      if (rail === host) rail = null;
    } else {
      publishRailHeight();
    }
  };
}

/* ---------- 工具位弹出面板的「同一时刻只开一个」 ----------
   切换器、账户菜单等挂在工具位下方的面板共用同一块位置，
   一个打开时要先关掉另一个。各面板打开时 claim，关闭时 release。 */

let openPanelClose = null;

/**
 * 声明「我这个面板要打开了」：先关掉当前开着的别的面板。
 * @param {() => void} close 本面板的关闭函数
 * @returns {() => void} release——本面板关闭时调用（重复调用无害）
 */
export function claimCornerPanel(close) {
  // 弹出那一刻把锚点量准（ResizeObserver 只在渲染时投递，页面隐藏期间可能掉通知）
  publishRailHeight();
  if (openPanelClose && openPanelClose !== close) {
    const prev = openPanelClose;
    openPanelClose = null;
    prev();
  }
  openPanelClose = close;
  return () => {
    if (openPanelClose === close) openPanelClose = null;
  };
}

/* ---------- 现成的工具位按钮（v0.13.0）----------
   项目自己的全局按钮（通知、管理、帮助…）一次给齐：图标、无障碍文案、位次、角标。
   之前各项目照 .corner-tool 抄一份按钮样式、自己拼角标——用这个就不必了。 */

/** 角标显示值：null / false / 0 / "" → 隐藏；true → 小圆点；数字 → 计数（> 99 显示 99+）；字符串原样 */
function badgeText(value) {
  if (value === null || value === undefined || value === false || value === 0 || value === "") return null;
  if (value === true) return "";
  if (typeof value === "number") return value > 99 ? "99+" : String(Math.max(0, Math.floor(value)));
  return String(value);
}

/**
 * 造一枚工具位按钮并挂进右上角工具位。
 *
 * @param {object} options
 * @param {string|Node} options.icon   SVG / 文本字符串（按 HTML 插入，只传自己写死的图标，别传用户输入）或现成节点
 * @param {string}  options.label      无障碍名（aria-label + title），必填
 * @param {number}  options.order      工具位位次，缺省 50（外观 20 右边）
 * @param {(e: MouseEvent) => void} options.onClick
 * @param {string}  options.href       给了就渲染成 <a class="corner-tool">（如「管理」直接跳页）
 * @param {number|boolean|string|null} options.badge  初始角标，语义同 setBadge
 * @param {string}  options.className  额外的类（项目自己的修饰类）
 * @returns {{ el: HTMLElement, setBadge(value: number|boolean|string|null): void, destroy(): void }}
 */
export function createCornerButton({ icon = "", label = "", order = 50, onClick = null, href = null, badge = null, className = "" } = {}) {
  const el = document.createElement(href ? "a" : "button");
  if (href) el.href = href;
  else el.type = "button";
  el.className = "corner-tool" + (className ? " " + className : "");
  if (typeof icon === "string") el.innerHTML = icon;
  else if (icon) el.append(icon);
  if (label) {
    el.title = label;
    el.setAttribute("aria-label", label);
  }
  if (onClick) el.addEventListener("click", onClick);

  const dot = document.createElement("span");
  dot.className = "corner-badge";
  dot.setAttribute("aria-hidden", "true");
  dot.hidden = true;
  el.append(dot);

  function setBadge(value) {
    const text = badgeText(value);
    dot.hidden = text === null;
    dot.textContent = text ?? "";
    // 角标对读屏不可见，计数并进按钮的无障碍名
    if (label) el.setAttribute("aria-label", text ? `${label} (${text})` : label);
  }
  setBadge(badge);

  const unmount = mountCornerTool(el, { order });
  return {
    el,
    setBadge,
    destroy() {
      if (onClick) el.removeEventListener("click", onClick);
      unmount();
    }
  };
}
