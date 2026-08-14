# @szyyw/design（vendored）

- 上游：https://github.com/Szyoo/szyyw-design
- 当前版本：**v0.3.0**
- 引入方式：Flask 无构建步骤，按上游 README 的"非 React 项目"路径直接 vendor
  四个文件（tokens.css / components.css / dotfield.js / scheme.js），原样拷贝不做修改。

## 升级流程

```bash
cd /Users/szyyw/Documents/GitHub/szyyw-design && git pull
cp tokens.css components.css dotfield.js scheme.js \
   /Users/szyyw/Documents/GitHub/jppost-tracker/src/static/vendor/szyyw-design/
# 然后更新本文件的版本号
```

## 约定

- **本目录文件禁止手改**——改设计先改上游仓库、升 tag，再整体拷回。
- 项目自有样式全部放 [../../style.css](../../style.css)（app 层），
  只允许引用 tokens 变量，禁止硬编码颜色（DESIGN.md §2）。
- 初始化入口在 [../../boot.js](../../boot.js)。
