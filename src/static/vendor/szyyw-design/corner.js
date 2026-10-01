/* ============================================================
   @szyyw/design · corner.js
   右上角常驻工具位 —— 全局开关（明暗切换、背景参数…）的共同容器。

   自身管定位与间距，工具按钮只管自己长什么样。order 小的排左边，
   所以「新工具挂在右边」＝ 给一个更大的 order。

   工具位还把自己的实测高度发布成 --corner-rail-h，供需要「让开这条」
   的浮层（背景参数抽屉）计算偏移——横竖排列、几枚按钮都对得上，
   不用在别处再写一遍魔数。
   ============================================================ */

/** 内置工具的位次，留出间隔给项目自己的按钮 */
export const CORNER_ORDER = {
  scheme: 10,
  settings: 20
};

const RAIL_HEIGHT_VAR = "--corner-rail-h";

let rail = null;
let railObserver = null;

function publishRailHeight() {
  if (!rail?.isConnected) return;
  const value = `${rail.offsetHeight}px`;
  // 只在真变了才写：这个属性写在 <html> 上，DotField 的 observer 盯着 style，
  // 无谓的重复写会让它白白重算一次颜色
  if (document.documentElement.style.getPropertyValue(RAIL_HEIGHT_VAR) === value) return;
  document.documentElement.style.setProperty(RAIL_HEIGHT_VAR, value);
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
      document.documentElement.style.removeProperty(RAIL_HEIGHT_VAR);
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
