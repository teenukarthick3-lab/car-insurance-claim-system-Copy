/* Car Insure — AI damage analysis viewer (claims team). The original image is never altered:
   boxes are an HTML layer on top of it, and "Original" simply hides that layer. */
(function () {
  'use strict';

  // Refresh automatically while the analysis is running (max ~3 minutes).
  if (document.querySelector('[data-pending]')) {
    var tries = Number(sessionStorage.getItem('da-refresh') || 0);
    if (tries < 36) {
      setTimeout(function () {
        try { sessionStorage.setItem('da-refresh', String(tries + 1)); } catch (e) {}
        location.reload();
      }, 5000);
    }
  } else {
    try { sessionStorage.removeItem('da-refresh'); } catch (e) {}
  }

  var viewer = document.querySelector('[data-damage-viewer]');
  if (!viewer) return;
  var tabs = viewer.querySelectorAll('.da-tab');
  var stages = viewer.querySelectorAll('.da-stage');
  var modeBtns = viewer.querySelectorAll('.da-mode-btn');

  function closePops() {
    viewer.querySelectorAll('.da-pop').forEach(function (p) { p.hidden = true; });
    viewer.querySelectorAll('.da-box.selected').forEach(function (b) { b.classList.remove('selected'); });
  }

  tabs.forEach(function (tab) {
    tab.addEventListener('click', function () {
      tabs.forEach(function (t) { t.classList.toggle('active', t === tab); t.setAttribute('aria-selected', t === tab); });
      stages.forEach(function (s) { s.classList.toggle('active', s.id === tab.dataset.target); });
      closePops();
    });
  });

  modeBtns.forEach(function (btn) {
    btn.addEventListener('click', function () {
      var analysis = btn.dataset.mode === 'analysis';
      modeBtns.forEach(function (b) { b.classList.toggle('active', b === btn); b.setAttribute('aria-pressed', b === btn); });
      viewer.querySelectorAll('.da-frame').forEach(function (f) { f.classList.toggle('show-overlay', analysis); });
      closePops();
    });
  });

  viewer.querySelectorAll('.da-box').forEach(function (box) {
    box.addEventListener('click', function (e) {
      e.stopPropagation();
      var frame = box.closest('.da-frame');
      var pop = frame.querySelector('.da-pop');
      var wasOpen = !pop.hidden && box.classList.contains('selected');
      closePops();
      if (wasOpen) return;
      ['label', 'confidence', 'location', 'severity', 'cost', 'status', 'reason'].forEach(function (k) {
        var el = pop.querySelector('[data-k="' + k + '"]');
        el.textContent = box.dataset[k] || '';
        el.hidden = !box.dataset[k];
      });
      box.classList.add('selected');
      pop.hidden = false;
      // Place the card next to the box, kept inside the image.
      var fr = frame.getBoundingClientRect(), br = box.getBoundingClientRect();
      var pw = pop.offsetWidth, ph = pop.offsetHeight;
      var left = br.right - fr.left + 10;
      if (left + pw > fr.width) left = br.left - fr.left - pw - 10;
      if (left < 6) left = Math.max(6, Math.min(fr.width - pw - 6, br.left - fr.left));
      var top = Math.max(6, Math.min(fr.height - ph - 6, br.top - fr.top));
      pop.style.left = left + 'px';
      pop.style.top = top + 'px';
    });
  });

  viewer.querySelectorAll('.da-pop-x').forEach(function (x) { x.addEventListener('click', closePops); });
  document.addEventListener('click', function (e) { if (!e.target.closest('.da-pop')) closePops(); });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closePops(); });
})();
