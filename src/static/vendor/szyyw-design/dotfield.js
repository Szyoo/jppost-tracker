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

export const DEFAULTS = {
  dotRadius: 1.6,
  dotSpacing: 16,
  cursorRadius: 420,
  cursorForce: 0.12,
  /** 指针模型：false=点被推开回弹（常驻斥力）；true=鼠标压出凹陷（随移动涨落） */
  bulgeOnly: false,
  bulgeStrength: 40,
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

/* 颜色 token 要「解析后」的值，不能直读自定义属性：
   自定义属性不做条件求值，getPropertyValue("--df-from") 拿到的是未展开的
   light-dark(...) 字面量，parseColor 必然失败、静默退回兜底色——
   于是浅色模式与 aurora 配色根本没生效过。
   把它套到真实的 color 属性上，浏览器才会算出 rgb()。
   探针放在一个哨兵色容器里：var() 无效时声明会退化成继承，
   继承到哨兵色即说明取值失败，可以跟真实颜色区分开。 */
const PROBE_SENTINEL = "rgb(1, 2, 3)";
let probe = null;

export function resolveTokenColor(name) {
  if (!probe?.isConnected) {
    const host = document.createElement("span");
    host.style.cssText =
      "position:absolute;width:0;height:0;overflow:hidden;visibility:hidden;pointer-events:none;color:" +
      PROBE_SENTINEL;
    probe = document.createElement("span");
    host.appendChild(probe);
    document.documentElement.appendChild(host);
  }
  probe.style.color = `var(${name})`;
  const value = getComputedStyle(probe).color;
  return value === PROBE_SENTINEL ? null : value;
}

function readThemeColors() {
  const cs = getComputedStyle(document.documentElement);
  const num = (name, fallback) => {
    const v = parseFloat(cs.getPropertyValue(name));
    return Number.isFinite(v) ? v : fallback;
  };
  return {
    from: parseColor(resolveTokenColor("--df-from")) ?? { r: 56, g: 189, b: 248, a: 0.5 },
    to: parseColor(resolveTokenColor("--df-to")) ?? { r: 168, g: 85, b: 247, a: 0.4 },
    glow: resolveTokenColor("--df-glow") ?? "#0b1020",
    // 主题级效果旋钮：主题可以只用 CSS 重配背景行为
    sparkle: num("--df-sparkle", 1) > 0,
    wave: num("--df-wave", 2.5)
  };
}

/**
 * 挂载点阵背景到容器（容器需 position:fixed/absolute 且铺满，如 .bg-layer）。
 * 返回 { destroy, refreshColors, setOptions, getOptions, resetOptions }。
 */
export function mountDotField(container, options = {}) {
  const props = { ...DEFAULTS, ...options };
  // 构造时传进来的取值是这个实例的基线，恢复默认回到它而不是包缺省
  const baseline = { ...props };
  /* 被显式指定过的参数不再跟随主题旋钮（--df-sparkle / --df-wave）——
     「手动调过就归你管」，恢复默认才交还给主题 */
  const explicit = new Set(Object.keys(options));
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
    if (!explicit.has("sparkle")) props.sparkle = sparkle;
    if (!explicit.has("waveAmplitude")) props.waveAmplitude = wave;
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
  let frameInterval = 1000 / props.fps;

  function frame(now) {
    if (!running || destroyed) return;
    raf = requestAnimationFrame(frame);
    if (now - lastFrame < frameInterval) return;
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
      let follow = 0.12;
      if (dist < props.cursorRadius && dist > 0.01) {
        let force;
        if (props.bulgeOnly) {
          // 凹陷模式：位移随互动强度涨落，鼠标一停就回填。
          // 二次衰减让坑边缘平滑，不会在半径处出现硬边
          const falloff = 1 - dist / props.cursorRadius;
          force = falloff * falloff * props.bulgeStrength * engagement;
          follow = 0.15;
        } else {
          force = (1 - dist / props.cursorRadius) * props.cursorForce * 60;
        }
        tx += (dx / dist) * force;
        ty += (dy / dist) * force;
      }
      d.x += (tx - d.x) * follow;
      d.y += (ty - d.y) * follow;
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

  /**
   * 改参数并立即生效（设置面板用）。只认 DEFAULTS 里有的键。
   * 颜色不走这里——颜色是 token，改 --df-from/--df-to/--df-glow 即可，
   * 下面的 observer 会接住。
   */
  function setOptions(patch = {}) {
    let regrid = false;
    for (const [key, value] of Object.entries(patch)) {
      if (!(key in DEFAULTS) || props[key] === value) continue;
      props[key] = value;
      explicit.add(key);
      if (key === "dotSpacing") regrid = true;
      if (key === "fps") frameInterval = 1000 / props.fps;
      if (key === "glowRadius" && glowEl) glowEl.setAttribute("r", String(props.glowRadius));
    }
    if (regrid) rebuild();
    else if (reducedMotion) drawStatic();
    return { ...props };
  }

  /** 回到挂载时的参数，并把主题旋钮的控制权交还给主题 */
  function resetOptions() {
    Object.assign(props, baseline);
    explicit.clear();
    for (const key of Object.keys(options)) explicit.add(key);
    frameInterval = 1000 / props.fps;
    if (glowEl) glowEl.setAttribute("r", String(props.glowRadius));
    rebuildColors();
    rebuild();
    return { ...props };
  }

  // 主题/明暗切换 → 换色；auto 模式下系统切换也要响应。
  // style 也听：设置面板把颜色写成 <html> 上的行内 token
  const attrObserver = new MutationObserver(rebuildColors);
  attrObserver.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["data-theme", "data-palette", "data-scheme", "style"]
  });
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
    setOptions,
    resetOptions,
    getOptions: () => ({ ...props }),
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
