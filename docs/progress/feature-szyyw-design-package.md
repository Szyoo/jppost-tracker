# feature/szyyw-design-package 分支进度

基于 `refactor/streamline-services` 分支（后者尚未合并 main）。

## 2026-07-19：接入 @szyyw/design v0.3.0 完成

- **vendor 方式**：Flask 无构建步骤，按上游 README"非 React 项目"路径把
  tokens.css / components.css / dotfield.js / scheme.js 原样拷入
  `src/static/vendor/szyyw-design/`，版本与升级流程记录在同目录 VENDORED.md。
  上游权威仓库：`/Users/szyyw/Documents/GitHub/szyyw-design`（改设计先改上游、升 tag 再拷回）。
- **style.css 缩减为纯应用层**：删掉与包重复的 token 定义和基础组件
  （glass/btn/field/pill/动效等），保留项目特有类（页头/页签/开关/widget/用户列表/
  认证页/服务卡/日志窗），颜色全部改走 token（color-mix 派生），无硬编码。
- **新能力：明暗三态**（暗/亮/跟随系统）——tokens 全部 light-dark() 双值，
  右上角常驻 `.scheme-toggle`，localStorage 持久化（key `jppost_scheme`），
  各模板 head 有内联脚本首帧恢复防闪烁；`.site-header` 加了 44px 右 padding 给按钮让位。
- **日志窗按规范 §9** 改用 `--term-*` token，浅色模式下保持真终端深色。
- 删除旧的手工移植 dotfield.js / ui.js，初始化统一进 `boot.js`（ESM）。
- **本地实机验证**：登录页/管理台四页签在明暗双模下渲染正常，DotField 换色正常，
  Vue 挂载、Socket.IO 日志推送（19 行历史日志回放）无回归；临时测试 admin 已从本地库清理。

## 待办

- [ ] VPS 部署验证（等 refactor/streamline-services 一起或先后合并 main 后统一上线）。
- [ ] 上游 szyyw-design 升级时记得同步 vendor（见 VENDORED.md）。
