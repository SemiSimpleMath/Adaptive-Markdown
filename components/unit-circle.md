---
name: unit-circle
description: Animated unit circle with a point tracing it; angle and cos/sin readout
self_contained: true
tags: [math, canvas, animation, trig]
provenance:
  origin_doc: intro
  saved_by: agent:claude-sonnet-4-6
---

<figure class="unit-circle">
  <canvas id="unit-circle-canvas" width="320" height="320"></canvas>
  <div class="readout" id="unit-circle-readout"></div>
  <style>
    figure.unit-circle { text-align: center; padding: 0.8em 0; border: 0; }
    figure.unit-circle canvas { background: var(--am-widget-bg, #fff); border-radius: 6px; max-width: 100%; }
    figure.unit-circle .readout {
      font-family: ui-monospace, monospace; font-size: 0.85em;
      color: var(--am-muted, #71717a); margin-top: 0.5em;
    }
  </style>
  <script>
    (function () {
      const c = document.getElementById('unit-circle-canvas');
      const r = document.getElementById('unit-circle-readout');
      if (!c || !r) return;
      const ctx = c.getContext('2d');
      const W = c.width, H = c.height;
      const cx = W / 2, cy = H / 2, R = Math.min(W, H) * 0.4;
      let t = 0, raf = null;
      function frame() {
        ctx.clearRect(0, 0, W, H);
        // axes
        ctx.strokeStyle = '#d4d4d8'; ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(0, cy); ctx.lineTo(W, cy);
        ctx.moveTo(cx, 0); ctx.lineTo(cx, H);
        ctx.stroke();
        // circle
        ctx.strokeStyle = '#4f46e5'; ctx.lineWidth = 2;
        ctx.beginPath(); ctx.arc(cx, cy, R, 0, Math.PI * 2); ctx.stroke();
        // angle wedge
        const cos = Math.cos(t), sin = Math.sin(t);
        const px = cx + R * cos, py = cy - R * sin;
        ctx.strokeStyle = '#a1a1aa'; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(px, py); ctx.stroke();
        // point
        ctx.fillStyle = '#4f46e5';
        ctx.beginPath(); ctx.arc(px, py, 5, 0, Math.PI * 2); ctx.fill();
        // readout
        r.textContent = 'θ = ' + t.toFixed(2)
          + '  cos θ = ' + cos.toFixed(2)
          + '  sin θ = ' + sin.toFixed(2);
        t += 0.015;
        if (t > Math.PI * 2) t -= Math.PI * 2;
        raf = requestAnimationFrame(frame);
      }
      frame();
      const fig = c.closest('figure');
      if (window.__doc && fig) {
        window.__doc.cleanup(fig, () => { if (raf) cancelAnimationFrame(raf); });
      }
    })();
  </script>
</figure>
