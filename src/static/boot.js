// 设计包初始化入口：点阵背景 + hover 光斑 + 明暗切换 + 背景参数面板。
// 依赖 vendor 的 @szyyw/design（版本见 vendor/szyyw-design/VENDORED.md）。
import { mountDotField, attachSpot } from '/static/vendor/szyyw-design/dotfield.js';
import { configureScheme, mountSchemeToggle } from '/static/vendor/szyyw-design/scheme.js';
import { restoreDotFieldSettings, mountDotFieldSettings } from '/static/vendor/szyyw-design/settings.js';

const layer = document.querySelector('.bg-layer');
const field = layer ? mountDotField(layer, restoreDotFieldSettings()) : null;
attachSpot();

// data-scheme 已由 <head> 里的内联脚本在首帧前恢复，这里只接管持久化与按钮
configureScheme({ persist: 'localStorage', storageKey: 'jppost_scheme' });
mountSchemeToggle({ labels: { auto: '跟随系统', light: '浅色', dark: '深色' } });

// 背景参数抽屉：改动只存本浏览器；版本检测的升级命令走本项目的同步脚本
if (field) {
  mountDotFieldSettings({
    field,
    note: '参数仅保存在本浏览器',
    update: {
      command: () => 'bash scripts/update-design.sh --remote',
    },
  });
}
