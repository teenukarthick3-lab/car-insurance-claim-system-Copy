/* Car Insure — evidence lightbox for the claim review page. */
(function () {
  'use strict';
  var items = window.GALLERY || [];
  var box = document.getElementById('lightbox');
  if (!box || !items.length) return;

  var img = document.getElementById('lb-img');
  var title = document.getElementById('lb-title');
  var count = document.getElementById('lb-count');
  var dl = document.getElementById('lb-download');
  var current = 0;
  var lastFocus = null;

  function show(i) {
    current = (i + items.length) % items.length;
    var it = items[current];
    img.classList.remove('zoomed');
    img.src = it.src;
    img.alt = it.title;
    title.textContent = it.title;
    count.textContent = (current + 1) + ' of ' + items.length;
    dl.href = it.dl;
  }

  function open(i) {
    lastFocus = document.activeElement;
    show(i);
    box.classList.add('open');
    document.body.style.overflow = 'hidden';
    document.getElementById('lb-close').focus();
  }

  function close() {
    box.classList.remove('open');
    document.body.style.overflow = '';
    if (lastFocus) lastFocus.focus();
  }

  document.querySelectorAll('.thumb[data-index]').forEach(function (btn) {
    btn.addEventListener('click', function () { open(parseInt(btn.dataset.index, 10)); });
  });
  document.getElementById('lb-close').addEventListener('click', close);
  document.getElementById('lb-prev').addEventListener('click', function () { show(current - 1); });
  document.getElementById('lb-next').addEventListener('click', function () { show(current + 1); });
  img.addEventListener('click', function () { img.classList.toggle('zoomed'); });
  box.addEventListener('click', function (e) {
    if (e.target === box || e.target.classList.contains('lb-img-wrap') || e.target.classList.contains('lb-stage')) close();
  });
  document.addEventListener('keydown', function (e) {
    if (!box.classList.contains('open')) return;
    if (e.key === 'Escape') close();
    else if (e.key === 'ArrowLeft') show(current - 1);
    else if (e.key === 'ArrowRight') show(current + 1);
  });
})();
