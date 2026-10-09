/* ============================================================
   @szyyw/design · appearance-text.js
   外观弹层 / 明暗按钮 / 背景参数控件的内置文案（zh / ja / en）。

   应用只传 locale，不再各自维护一份同样的翻译；个别词想改仍可用 labels 覆盖。
   未知语言回退中文；缺的键也按中文补齐。零 DOM、零 import。
   ============================================================ */

export const APPEARANCE_LOCALES = ["zh", "ja", "en"];

const zh = {
  title: "外观",
  open: "外观",
  close: "关闭",
  reset: "恢复默认",
  save: "保存",
  saving: "保存中…",
  saved: "已保存 ✓",
  error: "失败，重试",
  version: "设计包版本",
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
  theme: "主题",
  palette: "配色",
  scheme: "明暗",
  background: "背景参数",
  themes: { nebula: "星云" },
  palettes: { default: "青紫（默认）", aurora: "极光翠青" },
  schemes: { auto: "跟随系统", dark: "深色", light: "浅色" },
  controls: {
    dotRadius: "点大小",
    dotSpacing: "点间距",
    cursorRadius: "鼠标影响半径",
    cursorForce: "扰动力度",
    bulgeStrength: "凹陷强度",
    glowRadius: "鼠标光晕半径",
    waveAmplitude: "波浪幅度",
    bulgeOnly: "凹陷模式",
    bulgeOnlyHint: "开=鼠标压出凹陷；关=点被推开回弹",
    sparkle: "随机闪烁",
    gradientFrom: "点阵渐变 A",
    gradientTo: "点阵渐变 B",
    glowColor: "光晕颜色"
  }
};

const ja = {
  title: "外観",
  open: "外観",
  close: "閉じる",
  reset: "初期値に戻す",
  save: "保存",
  saving: "保存中…",
  saved: "保存しました ✓",
  error: "失敗しました。再試行",
  version: "デザインパッケージのバージョン",
  check: "更新を確認",
  checking: "確認中…",
  upToDate: "最新です ✓",
  updateAvailable: "新しい版があります",
  viewChanges: "変更内容を見る",
  copyCommand: "更新コマンドをコピー",
  copied: "コピーしました ✓",
  updateNow: "更新",
  updating: "更新中…",
  updated: "更新しました ✓",
  updateFailed: "更新に失敗しました。再試行",
  checkFailed: "確認に失敗（ネットワークまたは制限）",
  theme: "テーマ",
  palette: "カラー",
  scheme: "モード",
  background: "背景設定",
  themes: { nebula: "ネビュラ" },
  palettes: { default: "シアン×バイオレット（既定）", aurora: "オーロラ（グリーン×シアン）" },
  schemes: { auto: "システムに従う", dark: "ダーク", light: "ライト" },
  controls: {
    dotRadius: "ドットの大きさ",
    dotSpacing: "ドットの間隔",
    cursorRadius: "カーソルの影響範囲",
    cursorForce: "揺らぎの強さ",
    bulgeStrength: "へこみの強さ",
    glowRadius: "カーソルの光の範囲",
    waveAmplitude: "波の振幅",
    bulgeOnly: "へこみモード",
    bulgeOnlyHint: "オン＝カーソルでへこむ／オフ＝ドットが押しのけられて戻る",
    sparkle: "ランダムにきらめく",
    gradientFrom: "ドットのグラデーション A",
    gradientTo: "ドットのグラデーション B",
    glowColor: "光の色"
  }
};

const en = {
  title: "Appearance",
  open: "Appearance",
  close: "Close",
  reset: "Reset to defaults",
  save: "Save",
  saving: "Saving…",
  saved: "Saved ✓",
  error: "Failed, retry",
  version: "Design package version",
  check: "Check for updates",
  checking: "Checking…",
  upToDate: "Up to date ✓",
  updateAvailable: "Update available",
  viewChanges: "View changes",
  copyCommand: "Copy upgrade command",
  copied: "Copied ✓",
  updateNow: "Update to",
  updating: "Updating…",
  updated: "Updated ✓",
  updateFailed: "Update failed, retry",
  checkFailed: "Check failed (network or rate limit)",
  theme: "Theme",
  palette: "Palette",
  scheme: "Mode",
  background: "Background",
  themes: { nebula: "Nebula" },
  palettes: { default: "Cyan–violet (default)", aurora: "Aurora green–cyan" },
  schemes: { auto: "Follow system", dark: "Dark", light: "Light" },
  controls: {
    dotRadius: "Dot size",
    dotSpacing: "Dot spacing",
    cursorRadius: "Cursor radius",
    cursorForce: "Push strength",
    bulgeStrength: "Dent depth",
    glowRadius: "Cursor glow radius",
    waveAmplitude: "Wave amplitude",
    bulgeOnly: "Dent mode",
    bulgeOnlyHint: "On = the cursor dents the field; off = dots are pushed away and spring back",
    sparkle: "Random sparkle",
    gradientFrom: "Dot gradient A",
    gradientTo: "Dot gradient B",
    glowColor: "Glow color"
  }
};

export const APPEARANCE_TEXT = { zh, ja, en };

/** "ja-JP" → "ja"；不认识的回退 "zh" */
function pick(locale) {
  const base = String(locale || "zh").toLowerCase().split(/[-_]/)[0];
  return APPEARANCE_LOCALES.includes(base) ? base : "zh";
}

/**
 * 某语言的全套文案（缺的键按中文补齐，嵌套表逐个合并）；
 * 再叠 overrides（应用的 labels），返回新对象，不改内置表。
 */
export function appearanceText(locale, overrides = {}) {
  const base = APPEARANCE_TEXT[pick(locale)];
  const o = overrides || {};
  const merged = { ...zh, ...base, ...o };
  for (const k of ["themes", "palettes", "schemes", "controls"]) {
    merged[k] = { ...zh[k], ...base[k], ...(o[k] || {}) };
  }
  return merged;
}
