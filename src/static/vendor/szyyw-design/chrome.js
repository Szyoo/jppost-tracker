/* ============================================================
   @szyyw/design · chrome.js
   一站式工具位（v0.13.0，新项目首选）：一个调用挂齐右上角整条——
     应用切换器(5) · 账户菜单(6) · 🌗(10) · 语言(15) · 外观(20) + 点阵背景 / 光斑 + 外观持久化。
   等价于 mountAppearance +（SSO 站点）mountAppSwitcher + mountAccountMenu +（给了语言列表）mountLocaleToggle，
   全部同一个 locale；setLocale 一次同步所有子件，背景画布不重建。

   非 SSO 应用传 portal: null（或 false），只挂外观与语言。
   ============================================================ */

import { mountAppearance } from "./appearance.js";
import { mountAppSwitcher } from "./switcher.js";
import { mountAccountMenu } from "./account.js";
import { mountLocaleToggle } from "./locale-toggle.js";

const DEFAULT_PORTAL = "https://szyyw.xyz";

/**
 * @param {object}  options
 * @param {HTMLElement|null} options.background  点阵背景容器（通常 .bg-layer）；不给就没有背景
 * @param {string}  options.cookiePrefix  外观三项存储键前缀（同 mountAppearance）
 * @param {"cookie"|"localStorage"|"none"} options.persist  外观持久化，缺省 cookie
 * @param {string}  options.locale        "zh" | "ja" | "en"，缺省 "zh"
 * @param {string|null|false} options.portal  门户地址；缺省 https://szyyw.xyz；null / false = 非 SSO，不挂切换器与账户菜单
 * @param {object}  options.appearance    透传给 mountAppearance 的其余选项（labels / onChange / dotField / spot）
 * @param {object|false} options.switcher 透传给 mountAppSwitcher（labels / order / current / …）；false = 不挂
 * @param {object|false} options.account  透传给 mountAccountMenu（labels / order / onChange）；false = 不挂
 * @param {object}  options.localeToggle  { locales, onChange?, order?, labels? }；给了 locales 才挂语言切换
 */
export function mountChrome({
  background = null,
  cookiePrefix = "",
  persist = "cookie",
  locale = "zh",
  portal = DEFAULT_PORTAL,
  appearance: appearanceOpts = {},
  switcher: switcherOpts = {},
  account: accountOpts = {},
  localeToggle: localeOpts = null
} = {}) {
  let current = locale;

  const appearance = mountAppearance({
    ...(appearanceOpts || {}),
    background,
    persist,
    cookiePrefix,
    locale
  });

  const sso = !!portal;
  const switcher = sso && switcherOpts !== false ? mountAppSwitcher({ ...(switcherOpts || {}), portal, locale }) : null;
  const account = sso && accountOpts !== false ? mountAccountMenu({ ...(accountOpts || {}), portal, locale }) : null;

  let localeToggle = null;
  const handle = {
    appearance,
    switcher,
    account,
    localeToggle: null,
    get locale() {
      return current;
    },
    /** 同步所有子件的文案；画布不重建。工具位语言按钮选中时会自动调它，再调 localeToggle.onChange */
    setLocale(next) {
      if (!next) return;
      current = next;
      appearance.setLocale(next);
      switcher?.setLocale(next);
      account?.setLocale(next);
      localeToggle?.set(next);
    },
    destroy() {
      localeToggle?.destroy();
      account?.destroy();
      switcher?.destroy();
      appearance.destroy();
    }
  };

  if (localeOpts && Array.isArray(localeOpts.locales) && localeOpts.locales.length) {
    const { onChange, ...rest } = localeOpts;
    localeToggle = mountLocaleToggle({
      ...rest,
      current: locale,
      onChange: (next) => {
        handle.setLocale(next);
        onChange?.(next);
      }
    });
    handle.localeToggle = localeToggle;
  }

  return handle;
}
