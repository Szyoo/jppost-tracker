# @szyyw/design（vendored）

- 上游：https://github.com/Szyoo/szyyw-design
- 当前版本：**v0.14.1**
- 引入方式：Flask 无构建步骤，由上游 `sync.sh` 按 tag 同步（文件清单见文末，只在上游维护），
  原样拷贝不做修改。`scripts/update-design.sh` 也从同一个 tag 取 sync.sh，清单与版本一致。

## 升级流程（一键，以远端为准）

```bash
bash scripts/update-design.sh          # 默认：拉 GitHub 最新 tag
bash scripts/update-design.sh v0.8.0   # 指定 tag
bash scripts/update-design.sh --local  # 例外：同步本机 clone 工作区（调试未发版改动用）
```

之后本地起服务走查一遍，确认无回归再提交。
日常不用手动做：`.github/workflows/upgrade-shared.yml`（每 6 小时，调用 `scripts/upgrade-shared.sh`）
会自动检测上游最新正式 tag，升级本目录，测试通过就推 `main`。
线上外观弹层底部有版本检测——落后于上游最新 tag 时调色板按钮会亮角标提醒。

## 约定

- **本目录文件禁止手改**——改设计先改上游仓库、升 tag，再用脚本同步。
- 项目自有样式全部放 [../../style.css](../../style.css)（app 层），
  只允许引用 tokens 变量，禁止硬编码颜色（DESIGN.md §2）。
- 初始化入口在 [../../boot.js](../../boot.js)，用 `mountChrome` 一次挂齐工具位；应用切换器与账户菜单
  只在 portal SSO 开启时挂载（`<html data-sso="1" data-portal=…>` 由服务端按 `SZYYW_SSO` 输出，否则传 `portal: null`）。
- 首屏外观（theme / palette / scheme 三键）由 [../../../templates/_appearance_head.html](../../../templates/_appearance_head.html) 在样式表前恢复。

同步到此目录的文件：tokens.css components.css corner.css dotfield.js scheme.js corner.js settings.js switcher.js account.js version.js appearance.js appearance-data.js appearance-text.js chrome-text.js locale-toggle.js chrome.js toast.js menu.js
