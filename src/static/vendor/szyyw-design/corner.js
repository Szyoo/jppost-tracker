/* ============================================================
   @szyyw/design · corner.js
   右上角常驻工具位 —— 全局开关（明暗切换、背景参数…）的共同容器。

   自身管定位与间距，工具按钮只管自己长什么样。order 小的排左边，
   所以「新工具挂在右边」＝ 给一个更大的 order。
   ============================================================ */

/** 内置工具的位次，留出间隔给项目自己的按钮 */
export const CORNER_ORDER = {
  scheme: 10,
  settings: 20
};

let rail = null;

/** 取得（必要时创建）右上角工具位容器 */
export function cornerRail() {
  if (rail?.isConnected) return rail;
  rail = document.querySelector(".corner-tools");
  if (!rail) {
    rail = document.createElement("div");
    rail.className = "corner-tools";
    document.body.appendChild(rail);
  }
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
  return () => {
    el.remove();
    if (host.childElementCount === 0) {
      host.remove();
      if (rail === host) rail = null;
    }
  };
}
