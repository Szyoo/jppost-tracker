/* ============================================================
   @szyyw/design · appearance.js
   外观（主题 → 配色 → 明暗）的客户端状态与统一「外观」弹层。

   状态唯一存放处是 <html data-theme / data-palette / data-scheme>（SSR 渲染的属性是真相）。
   - 明暗仍归 scheme.js 管：这里只转调 setScheme，持久化由 configureScheme 决定；
     🌗 按钮改明暗时也会转成一次 "szyyw:appearancechange"（changed = ["scheme"]），
     应用只在 onAppearanceChange / mountAppearancePanel({ onChange }) 一处收账。
   - 主题 / 配色由 configureAppearance 决定写 cookie / localStorage / 不存。
   - 选项表与服务端读法在 appearance-data.js（零 DOM，服务端可 import）。

   mountAppearancePanel 是 mountDotFieldSettings 的上位替代：同一枚调色板按钮（order 20），
   面板 = 配色 + 明暗 +（传了 field）折叠的「背景参数」+ 版本行。两者二选一，不要同时挂。
   模块顶层不碰 DOM：SSR 时被求值也不会炸。
   ============================================================ */

import {
  getScheme,
  setScheme,
  onSchemeChange,
  refreshThemeColor,
  configureScheme,
  mountSchemeToggle
} from "./scheme.js";
import { mountCornerTool, CORNER_ORDER } from "./corner.js";
import {
  renderDotFieldControls,
  renderUpdateSection,
  createPaletteToggle,
  createCornerPanel,
  bindCornerPanel,
  restoreDotFieldSettings
} from "./settings.js";
import { mountDotField, attachSpot } from "./dotfield.js";
import {
  THEMES,
  PALETTES,
  SCHEMES,
  APPEARANCE_COOKIES,
  normalizeAppearance,
  appearanceCookieNames
} from "./appearance-data.js";
import { appearanceText } from "./appearance-text.js";

const EVENT = "szyyw:appearancechange";

const config = {
  /** "cookie" | "localStorage" | "none"。SSR 项目用 cookie，服务端要读它（与 configureScheme 同构） */
  persist: "cookie",
  storageKeys: { theme: APPEARANCE_COOKIES.theme, palette: APPEARANCE_COOKIES.palette },
  cookieDays: 365
};

function readStored(key) {
  const name = config.storageKeys[key];
  if (!name) return null;
  try {
    if (config.persist === "cookie") {
      const esc = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      const m = document.cookie.match(new RegExp(`(?:^|;\\s*)${esc}=([^;]*)`));
      return m ? decodeURIComponent(m[1]) : null;
    }
    if (config.persist === "localStorage") return localStorage.getItem(name);
  } catch {
    // 隐私模式 / 坏编码——当没存过
  }
  return null;
}

function writeStored(key, value) {
  const name = config.storageKeys[key];
  if (!name) return;
  try {
    if (config.persist === "cookie") {
      document.cookie = `${name}=${encodeURIComponent(value)}; path=/; max-age=${
        60 * 60 * 24 * config.cookieDays
      }; samesite=lax`;
    } else if (config.persist === "localStorage") {
      localStorage.setItem(name, value);
    }
  } catch {
    // 存不了——功能照常，只是刷新后回到缺省
  }
}

function emit(appearance, changed) {
  document.dispatchEvent(new CustomEvent(EVENT, { detail: { appearance, changed } }));
}

/* 🌗 / 任何 setScheme 调用 → appearancechange(["scheme"])。
   懒绑一次（模块顶层不碰 document）；setAppearance 自己改明暗时先压住，
   最后合成一次事件，免得同一次改动广播两遍。 */
let bridged = false;
let muteScheme = false;

function ensureBridge() {
  if (bridged || typeof document === "undefined") return;
  bridged = true;
  onSchemeChange(() => {
    if (!muteScheme) emit(getAppearance(), ["scheme"]);
  });
}

function writeAttrs(theme, palette) {
  const root = document.documentElement;
  root.dataset.theme = theme;
  // default 配色 = tokens.css 的 :root 基础块，不写属性（与 appearanceAttrs 一致）
  if (palette === "default") root.removeAttribute("data-palette");
  else root.dataset.palette = palette;
}

/** 读 <html> 当前外观（SSR 渲染的属性是真相；缺省项按 DEFAULT_APPEARANCE 兜底） */
export function getAppearance() {
  const ds = document.documentElement.dataset;
  return normalizeAppearance({ theme: ds.theme, palette: ds.palette, scheme: getScheme() });
}

/**
 * 改外观。主题 / 配色写 <html> 并按 configureAppearance 持久化；明暗转交 setScheme。
 * 随后重算 theme-color，有变化才广播一次 "szyyw:appearancechange"。
 */
export function setAppearance(patch = {}, { persist = true } = {}) {
  ensureBridge();
  const cur = getAppearance();
  const next = normalizeAppearance({ ...cur, ...(patch || {}) });
  const changed = [];
  if (next.theme !== cur.theme) changed.push("theme");
  if (next.palette !== cur.palette) changed.push("palette");

  writeAttrs(next.theme, next.palette);
  if (persist) {
    writeStored("theme", next.theme);
    writeStored("palette", next.palette);
  }

  if (next.scheme !== cur.scheme) {
    changed.push("scheme");
    muteScheme = true;
    try {
      setScheme(next.scheme, { persist });
    } finally {
      muteScheme = false;
    }
  }

  // 配色变了底色就变；setScheme 只在明暗变化时会算
  refreshThemeColor();
  const result = getAppearance();
  if (changed.length) emit(result, changed);
  return result;
}

/** 订阅外观变化（含 🌗 改明暗），返回解绑函数 */
export function onAppearanceChange(handler) {
  ensureBridge();
  const listener = (e) => handler(e.detail.appearance, e.detail.changed);
  document.addEventListener(EVENT, listener);
  return () => document.removeEventListener(EVENT, listener);
}

/**
 * 配置主题 / 配色的持久化并对齐初始状态（与 configureScheme 同构；明暗的键走 configureScheme）。
 * <html> 上已有合法属性（SSR 渲染 / <head> 内联脚本写好）的项不覆盖；
 * 没有的项从存储恢复（静态页）。
 */
export function configureAppearance(options = {}) {
  const { storageKeys, ...rest } = options || {};
  Object.assign(config, rest);
  if (storageKeys) config.storageKeys = { ...config.storageKeys, ...storageKeys };
  ensureBridge();

  const ds = document.documentElement.dataset;
  const patch = {};
  if (!THEMES.some((t) => t.id === ds.theme)) {
    const stored = readStored("theme");
    if (stored) patch.theme = stored;
  }
  if (!PALETTES.some((p) => p.id === ds.palette)) {
    const stored = readStored("palette");
    if (stored) patch.palette = stored;
  }
  if (Object.keys(patch).length) setAppearance(patch, { persist: false });
  else refreshThemeColor();
  return getAppearance();
}

/* ---------- 外观弹层 ---------- */

/** 最近一次 mountAppearancePanel 的句柄，给 openAppearancePanel 用 */
let current = null;

/**
 * 打开最近一次 mountAppearancePanel 挂出的面板（如设置页里的「打开外观」按钮）。
 * 没挂过返回 false（no-op）。
 */
export function openAppearancePanel() {
  if (!current) return false;
  current.open();
  return true;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/** 一行 chip：.ctl.appearance-row > .ctl-label + .chip-row > button.chip */
function chipRow(key, label, options, onPick) {
  const row = el("div", "ctl appearance-row");
  row.dataset.key = key;
  const id = `szyyw-appearance-${key}`;
  const title = el("span", "ctl-label", label);
  title.id = id;
  const chips = el("div", "chip-row");
  chips.setAttribute("role", "group");
  chips.setAttribute("aria-labelledby", id);
  const buttons = options.map(({ value, text }) => {
    const b = el("button", "chip", text);
    b.type = "button";
    b.dataset.value = value;
    b.addEventListener("click", () => onPick(value));
    chips.append(b);
    return b;
  });
  row.append(title, chips);
  return {
    row,
    render(active) {
      for (const b of buttons) {
        const on = b.dataset.value === active;
        b.classList.toggle("active", on);
        b.setAttribute("aria-pressed", String(on));
      }
    }
  };
}

/**
 * 统一「外观」弹层，挂进右上角工具位（缺省 order 20，调色板图标，.settings-toggle）。
 * 面板：（主题 > 1 个时）主题行 → 配色行 → 明暗行 →（传了 field）折叠「背景参数」→（dotField.update !== false）版本行。
 * 与 mountDotFieldSettings 二选一。
 */
export function mountAppearancePanel({
  field = null,
  title = null,
  order = CORNER_ORDER.settings,
  onChange = null,
  dotField = {},
  labels = {},
  locale = "zh"
} = {}) {
  if (field && !field.setOptions) throw new Error("mountAppearancePanel 的 field 需要 mountDotField() 返回的实例");
  ensureBridge();

  const {
    persist = "localStorage",
    storageKey = "szyyw:dotfield",
    onSave = null,
    note = "",
    update = {}
  } = dotField || {};

  // 内置文案（locale）打底，labels 逐键覆盖；open 跟着标题走，除非单独给了
  const text = appearanceText(locale, labels);
  title = title ?? labels.title ?? text.title;
  if (!labels.open) text.open = title;

  const btn = createPaletteToggle(text.open);
  const { panel, closeBtn, body } = createCornerPanel({
    className: "settings-panel appearance-panel",
    title,
    closeLabel: text.close,
    withFoot: false
  });

  /* ---------- 主题 / 配色 / 明暗 ---------- */

  const rows = [];

  if (THEMES.length > 1) {
    const r = chipRow(
      "theme",
      text.theme,
      THEMES.map((t) => ({ value: t.id, text: text.themes[t.id] ?? t.id })),
      (v) => setAppearance({ theme: v })
    );
    rows.push(["theme", r]);
    body.append(r.row);
  }

  const paletteRow = chipRow(
    "palette",
    text.palette,
    // 只列当前主题的配色；主题只有一个时就是全部
    PALETTES.filter((p) => p.theme === getAppearance().theme).map((p) => ({
      value: p.id,
      text: text.palettes[p.id] ?? p.id
    })),
    (v) => setAppearance({ palette: v })
  );
  rows.push(["palette", paletteRow]);
  body.append(paletteRow.row);

  const schemeRow = chipRow(
    "scheme",
    text.scheme,
    SCHEMES.map((s) => ({ value: s, text: text.schemes[s] ?? s })),
    (v) => setAppearance({ scheme: v })
  );
  rows.push(["scheme", schemeRow]);
  body.append(schemeRow.row);

  const renderRows = () => {
    const a = getAppearance();
    for (const [key, r] of rows) r.render(a[key]);
  };

  /* ---------- 背景参数（折叠） ---------- */

  let controls = null;
  if (field) {
    const details = el("details", "appearance-advanced");
    details.append(el("summary", "ctl-label", text.background));
    const advBody = el("div", "appearance-advanced-body");
    const advFoot = el("div", "appearance-advanced-foot");
    details.append(advBody, advFoot);
    body.append(details);
    controls = renderDotFieldControls({
      field,
      body: advBody,
      foot: advFoot,
      persist,
      storageKey,
      onSave,
      note,
      text
    });
  }

  /* ---------- 版本行 ---------- */

  const runCheck = update !== false ? renderUpdateSection({ body, btn, text, update }) : null;

  /* ---------- 同步与开合 ---------- */

  const refresh = () => {
    renderRows();
    controls?.sync();
  };

  // 外观任何来源的变化（面板 / 🌗 / setAppearance）都让面板跟上，并转给应用存账号级偏好
  const off = onAppearanceChange((a, changed) => {
    refresh();
    if (!onChange) return;
    try {
      const r = onChange(a, changed);
      if (r && typeof r.catch === "function") r.catch(() => {});
    } catch {
      // 应用侧存储失败不打断界面
    }
  });

  const ctl = bindCornerPanel({ btn, panel, closeBtn, onOpen: refresh });

  renderRows();
  controls?.rerender();
  document.body.append(panel);
  const unmount = mountCornerTool(btn, { order });

  const handle = {
    open: () => ctl.setOpen(true),
    close: () => ctl.setOpen(false),
    /** 外部改了外观 / 背景参数后让面板跟上 */
    sync: refresh,
    /** 手动触发一次强制检查（绕过缓存）；update: false 时 no-op */
    checkUpdate: () => runCheck?.(true),
    destroy() {
      ctl.unbind();
      off();
      unmount();
      panel.remove();
      if (current === handle) current = null;
    }
  };
  current = handle;
  return handle;
}

/* ---------- 一次挂齐 ---------- */

/**
 * 应用接外观的一站式入口（v0.12.0）：明暗持久化 + 🌗 按钮 + 主题 / 配色持久化 +
 * （给了 background）点阵背景与光斑 + 外观弹层，全部按同一个 locale、同一组存储键。
 * 等价于依次调 configureScheme / configureAppearance / mountDotField / attachSpot /
 * mountSchemeToggle / mountAppearancePanel；需要细调时仍可分开调。
 *
 * 切语言用 handle.setLocale——只重挂按钮与弹层，背景画布不重建。
 */
export function mountAppearance({
  background = null,
  spot = true,
  persist = "cookie",
  cookiePrefix = "",
  locale = "zh",
  labels = {},
  onChange = null,
  dotField = {}
} = {}) {
  const keys = appearanceCookieNames(cookiePrefix);
  configureScheme({ persist, storageKey: keys.scheme });
  configureAppearance({ persist, storageKeys: { theme: keys.theme, palette: keys.palette } });

  // restore 把存过的颜色写回 token，并把行为参数交给画布
  const field = background ? mountDotField(background, restoreDotFieldSettings()) : null;
  const detachSpot = background && spot ? attachSpot() : null;

  let toggle = null;
  let panel = null;
  const mountUi = (loc) => {
    toggle?.destroy();
    panel?.destroy();
    toggle = mountSchemeToggle({ locale: loc, labels: labels.schemes });
    panel = mountAppearancePanel({ field, locale: loc, labels, onChange, dotField });
  };
  mountUi(locale);

  return {
    field,
    setLocale: mountUi,
    open: () => panel.open(),
    close: () => panel.close(),
    sync: () => panel.sync(),
    destroy() {
      toggle.destroy();
      panel.destroy();
      detachSpot?.();
      field?.destroy();
    }
  };
}
