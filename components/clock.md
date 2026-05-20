---
name: clock
description: Analog clock SVG, ticks every second, shows local time
self_contained: true
tags: [ui, time, svg, animation]
provenance:
  saved_by: agent:claude-sonnet-4-6
---

<figure class="clock-figure">
  <svg id="clock-svg" viewBox="-50 -50 100 100" width="180" height="180" aria-label="Analog clock">
    <circle cx="0" cy="0" r="46" fill="var(--am-widget-bg, #fff)" stroke="#4f46e5" stroke-width="2"/>
    <g id="clock-ticks"></g>
    <line id="clock-hour"   x1="0" y1="0" x2="0" y2="-22" stroke="#18181b" stroke-width="2.5" stroke-linecap="round"/>
    <line id="clock-minute" x1="0" y1="0" x2="0" y2="-34" stroke="#18181b" stroke-width="1.8" stroke-linecap="round"/>
    <line id="clock-second" x1="0" y1="0" x2="0" y2="-38" stroke="#4f46e5" stroke-width="1"   stroke-linecap="round"/>
    <circle cx="0" cy="0" r="2" fill="#4f46e5"/>
  </svg>
  <figcaption id="clock-readout" style="font-family: ui-monospace, monospace; font-size: 0.85em; color: var(--am-muted, #71717a);"></figcaption>
  <style>
    figure.clock-figure { text-align: center; border: 0; padding: 0.6em 0; }
  </style>
  <script>
    (function () {
      const svg = document.getElementById('clock-svg');
      const ticks = document.getElementById('clock-ticks');
      const h = document.getElementById('clock-hour');
      const m = document.getElementById('clock-minute');
      const s = document.getElementById('clock-second');
      const r = document.getElementById('clock-readout');
      if (!svg || !ticks) return;
      // Draw hour ticks once
      for (let i = 0; i < 12; i++) {
        const a = (i / 12) * Math.PI * 2 - Math.PI / 2;
        const x1 = Math.cos(a) * 42, y1 = Math.sin(a) * 42;
        const x2 = Math.cos(a) * 46, y2 = Math.sin(a) * 46;
        const l = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        l.setAttribute('x1', x1.toFixed(2)); l.setAttribute('y1', y1.toFixed(2));
        l.setAttribute('x2', x2.toFixed(2)); l.setAttribute('y2', y2.toFixed(2));
        l.setAttribute('stroke', '#71717a'); l.setAttribute('stroke-width', '1.5');
        ticks.appendChild(l);
      }
      function frame() {
        const d = new Date();
        const hh = d.getHours() % 12, mm = d.getMinutes(), ss = d.getSeconds();
        const hourAngle = ((hh + mm / 60) / 12) * 360;
        const minAngle  = ((mm + ss / 60) / 60) * 360;
        const secAngle  = (ss / 60) * 360;
        h.setAttribute('transform', 'rotate(' + hourAngle + ')');
        m.setAttribute('transform', 'rotate(' + minAngle + ')');
        s.setAttribute('transform', 'rotate(' + secAngle + ')');
        if (r) r.textContent = d.toLocaleTimeString();
      }
      frame();
      const id = setInterval(frame, 1000);
      const fig = svg.closest('figure');
      if (window.__doc && fig) {
        window.__doc.cleanup(fig, () => clearInterval(id));
      }
    })();
  </script>
</figure>
