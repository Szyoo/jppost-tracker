# @szyyw/design（vendored）

- 上游：https://github.com/Szyoo/szyyw-design
- 当前版本：**v0.5.0**
- 引入方式：Flask 无构建步骤，按上游 README 的"非 React 项目"路径 vendor 七个文件
  （tokens.css / components.css / dotfield.js / scheme.js / corner.js / settings.js /
  version.js），原样拷贝不做修改。

## 升级流程（一键）

```bash
bash scripts/update-design.sh          # 从本机上游 clone 同步（默认）
bash scripts/update-design.sh --remote # 不依赖本机 clone，直接拉 GitHub 最新 tag
```

之后本地起服务走查一遍，确认无回归再提交。
线上齿轮面板底部有版本检测——落后于上游最新 tag 时齿轮会亮角标提醒。

## 约定

- **本目录文件禁止手改**——改设计先改上游仓库、升 tag，再用脚本同步。
- 项目自有样式全部放 [../../style.css](../../style.css)（app 层），
  只允许引用 tokens 变量，禁止硬编码颜色（DESIGN.md §2）。
- 初始化入口在 [../../boot.js](../../boot.js)。
