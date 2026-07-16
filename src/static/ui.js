// 页面级交互增强：spotlight 聚光跟随（DESIGN.md §6）。
// 事件委托到 document，对任何带 .spot 的元素在 mousemove 时写入 --mx/--my。
(function () {
  'use strict';
  document.addEventListener(
    'mousemove',
    function (e) {
      var target = e.target && e.target.closest ? e.target.closest('.spot') : null;
      if (!target) return;
      var rect = target.getBoundingClientRect();
      target.style.setProperty('--mx', e.clientX - rect.left + 'px');
      target.style.setProperty('--my', e.clientY - rect.top + 'px');
    },
    { passive: true }
  );
})();
