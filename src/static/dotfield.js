// DotField 交互式点阵背景 —— 从 szyyw.xyz portal 的 React 版移植为纯 JS。
// 挂载到 .bg-layer 容器上：全屏 fixed canvas + SVG 光晕，鼠标扰动 / 闪烁 / 波浪。
(function () {
  'use strict';

  var TWO_PI = Math.PI * 2;

  // 默认参数与 portal 保持一致（DESIGN.md §4）
  var PROPS = {
    dotRadius: 1.6,
    dotSpacing: 16,
    cursorRadius: 420,
    cursorForce: 0.12,
    bulgeOnly: false,
    bulgeStrength: 40,
    glowRadius: 180,
    sparkle: true,
    waveAmplitude: 2.5,
    gradientFrom: 'rgba(56, 189, 248, 0.5)',
    gradientTo: 'rgba(168, 85, 247, 0.4)',
    glowColor: '#0b1020'
  };

  function mountDotField(container) {
    var canvas = document.createElement('canvas');
    canvas.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;';
    container.appendChild(canvas);

    var svgNS = 'http://www.w3.org/2000/svg';
    var svg = document.createElementNS(svgNS, 'svg');
    svg.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;pointer-events:none;';
    var defs = document.createElementNS(svgNS, 'defs');
    var glowId = 'dot-field-glow-' + Math.random().toString(36).slice(2, 9);
    var gradient = document.createElementNS(svgNS, 'radialGradient');
    gradient.setAttribute('id', glowId);
    var stop0 = document.createElementNS(svgNS, 'stop');
    stop0.setAttribute('offset', '0%');
    stop0.setAttribute('stop-color', PROPS.glowColor);
    var stop1 = document.createElementNS(svgNS, 'stop');
    stop1.setAttribute('offset', '100%');
    stop1.setAttribute('stop-color', 'transparent');
    gradient.appendChild(stop0);
    gradient.appendChild(stop1);
    defs.appendChild(gradient);
    svg.appendChild(defs);
    var glowEl = document.createElementNS(svgNS, 'circle');
    glowEl.setAttribute('cx', '-9999');
    glowEl.setAttribute('cy', '-9999');
    glowEl.setAttribute('r', PROPS.glowRadius);
    glowEl.setAttribute('fill', 'url(#' + glowId + ')');
    glowEl.style.opacity = '0';
    glowEl.style.willChange = 'opacity';
    svg.appendChild(glowEl);
    container.appendChild(svg);

    var ctx = canvas.getContext('2d', { alpha: true });
    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    var dots = [];
    var mouse = { x: -9999, y: -9999, prevX: -9999, prevY: -9999, speed: 0 };
    var size = { w: 0, h: 0, offsetX: 0, offsetY: 0 };
    var glowOpacity = 0;
    var engagement = 0;
    var frameCount = 0;
    var resizeTimer = null;

    function buildDots(w, h) {
      var step = PROPS.dotRadius + PROPS.dotSpacing;
      var cols = Math.floor(w / step);
      var rows = Math.floor(h / step);
      var padX = (w % step) / 2;
      var padY = (h % step) / 2;
      dots = new Array(rows * cols);
      var idx = 0;
      for (var row = 0; row < rows; row++) {
        for (var col = 0; col < cols; col++) {
          var ax = padX + col * step + step / 2;
          var ay = padY + row * step + step / 2;
          dots[idx++] = { ax: ax, ay: ay, sx: ax, sy: ay, vx: 0, vy: 0, x: ax, y: ay };
        }
      }
    }

    function doResize() {
      var rect = container.getBoundingClientRect();
      var w = rect.width;
      var h = rect.height;
      canvas.width = w * dpr;
      canvas.height = h * dpr;
      canvas.style.width = w + 'px';
      canvas.style.height = h + 'px';
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      size = { w: w, h: h, offsetX: rect.left + window.scrollX, offsetY: rect.top + window.scrollY };
      buildDots(w, h);
    }

    function onResize() {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(doResize, 100);
    }

    function onMouseMove(e) {
      mouse.x = e.pageX - size.offsetX;
      mouse.y = e.pageY - size.offsetY;
    }

    setInterval(function updateMouseSpeed() {
      var dx = mouse.prevX - mouse.x;
      var dy = mouse.prevY - mouse.y;
      var dist = Math.sqrt(dx * dx + dy * dy);
      mouse.speed += (dist - mouse.speed) * 0.5;
      if (mouse.speed < 0.001) mouse.speed = 0;
      mouse.prevX = mouse.x;
      mouse.prevY = mouse.y;
    }, 20);

    function tick() {
      frameCount++;
      var w = size.w;
      var h = size.h;
      var t = frameCount * 0.02;

      var targetEngagement = Math.min(mouse.speed / 5, 1);
      engagement += (targetEngagement - engagement) * 0.06;
      if (engagement < 0.001) engagement = 0;
      glowOpacity += (engagement - glowOpacity) * 0.08;

      glowEl.setAttribute('cx', mouse.x);
      glowEl.setAttribute('cy', mouse.y);
      glowEl.style.opacity = glowOpacity;

      ctx.clearRect(0, 0, w, h);
      var grad = ctx.createLinearGradient(0, 0, w, h);
      grad.addColorStop(0, PROPS.gradientFrom);
      grad.addColorStop(1, PROPS.gradientTo);
      ctx.fillStyle = grad;

      var cr = PROPS.cursorRadius;
      var crSq = cr * cr;
      var rad = PROPS.dotRadius / 2;
      var isBulge = PROPS.bulgeOnly;

      ctx.beginPath();
      for (var i = 0; i < dots.length; i++) {
        var d = dots[i];
        var dx = mouse.x - d.ax;
        var dy = mouse.y - d.ay;
        var distSq = dx * dx + dy * dy;

        if (distSq < crSq && engagement > 0.01) {
          var dist = Math.sqrt(distSq);
          if (isBulge) {
            var k = 1 - dist / cr;
            var push = k * k * PROPS.bulgeStrength * engagement;
            var angle = Math.atan2(dy, dx);
            d.sx += (d.ax - Math.cos(angle) * push - d.sx) * 0.15;
            d.sy += (d.ay - Math.sin(angle) * push - d.sy) * 0.15;
          } else {
            var angle2 = Math.atan2(dy, dx);
            var move = (500 / dist) * (mouse.speed * PROPS.cursorForce);
            d.vx += Math.cos(angle2) * -move;
            d.vy += Math.sin(angle2) * -move;
          }
        } else if (isBulge) {
          d.sx += (d.ax - d.sx) * 0.1;
          d.sy += (d.ay - d.sy) * 0.1;
        }

        if (!isBulge) {
          d.vx *= 0.9;
          d.vy *= 0.9;
          d.x = d.ax + d.vx;
          d.y = d.ay + d.vy;
          d.sx += (d.x - d.sx) * 0.1;
          d.sy += (d.y - d.sy) * 0.1;
        }

        var drawX = d.sx;
        var drawY = d.sy;
        if (PROPS.waveAmplitude > 0) {
          drawY += Math.sin(d.ax * 0.03 + t) * PROPS.waveAmplitude;
          drawX += Math.cos(d.ay * 0.03 + t * 0.7) * PROPS.waveAmplitude * 0.5;
        }

        if (PROPS.sparkle) {
          var hash = ((i * 2654435761) ^ (frameCount >> 3)) >>> 0;
          if (hash % 100 < 3) {
            ctx.moveTo(drawX + rad * 1.8, drawY);
            ctx.arc(drawX, drawY, rad * 1.8, 0, TWO_PI);
          } else {
            ctx.moveTo(drawX + rad, drawY);
            ctx.arc(drawX, drawY, rad, 0, TWO_PI);
          }
        } else {
          ctx.moveTo(drawX + rad, drawY);
          ctx.arc(drawX, drawY, rad, 0, TWO_PI);
        }
      }
      ctx.fill();

      requestAnimationFrame(tick);
    }

    doResize();
    window.addEventListener('resize', onResize);
    window.addEventListener('mousemove', onMouseMove, { passive: true });
    requestAnimationFrame(tick);
  }

  function init() {
    var layer = document.querySelector('.bg-layer');
    if (layer) mountDotField(layer);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
