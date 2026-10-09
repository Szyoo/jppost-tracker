/* ============================================================
   @szyyw/design · toast.js
   操作反馈 toast：视口底部居中堆叠，几秒后自己消失，点一下立刻关。
   样子在 components.css 的 .toast-region / .toast；本模块只管 DOM 与计时。

     import { toast } from "@szyyw/design/toast";
     toast("已保存");                                   // info
     toast("保存失败：网络错误", { tone: "err", timeout: 6000 });

   - 区域懒创建（第一次调用时挂到 body），aria-live="polite"；err 单条用 role="alert" 立即播报
   - 同时最多留 4 条，新的进来挤掉最旧的
   - timeout: 0 = 不自动消失（只能点掉 / 调 close）
   - prefers-reduced-motion 下没有入场 / 退场动画
   模块顶层不碰 DOM：SSR 时被求值也不会炸。
   ============================================================ */

const TONES = ["ok", "err", "warn", "info"];
const ICONS = { ok: "✓", err: "!", warn: "!", info: "i" };
const MAX = 4;
const LEAVE_MS = 220;

let region = null;

function ensureRegion() {
  if (region?.isConnected) return region;
  region = document.querySelector(".toast-region");
  if (!region) {
    region = document.createElement("div");
    region.className = "toast-region";
    region.setAttribute("role", "status");
    region.setAttribute("aria-live", "polite");
    document.body.appendChild(region);
  }
  return region;
}

const reducedMotion = () =>
  typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;

/**
 * 弹一条 toast。
 *
 * @param {string|Node} message
 * @param {object} options
 * @param {"ok"|"err"|"warn"|"info"} options.tone  缺省 "info"
 * @param {number} options.timeout  毫秒，缺省 3000；0 = 不自动消失
 * @returns {{ el: HTMLElement, close(): void }}
 */
export function toast(message, { tone = "info", timeout = 3000 } = {}) {
  const host = ensureRegion();
  const t = TONES.includes(tone) ? tone : "info";

  const el = document.createElement("div");
  el.className = `toast ${t}`;
  if (t === "err") el.setAttribute("role", "alert");
  const icon = document.createElement("span");
  icon.className = "toast-icon";
  icon.setAttribute("aria-hidden", "true");
  icon.textContent = ICONS[t];
  const text = document.createElement("span");
  text.className = "toast-text";
  if (message instanceof Node) text.append(message);
  else text.textContent = String(message ?? "");
  el.append(icon, text);

  let timer = null;
  let closed = false;
  function close() {
    if (closed) return;
    closed = true;
    clearTimeout(timer);
    if (reducedMotion()) {
      el.remove();
      return;
    }
    el.classList.add("leaving");
    setTimeout(() => el.remove(), LEAVE_MS);
  }

  el.addEventListener("click", close);
  host.appendChild(el);
  // 超出上限：最旧的（还没在退场的）先走
  const live = [...host.querySelectorAll(".toast:not(.leaving)")];
  for (const old of live.slice(0, Math.max(0, live.length - MAX))) old.click();

  if (timeout > 0) timer = setTimeout(close, timeout);
  return { el, close };
}

/** 关掉所有 toast（如切页时） */
export function clearToasts() {
  if (!region?.isConnected) return;
  for (const el of region.querySelectorAll(".toast:not(.leaving)")) el.click();
}
