// 设计包初始化入口：点阵背景 + hover 光斑 + 明暗切换按钮。
// 依赖 vendor 的 @szyyw/design v0.3.0（见 vendor/szyyw-design/VENDORED.md）。
import { mountDotField, attachSpot } from '/static/vendor/szyyw-design/dotfield.js';
import { configureScheme, mountSchemeToggle } from '/static/vendor/szyyw-design/scheme.js';

const layer = document.querySelector('.bg-layer');
if (layer) mountDotField(layer);
attachSpot();

// data-scheme 已由 <head> 里的内联脚本在首帧前恢复，这里只接管持久化与按钮
configureScheme({ persist: 'localStorage', storageKey: 'jppost_scheme' });
mountSchemeToggle({ labels: { auto: '跟随系统', light: '浅色', dark: '深色' } });
