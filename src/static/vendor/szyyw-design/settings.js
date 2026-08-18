/* ============================================================
   @szyyw/design · settings.js
   背景参数面板 —— 右上角齿轮按钮 + 右侧抽屉，实时调点阵背景。

   参数分两路，面板里看不出区别，但底下各归各家：
     行为参数（点大小/间距/指针模型/波浪…）→ field.setOptions()
     颜色    → 写 <html> 行内 token（--df-from/--df-to/--df-glow），
               DotField 的 observer 接住换色。颜色永远是 token，
               不从组件层硬塞进画布。
   ============================================================ */

import { mountCornerTool, CORNER_ORDER } from "./corner.js";
import { DEFAULTS, resolveTokenColor } from "./dotfield.js";
import { VERSION, REPO } from "./version.js";

const SLIDERS = [
  { key: "dotRadius", label: "点大小", min: 0.5, max: 4, step: 0.1 },
  { key: "dotSpacing", label: "点间距", min: 6, max: 40, step: 1 },
  { key: "cursorRadius", label: "鼠标影响半径", min: 100, max: 800, step: 10 },
  { key: "cursorForce", label: "扰动力度", min: 0.02, max: 0.3, step: 0.01, hideWhenBulge: true },
  { key: "bulgeStrength", label: "凹陷强度", min: 10, max: 120, step: 1, onlyWhenBulge: true },
  { key: "glowRadius", label: "鼠标光晕半径", min: 60, max: 400, step: 10 },
  { key: "waveAmplitude", label: "波浪幅度", min: 0, max: 8, step: 0.5 }
];

const TOGGLES = [
  { key: "bulgeOnly", label: "凹陷模式", hint: "开=鼠标压出凹陷；关=点被推开回弹" },
  { key: "sparkle", label: "随机闪烁" }
];

/** 颜色项与 token 的对应；alpha=false 的项没有透明度滑杆 */
const COLORS = [
  { key: "gradientFrom", varName: "--df-from", label: "点阵渐变 A", alpha: true },
  { key: "gradientTo", varName: "--df-to", label: "点阵渐变 B", alpha: true },
  { key: "glowColor", varName: "--df-glow", label: "光晕颜色", alpha: false }
];

const COLOR_KEYS = COLORS.map((c) => c.key);

/** "rgba(56, 189, 248, 0.5)" | "#0b1020" → { hex, alpha } */
function splitColor(text) {
  const m = /^rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)$/.exec(text || "");
  if (m) {
    const hex = "#" + [m[1], m[2], m[3]].map((n) => Math.round(Number(n)).toString(16).padStart(2, "0")).join("");
    return { hex, alpha: m[4] === undefined ? 1 : Number(m[4]) };
  }
  return { hex: /^#[0-9a-fA-F]{6}$/.test(text || "") ? text : "#000000", alpha: 1 };
}

function joinColor(hex, alpha, withAlpha) {
  if (!withAlpha) return hex;
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/* ---------- 版本检测 ---------- */

const UPDATE_CACHE_KEY = "szyyw:design-update";

function parseVer(text) {
  const m = /^v?(\d+)\.(\d+)\.(\d+)$/.exec((text || "").trim());
  return m ? [+m[1], +m[2], +m[3]] : null;
}

function newerThan(a, b) {
  for (let i = 0; i < 3; i++) {
    if (a[i] !== b[i]) return a[i] > b[i];
  }
  return false;
}

function toResult(latestTag, repo) {
  const cur = parseVer(VERSION);
  const lat = parseVer(latestTag);
  return {
    current: VERSION,
    latest: latestTag.replace(/^v/, ""),
    hasUpdate: !!(cur && lat && newerThan(lat, cur)),
    compareUrl: `https://github.com/${repo}/compare/v${VERSION}...${latestTag}`
  };
}

/**
 * 查上游最新 tag 并与当前 VERSION 比对。
 * 匿名走 GitHub API（限流 60 次/时/IP），结果按 cacheHours 缓存在 localStorage，
 * force 才绕过缓存。tags 接口不保证顺序，自己按 semver 挑最大。
 */
export async function checkDesignUpdate({
  repo = REPO,
  cacheHours = 6,
  force = false,
  cacheKey = UPDATE_CACHE_KEY
} = {}) {
  if (!force) {
    try {
      const c = JSON.parse(localStorage.getItem(cacheKey) || "null");
      if (c?.repo === repo && c.latest && Date.now() - c.at < cacheHours * 3600e3) {
        return toResult(c.latest, repo);
      }
    } catch {
      // 坏缓存当没有
    }
  }
  const res = await fetch(`https://api.github.com/repos/${repo}/tags?per_page=100`, {
    headers: { Accept: "application/vnd.github+json" }
  });
  if (!res.ok) throw new Error(`GitHub API ${res.status}`);
  const tags = await res.json();
  let latest = null;
  for (const t of tags) {
    const ver = parseVer(t?.name);
    if (ver && (!latest || newerThan(ver, latest.ver))) latest = { ver, name: t.name };
  }
  if (!latest) throw new Error("no semver tags");
  try {
    localStorage.setItem(cacheKey, JSON.stringify({ at: Date.now(), repo, latest: latest.name }));
  } catch {
    // 存不了就每次现查
  }
  return toResult(latest.name, repo);
}

/** 滑杆右侧的读数：按 step 定小数位，免得 0.30000000000000004 */
function formatValue(value, step) {
  const decimals = (String(step).split(".")[1] || "").length;
  return decimals ? Number(value).toFixed(decimals) : String(Math.round(value));
}

function readStored(storageKey) {
  try {
    const raw = localStorage.getItem(storageKey);
    return raw ? JSON.parse(raw) : null;
  } catch {
    // 隐私模式 / 坏 JSON——当没存过，用缺省
    return null;
  }
}

function applyColors(values, only = null) {
  for (const c of COLORS) {
    if (only && !only.has(c.key)) continue;
    if (values[c.key]) document.documentElement.style.setProperty(c.varName, values[c.key]);
  }
}

function clearColors() {
  for (const c of COLORS) document.documentElement.style.removeProperty(c.varName);
}

/**
 * 取回上次存的背景参数：颜色立刻写回 token（避免首帧用缺省色再跳一下），
 * 行为参数以对象返回，直接喂给 mountDotField。
 *
 *   const field = mountDotField(bg, restoreDotFieldSettings());
 */
export function restoreDotFieldSettings({ storageKey = "szyyw:dotfield" } = {}) {
  const stored = readStored(storageKey);
  if (!stored) return {};
  applyColors(stored);
  const patch = {};
  for (const key of Object.keys(DEFAULTS)) {
    if (stored[key] !== undefined) patch[key] = stored[key];
  }
  return patch;
}

/**
 * 挂载背景参数面板。齿轮按钮进右上角工具位，排在明暗切换右边。
 *
 * @param field      mountDotField() 的返回值（必需）
 * @param onSave     传了才显示「保存」按钮（存服务端用）；异步，抛错即算失败
 * @param note       没有 onSave 时显示在页脚的说明，如「访客模式 · 仅本地预览」
 * @param persist    "localStorage"（缺省，改完即存）| "none"
 * @param update     版本检测。false 关闭；{ onUpdate } 接了服务端更新端点才是真·一键更新，
 *                   没接则退化为「复制升级命令」（浏览器改不了服务器上的依赖）
 */
export function mountDotFieldSettings({
  field,
  title = "背景参数",
  persist = "localStorage",
  storageKey = "szyyw:dotfield",
  order = CORNER_ORDER.settings,
  onSave = null,
  note = "",
  update = {},
  labels = {}
} = {}) {
  if (!field?.setOptions) throw new Error("mountDotFieldSettings 需要 mountDotField() 返回的实例");

  const text = {
    open: "背景参数",
    close: "关闭",
    reset: "恢复默认",
    save: "保存",
    saving: "保存中…",
    saved: "已保存 ✓",
    error: "失败，重试",
    version: "版本",
    check: "检查更新",
    checking: "检查中…",
    upToDate: "已是最新 ✓",
    updateAvailable: "有新版",
    viewChanges: "查看变更",
    copyCommand: "复制升级命令",
    copied: "已复制 ✓",
    updateNow: "更新到",
    updating: "更新中…",
    updated: "已更新 ✓",
    updateFailed: "更新失败，重试",
    checkFailed: "检查失败（网络或限流）",
    ...labels
  };

  // 面板状态 = 画布当前参数 + 当前解析出来的颜色
  const values = { ...field.getOptions() };
  for (const c of COLORS) values[c.key] = resolveTokenColor(c.varName) ?? "#000000";

  /* ---------- 骨架 ---------- */

  const btn = el("button", "corner-tool settings-toggle");
  btn.type = "button";
  btn.title = text.open;
  btn.setAttribute("aria-label", text.open);
  btn.innerHTML =
    '<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<circle cx="12" cy="12" r="3"></circle>' +
    '<path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h.01a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51h.01a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v.01a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"></path>' +
    "</svg>";

  const backdrop = el("div", "panel-backdrop");
  const panel = el("aside", "glass settings-panel");
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-label", title);

  const head = el("div", "panel-head");
  head.append(el("h2", "panel-title", title));
  const closeBtn = el("button", "close-x", "✕");
  closeBtn.type = "button";
  closeBtn.title = text.close;
  closeBtn.setAttribute("aria-label", text.close);
  head.append(closeBtn);

  const body = el("div", "panel-body");
  const foot = el("div", "panel-foot");
  panel.append(head, body, foot);

  /* ---------- 控件 ---------- */

  const rerenders = [];
  /* 只有被真正动过的参数才落盘。面板一开就把当前解析色填进 values（色块要显示
     当前颜色），但那是「主题算出来的值」不是「用户的选择」——整包存下去会把
     没碰过的颜色钉死成当时那套明暗，sparkle/波浪也会脱离主题旋钮。
     刷新后要把上次存过的键认回来，否则这次只改一项就把上次的覆盖没了。 */
  const touched = new Set(persist === "localStorage" ? Object.keys(readStored(storageKey) ?? {}) : []);

  function commit(key, value) {
    values[key] = value;
    touched.add(key);
    if (COLOR_KEYS.includes(key)) applyColors(values, touched);
    else field.setOptions({ [key]: value });
    save();
    for (const fn of rerenders) fn();
  }

  for (const t of TOGGLES) {
    const row = el("label", "ctl ctl-toggle");
    const textCol = el("div");
    textCol.append(el("span", "ctl-label", t.label));
    if (t.hint) textCol.append(el("span", "ctl-hint", t.hint));
    const input = el("input", "switch");
    input.type = "checkbox";
    input.checked = !!values[t.key];
    input.addEventListener("change", () => commit(t.key, input.checked));
    row.append(textCol, input);
    body.append(row);
    rerenders.push(() => {
      input.checked = !!values[t.key];
    });
  }

  for (const s of SLIDERS) {
    const row = el("label", "ctl");
    const line = el("div", "ctl-row");
    const value = el("span", "ctl-value num");
    line.append(el("span", "ctl-label", s.label), value);
    const input = el("input");
    input.type = "range";
    input.min = s.min;
    input.max = s.max;
    input.step = s.step;
    input.addEventListener("input", () => commit(s.key, Number(input.value)));
    row.append(line, input);
    body.append(row);
    rerenders.push(() => {
      // 两种指针模型各有各的旋钮，另一半藏起来免得调了没反应
      const hidden = (s.onlyWhenBulge && !values.bulgeOnly) || (s.hideWhenBulge && values.bulgeOnly);
      row.hidden = hidden;
      if (hidden) return;
      input.value = values[s.key];
      value.textContent = formatValue(values[s.key], s.step);
    });
  }

  for (const c of COLORS) {
    const row = el("label", "ctl");
    const line = el("div", "ctl-row");
    const swatch = el("input");
    swatch.type = "color";
    line.append(el("span", "ctl-label", c.label), swatch);
    row.append(line);

    let alphaInput = null;
    if (c.alpha) {
      alphaInput = el("input");
      alphaInput.type = "range";
      alphaInput.min = 0.05;
      alphaInput.max = 1;
      alphaInput.step = 0.05;
      alphaInput.addEventListener("input", () =>
        commit(c.key, joinColor(splitColor(values[c.key]).hex, Number(alphaInput.value), true))
      );
      row.append(alphaInput);
    }

    swatch.addEventListener("input", () =>
      commit(c.key, joinColor(swatch.value, splitColor(values[c.key]).alpha, c.alpha))
    );

    body.append(row);
    rerenders.push(() => {
      const parsed = splitColor(values[c.key]);
      swatch.value = parsed.hex;
      if (alphaInput) alphaInput.value = parsed.alpha;
    });
  }

  /* ---------- 版本与更新 ---------- */

  let runCheck = null;

  if (update !== false) {
    const cfg = {
      repo: REPO,
      cacheHours: 6,
      /** 复制给用户的升级命令；vendored 项目传自己的 cp 流程 */
      command: (v) => `npm i github:${cfg.repo}#v${v}`,
      /** 接了才显示真按钮：由消费方的服务端完成更新（如 portal 的 admin 端点） */
      onUpdate: null,
      ...update
    };

    body.append(el("hr", "divider"));
    const section = el("div", "ctl");
    const line = el("div", "ctl-row");
    const label = el("span", "ctl-label");
    label.append(text.version + " ", el("span", "num", "v" + VERSION));
    const checkBtn = el("button", "btn btn-ghost btn-small", text.check);
    checkBtn.type = "button";
    line.append(label, checkBtn);
    const status = el("span", "ctl-hint");
    const actions = el("div", "update-actions");
    actions.hidden = true;
    section.append(line, status, actions);
    body.append(section);

    const renderResult = (r) => {
      btn.dataset.update = r.hasUpdate ? "1" : "";
      actions.hidden = !r.hasUpdate;
      actions.textContent = "";
      status.textContent = "";
      if (!r.hasUpdate) {
        status.textContent = text.upToDate;
        return;
      }
      status.append(`${text.updateAvailable} v${r.latest} · `);
      const link = el("a", "update-link", text.viewChanges);
      link.href = r.compareUrl;
      link.target = "_blank";
      link.rel = "noreferrer";
      status.append(link);

      if (cfg.onUpdate) {
        const upBtn = el("button", "btn btn-small", `${text.updateNow} v${r.latest}`);
        upBtn.type = "button";
        upBtn.addEventListener("click", async () => {
          upBtn.disabled = true;
          upBtn.textContent = text.updating;
          try {
            await cfg.onUpdate(r);
            // 成功后不复位——更新是部署动作，等消费方刷新页面收尾
            upBtn.textContent = text.updated;
          } catch {
            upBtn.disabled = false;
            upBtn.textContent = text.updateFailed;
          }
        });
        actions.append(upBtn);
      } else {
        const copyBtn = el("button", "btn btn-small", text.copyCommand);
        copyBtn.type = "button";
        copyBtn.addEventListener("click", async () => {
          const cmd = cfg.command(r.latest);
          try {
            await navigator.clipboard.writeText(cmd);
            copyBtn.textContent = text.copied;
          } catch {
            // 剪贴板被拒（非安全上下文/无手势）——退化成弹窗手动复制
            window.prompt(text.copyCommand, cmd);
            copyBtn.textContent = text.copyCommand;
            return;
          }
          setTimeout(() => {
            copyBtn.textContent = text.copyCommand;
          }, 1600);
        });
        actions.append(copyBtn);
      }
    };

    runCheck = async (force) => {
      checkBtn.disabled = true;
      status.textContent = text.checking;
      try {
        renderResult(await checkDesignUpdate({ repo: cfg.repo, cacheHours: cfg.cacheHours, force }));
      } catch {
        status.textContent = text.checkFailed;
      }
      checkBtn.disabled = false;
    };

    checkBtn.addEventListener("click", () => runCheck(true));
    // 挂载后静默查一次（走缓存，至多 6h 一次网络请求），有新版就点亮齿轮角标
    setTimeout(() => runCheck(false), 800);
  }

  /* ---------- 页脚 ---------- */

  const resetBtn = el("button", "btn btn-ghost", text.reset);
  resetBtn.type = "button";
  resetBtn.addEventListener("click", () => {
    clearColors();
    touched.clear();
    const restored = field.resetOptions();
    Object.assign(values, restored);
    for (const c of COLORS) values[c.key] = resolveTokenColor(c.varName) ?? "#000000";
    save();
    for (const fn of rerenders) fn();
  });
  foot.append(resetBtn);

  let saveBtn = null;
  if (onSave) {
    saveBtn = el("button", "btn", text.save);
    saveBtn.type = "button";
    saveBtn.addEventListener("click", async () => {
      saveBtn.disabled = true;
      saveBtn.textContent = text.saving;
      let ok = true;
      try {
        // 跟本地存的是同一份「改动过的键」，服务端取回来喂 restore 才对得上
        await onSave(snapshot());
      } catch {
        ok = false;
      }
      saveBtn.textContent = ok ? text.saved : text.error;
      setTimeout(() => {
        saveBtn.disabled = false;
        saveBtn.textContent = text.save;
      }, 1600);
    });
    foot.append(saveBtn);
  } else if (note) {
    foot.append(el("span", "guest-note", note));
  }

  /** 只含被改动过的键——没碰过的继续跟主题走 */
  function snapshot() {
    const payload = {};
    for (const key of touched) payload[key] = values[key];
    return payload;
  }

  function save() {
    if (persist !== "localStorage") return;
    try {
      localStorage.setItem(storageKey, JSON.stringify(snapshot()));
    } catch {
      // 隐私模式下存不了——面板照常用，只是刷新后回到缺省
    }
  }

  /* ---------- 开合 ---------- */

  let open = false;
  let scrollLock = "";

  function setOpen(next) {
    if (next === open) return;
    open = next;
    panel.classList.toggle("open", open);
    backdrop.classList.toggle("show", open);
    btn.setAttribute("aria-expanded", String(open));
    // 关着时别让抽屉里的控件还能被 Tab 走到
    if (open) panel.removeAttribute("inert");
    else panel.setAttribute("inert", "");
    if (open) {
      scrollLock = document.body.style.overflow;
      document.body.style.overflow = "hidden";
      for (const fn of rerenders) fn();
      closeBtn.focus();
    } else {
      document.body.style.overflow = scrollLock;
    }
  }

  function onKey(e) {
    if (e.key === "Escape" && open) setOpen(false);
  }

  btn.addEventListener("click", () => setOpen(!open));
  closeBtn.addEventListener("click", () => setOpen(false));
  backdrop.addEventListener("click", () => setOpen(false));
  document.addEventListener("keydown", onKey);

  panel.setAttribute("inert", "");
  for (const fn of rerenders) fn();
  document.body.append(backdrop, panel);
  const unmount = mountCornerTool(btn, { order });

  return {
    open: () => setOpen(true),
    close: () => setOpen(false),
    /** 手动触发一次强制检查（绕过缓存） */
    checkUpdate: () => runCheck?.(true),
    /** 外部改了参数后让面板跟上（比如从服务端拉到设置） */
    sync() {
      Object.assign(values, field.getOptions());
      for (const c of COLORS) values[c.key] = resolveTokenColor(c.varName) ?? "#000000";
      for (const fn of rerenders) fn();
    },
    destroy() {
      document.removeEventListener("keydown", onKey);
      if (open) document.body.style.overflow = scrollLock;
      unmount();
      backdrop.remove();
      panel.remove();
    }
  };
}
