/* ============================================================
   @szyyw/design · dotfield.js
   交互式点阵背景 —— jppost 版（sparkle / 光晕跟随）与
   finance-ledger 版（颜色量化缓存 / 30fps / 可见性暂停 /
   宽度变化才重建）的合并实现。ESM，无依赖。

   颜色从 CSS 变量取（--df-from / --df-to / --df-glow），
   data-theme / data-scheme / 系统明暗变化时自动换色——
   主题切换无需重建实例。
   ============================================================ */

const TWO_PI = Math.PI * 2;

const DEFAULTS = {
  dotRadius: 1.6,
  dotSpacing: 16,
  cursorRadius: 420,
  cursorForce: 0.12,
  waveAmplitude: 2.5,
  sparkle: true,
  glow: true,
  glowRadius: 180,
  fps: 30
};

/** 解析 "#rrggbb" / "rgb()" / "rgba()" 为 {r,g,b,a}；解析失败返回 null */
function parseColor(text) {
  const s = (text || "").trim();
  let m = s.match(/^#([0-9a-f]{6})$/i);
  if (m) {
    const n = parseInt(m[1], 16);
    return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255, a: 1 };
  }
  m = s.match(/^rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)$/i);
  if (m) return { r: +m[1], g: +m[2], b: +m[3], a: m[4] === undefined ? 1 : +m[4] };
  return null;
}

function readThemeColors() {
  const cs = getComputedStyle(document.documentElement);
  const num = (name, fallback) => {
    const v = parseFloat(cs.getPropertyValue(name));
    return Number.isFinite(v) ? v : fallback;
  };
  return {
    from: parseColor(cs.getPropertyValue("--df-from")) ?? { r: 56, g: 189, b: 248, a: 0.5 },
    to: parseColor(cs.getPropertyValue("--df-to")) ?? { r: 168, g: 85, b: 247, a: 0.4 },
    glow: (cs.getPropertyValue("--df-glow") || "#0b1020").trim(),
    // 主题级效果旋钮：主题可以只用 CSS 重配背景行为
    sparkle: num("--df-sparkle", 1) > 0,
    wave: num("--df-wave", 2.5)
  };
}

/**
 * 挂载点阵背景到容器（容器需 position:fixed/absolute 且铺满，如 .bg-layer）。
 * 返回 { destroy, refreshColors }。
 */
export function mountDotField(container, options = {}) {
  const props = { ...DEFAULTS, ...options };
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const finePointer = window.matchMedia("(pointer: fine)").matches;

  const canvas = document.createElement("canvas");
  // translateZ(0) 强制独立合成层，滚动时不参与页面重绘
  canvas.style.cssText = "position:absolute;inset:0;width:100%;height:100%;transform:translateZ(0);";
  container.appendChild(canvas);
  const ctx = canvas.getContext("2d");

  // 光晕：SVG 径向渐变（底色同色的“暗斑”），跟随鼠标、按互动强度淡入
  let glowEl = null;
  let glowStop = null;
  let glowStopEnd = null;
  let svg = null;
  if (props.glow && finePointer && !reducedMotion) {
    const NS = "http://www.w3.org/2000/svg";
    svg = document.createElementNS(NS, "svg");
    svg.style.cssText = "position:absolute;inset:0;width:100%;height:100%;pointer-events:none;";
    const defs = document.createElementNS(NS, "defs");
    const gradId = "df-glow-" + Math.random().toString(36).slice(2, 9);
    const grad = document.createElementNS(NS, "radialGradient");
    grad.setAttribute("id", gradId);
    glowStop = document.createElementNS(NS, "stop");
    glowStop.setAttribute("offset", "0%");
    glowStopEnd = document.createElementNS(NS, "stop");
    glowStopEnd.setAttribute("offset", "100%");
    // 终点必须是「同色 + 透明度 0」——transparent 关键字是透明黑，
    // 插值中段会出现半透明灰，浅色模式下成一圈灰晕
    glowStopEnd.setAttribute("stop-opacity", "0");
    grad.appendChild(glowStop);
    grad.appendChild(glowStopEnd);
    defs.appendChild(grad);
    svg.appendChild(defs);
    glowEl = document.createElementNS(NS, "circle");
    glowEl.setAttribute("cx", "-9999");
    glowEl.setAttribute("cy", "-9999");
    glowEl.setAttribute("r", String(props.glowRadius));
    glowEl.setAttribute("fill", `url(#${gradId})`);
    glowEl.style.opacity = "0";
    glowEl.style.willChange = "opacity";
    svg.appendChild(glowEl);
    container.appendChild(svg);
  }

  let width = 0;
  let height = 0;
  let dots = [];
  let raf = 0;
  let running = true;
  let destroyed = false;
  const mouse = { x: -9999, y: -9999, prevX: -9999, prevY: -9999, speed: 0 };
  let engagement = 0;
  let glowOpacity = 0;

  // 颜色量化缓存：渐变 12 档 × 透明度 8 档，避免每帧上万次字符串分配
  const GRAD_STEPS = 12;
  const ALPHA_STEPS = 8;
  let colorCache = [];

  function rebuildColors() {
    const { from, to, glow, sparkle, wave } = readThemeColors();
    if (options.sparkle === undefined) props.sparkle = sparkle;
    if (options.waveAmplitude === undefined) props.waveAmplitude = wave;
    colorCache = [];
    for (let gi = 0; gi <= GRAD_STEPS; gi++) {
      const t = gi / GRAD_STEPS;
      const r = Math.round(from.r + (to.r - from.r) * t);
      const g = Math.round(from.g + (to.g - from.g) * t);
      const b = Math.round(from.b + (to.b - from.b) * t);
      const baseA = from.a + (to.a - from.a) * t;
      for (let ai = 0; ai <= ALPHA_STEPS; ai++) {
        colorCache[gi * (ALPHA_STEPS + 1) + ai] = `rgba(${r},${g},${b},${(baseA * ai) / ALPHA_STEPS})`;
      }
    }
    if (glowStop) glowStop.setAttribute("stop-color", glow);
    if (glowStopEnd) glowStopEnd.setAttribute("stop-color", glow);
    if (reducedMotion) drawStatic();
  }

  function rebuild() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    width = container.clientWidth;
    height = container.clientHeight;
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    dots = [];
    // 小屏加大间距，减少手机端渲染负担
    const spacing = width < 768 ? props.dotSpacing * 1.5 : props.dotSpacing;
    for (let y = spacing / 2; y < height; y += spacing) {
      for (let x = spacing / 2; x < width; x += spacing) {
        dots.push({ x, y, ox: x, oy: y, tw: Math.random() * TWO_PI });
      }
    }
    if (reducedMotion) drawStatic();
  }

  /** 减少动效偏好：画一帧静态点阵，不进动画循环 */
  function drawStatic() {
    ctx.clearRect(0, 0, width, height);
    const invW = 1 / Math.max(1, width);
    const invH = 1 / Math.max(1, height);
    for (const d of dots) {
      const gi = Math.min(GRAD_STEPS, Math.max(0, Math.round(((d.ox * invW + d.oy * invH) / 2) * GRAD_STEPS)));
      ctx.fillStyle = colorCache[gi * (ALPHA_STEPS + 1) + Math.round(ALPHA_STEPS * 0.7)];
      ctx.beginPath();
      ctx.arc(d.ox, d.oy, props.dotRadius, 0, TWO_PI);
      ctx.fill();
    }
  }

  // 只有宽度变化才重建点阵；纯高度抖动（软键盘/工具栏）只在超阈值时重建
  let lastWidth = 0;
  let lastHeight = 0;
  function onResize() {
    const w = container.clientWidth;
    const h = container.clientHeight;
    if (w !== lastWidth || Math.abs(h - lastHeight) > 160) {
      lastWidth = w;
      lastHeight = h;
      rebuild();
    }
  }

  let tick = 0;
  let lastFrame = 0;
  let frameCount = 0;
  const FRAME_INTERVAL = 1000 / props.fps;

  function frame(now) {
    if (!running || destroyed) return;
    raf = requestAnimationFrame(frame);
    if (now - lastFrame < FRAME_INTERVAL) return;
    // 真实时间驱动：rAF 被滚动节流后相位平滑续接，不会跳变闪烁
    const dt = Math.min(now - lastFrame, 100);
    lastFrame = now;
    tick += dt / 1000;
    frameCount++;

    // 鼠标速度 → 互动强度 → 光晕透明度（jppost 的 engagement 模型）
    const mdx = mouse.prevX - mouse.x;
    const mdy = mouse.prevY - mouse.y;
    mouse.speed += (Math.hypot(mdx, mdy) - mouse.speed) * 0.4;
    if (mouse.speed < 0.001) mouse.speed = 0;
    mouse.prevX = mouse.x;
    mouse.prevY = mouse.y;
    engagement += (Math.min(mouse.speed / 5, 1) - engagement) * 0.06;
    if (glowEl) {
      glowOpacity += (engagement - glowOpacity) * 0.08;
      glowEl.setAttribute("cx", String(mouse.x));
      glowEl.setAttribute("cy", String(mouse.y));
      glowEl.style.opacity = String(glowOpacity);
    }

    ctx.clearRect(0, 0, width, height);
    const invW = 1 / Math.max(1, width);
    const invH = 1 / Math.max(1, height);
    for (let i = 0; i < dots.length; i++) {
      const d = dots[i];
      const wave = Math.sin(tick * 1.4 + d.ox * 0.02) * props.waveAmplitude;
      let tx = d.ox;
      let ty = d.oy + wave;
      const dx = d.ox - mouse.x;
      const dy = d.oy - mouse.y;
      const dist = Math.hypot(dx, dy);
      if (dist < props.cursorRadius && dist > 0.01) {
        const force = (1 - dist / props.cursorRadius) * props.cursorForce * 60;
        tx += (dx / dist) * force;
        ty += (dy / dist) * force;
      }
      d.x += (tx - d.x) * 0.12;
      d.y += (ty - d.y) * 0.12;
      const twinkle = 0.55 + 0.45 * Math.sin(tick * 2 + d.tw);
      const gi = Math.min(GRAD_STEPS, Math.max(0, Math.round(((d.x * invW + d.y * invH) / 2) * GRAD_STEPS)));
      const ai = Math.min(ALPHA_STEPS, Math.max(0, Math.round(twinkle * ALPHA_STEPS)));
      ctx.fillStyle = colorCache[gi * (ALPHA_STEPS + 1) + ai];
      // sparkle：伪随机挑 ~3% 的点短暂放大，jppost 的星闪效果
      let r = props.dotRadius;
      if (props.sparkle) {
        const hash = ((i * 2654435761) ^ (frameCount >> 3)) >>> 0;
        if (hash % 100 < 3) r = props.dotRadius * 1.8;
      }
      ctx.beginPath();
      ctx.arc(d.x, d.y, r, 0, TWO_PI);
      ctx.fill();
    }
  }

  function onMove(e) {
    mouse.x = e.clientX;
    mouse.y = e.clientY;
  }
  function onLeave() {
    mouse.x = -9999;
    mouse.y = -9999;
  }
  function onVisibility() {
    running = document.visibilityState === "visible";
    if (destroyed) return;
    if (running && !reducedMotion) {
      lastFrame = performance.now();
      raf = requestAnimationFrame(frame);
    } else {
      cancelAnimationFrame(raf);
    }
  }

  // 主题/明暗切换 → 换色；auto 模式下系统切换也要响应
  const attrObserver = new MutationObserver(rebuildColors);
  attrObserver.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme", "data-palette", "data-scheme"] });
  const mql = window.matchMedia("(prefers-color-scheme: dark)");
  mql.addEventListener("change", rebuildColors);

  rebuildColors();
  rebuild();
  lastWidth = container.clientWidth;
  lastHeight = container.clientHeight;
  if (!reducedMotion) raf = requestAnimationFrame(frame);
  window.addEventListener("resize", onResize);
  if (finePointer && !reducedMotion) {
    window.addEventListener("pointermove", onMove, { passive: true });
    window.addEventListener("pointerleave", onLeave);
  }
  document.addEventListener("visibilitychange", onVisibility);

  return {
    refreshColors: rebuildColors,
    destroy() {
      destroyed = true;
      running = false;
      cancelAnimationFrame(raf);
      attrObserver.disconnect();
      mql.removeEventListener("change", rebuildColors);
      window.removeEventListener("resize", onResize);
      if (finePointer && !reducedMotion) {
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerleave", onLeave);
      }
      document.removeEventListener("visibilitychange", onVisibility);
      canvas.remove();
      if (svg) svg.remove();
    }
  };
}

/**
 * .spot 光斑跟踪：把指针位置写进元素的 --mx/--my。
 * 事件委托一次挂载即可覆盖动态增删的元素。返回解绑函数。
 */
export function attachSpot(root = document) {
  if (!window.matchMedia("(pointer: fine)").matches) return () => {};
  function onMove(e) {
    const target = e.target instanceof Element ? e.target.closest(".spot") : null;
    if (!target) return;
    const rect = target.getBoundingClientRect();
    target.style.setProperty("--mx", `${(((e.clientX - rect.left) / rect.width) * 100).toFixed(2)}%`);
    target.style.setProperty("--my", `${(((e.clientY - rect.top) / rect.height) * 100).toFixed(2)}%`);
  }
  root.addEventListener("pointermove", onMove, { passive: true });
  return () => root.removeEventListener("pointermove", onMove);
}
