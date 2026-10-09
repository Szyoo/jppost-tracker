/* ============================================================
   @szyyw/design · chrome-text.js
   工具位「外壳」部件的内置文案（zh / ja / en）：应用切换器、账户菜单、语言切换。
   外观弹层 / 🌗 / 背景参数的文案在 appearance-text.js，两者同一套用法：
   应用只传 locale；个别词用 labels 覆盖；未知语言回退中文，缺键按中文补齐。
   零 DOM、零 import（服务端也可 import）。
   ============================================================ */

export const CHROME_LOCALES = ["zh", "ja", "en"];

/* 语言切换按钮 / 列表里的语言名：每种语言用它自己的写法（不随界面语言变），
   切错了语言的人也认得出自己那一项 */
const LOCALE_NAMES = { zh: "中文", ja: "日本語", en: "English" };
const LOCALE_SHORT = { zh: "中", ja: "日", en: "EN" };

const zh = {
  switcher: {
    open: "应用",
    portal: "回到门户",
    loading: "加载中…",
    empty: "没有可打开的应用",
    unauth: "未登录",
    error: "加载失败"
  },
  account: {
    login: "登录",
    settings: "账户设置",
    logout: "登出",
    /** {status} 换成 statusFmt（有 HTTP 状态码时）或空串 */
    logoutFailed: "登出失败{status}，请重试",
    statusFmt: "（{code}）",
    roles: { admin: "管理员", user: "用户", guest: "访客" }
  },
  locale: {
    title: "语言",
    names: LOCALE_NAMES,
    short: LOCALE_SHORT
  }
};

const ja = {
  switcher: {
    open: "アプリ",
    portal: "ポータルに戻る",
    loading: "読み込み中…",
    empty: "開けるアプリがありません",
    unauth: "未ログイン",
    error: "読み込みに失敗しました"
  },
  account: {
    login: "ログイン",
    settings: "アカウント設定",
    logout: "ログアウト",
    logoutFailed: "ログアウトに失敗しました{status}。もう一度お試しください",
    statusFmt: "（{code}）",
    roles: { admin: "管理者", user: "ユーザー", guest: "ゲスト" }
  },
  locale: {
    title: "言語",
    names: LOCALE_NAMES,
    short: LOCALE_SHORT
  }
};

const en = {
  switcher: {
    open: "Apps",
    portal: "Back to portal",
    loading: "Loading…",
    empty: "No apps available",
    unauth: "Not signed in",
    error: "Failed to load"
  },
  account: {
    login: "Sign in",
    settings: "Account settings",
    logout: "Sign out",
    logoutFailed: "Sign-out failed{status}, please retry",
    statusFmt: " ({code})",
    roles: { admin: "Admin", user: "User", guest: "Guest" }
  },
  locale: {
    title: "Language",
    names: LOCALE_NAMES,
    short: LOCALE_SHORT
  }
};

export const CHROME_TEXT = { zh, ja, en };

/** "ja-JP" → "ja"；不认识的回退 "zh"（与 appearance-text.js 同规则） */
export function chromeLocale(locale) {
  const base = String(locale || "zh").toLowerCase().split(/[-_]/)[0];
  return CHROME_LOCALES.includes(base) ? base : "zh";
}

/**
 * 某语言的全套外壳文案：缺键按中文补齐，再逐段叠 overrides
 * （{ switcher?, account?: { roles? }, locale?: { names?, short? } }）。返回新对象，不改内置表。
 */
export function chromeText(locale, overrides = {}) {
  const base = CHROME_TEXT[chromeLocale(locale)];
  const o = overrides || {};
  const oa = o.account || {};
  const ol = o.locale || {};
  return {
    switcher: { ...zh.switcher, ...base.switcher, ...(o.switcher || {}) },
    account: {
      ...zh.account,
      ...base.account,
      ...oa,
      roles: { ...zh.account.roles, ...base.account.roles, ...(oa.roles || {}) }
    },
    locale: {
      ...zh.locale,
      ...base.locale,
      ...ol,
      names: { ...LOCALE_NAMES, ...(ol.names || {}) },
      short: { ...LOCALE_SHORT, ...(ol.short || {}) }
    }
  };
}

/** 角色显示名：内置 / 覆盖表里有就用，没有（未知角色）原样返回 */
export function roleLabel(role, roles) {
  const r = String(role ?? "");
  return (roles && Object.prototype.hasOwnProperty.call(roles, r) && roles[r]) || r;
}
