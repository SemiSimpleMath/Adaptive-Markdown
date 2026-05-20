---
name: dark-mode-toggle
description: Fixed sun/moon button top-right; toggles a 'dark' class on body and persists in localStorage
self_contained: false
tags: [ui, theme, toggle, localStorage]
provenance:
  saved_by: agent:claude-sonnet-4-6
---

<button id="dm-toggle" type="button" aria-label="Toggle dark mode">🌙</button>
<style>
  #dm-toggle {
    position: fixed; top: 16px; right: 16px; z-index: 1000;
    width: 36px; height: 36px; border-radius: 18px;
    border: 1px solid var(--am-blockquote-border, #e4e4e7);
    background: var(--am-widget-bg, #fff); cursor: pointer;
    font-size: 16px; line-height: 1;
    box-shadow: 0 2px 6px rgba(0, 0, 0, 0.08);
  }
  body.dm-active { background: #15151c; color: #e8e8ed; }
  body.dm-active a { color: #818cf8; }
</style>
<script>
  (function () {
    const btn = document.getElementById('dm-toggle');
    if (!btn) return;
    function paint(on) {
      document.body.classList.toggle('dm-active', on);
      btn.textContent = on ? '☀' : '🌙';
    }
    let on = false;
    try { on = localStorage.getItem('am_doc_dark') === '1'; } catch (e) {}
    paint(on);
    btn.addEventListener('click', () => {
      on = !on;
      paint(on);
      try { localStorage.setItem('am_doc_dark', on ? '1' : '0'); } catch (e) {}
    });
  })();
</script>
