// Refresh edited thumbnails when returning to a list page: the editor records
// {rel: timestamp} in sessionStorage; this busts the cache for those images.
addEventListener('pageshow', function () {
  var e;
  try { e = JSON.parse(sessionStorage.getItem('edited') || '{}'); } catch (_) { e = {}; }
  document.querySelectorAll('img[data-rel]').forEach(function (i) {
    var r = i.getAttribute('data-rel');
    if (e[r]) i.src = '/out/' + r + '?v=' + e[r];
  });
});

// Search results: click the image to open it large in the lightbox; click
// anywhere else on a card to toggle whether it's picked.
addEventListener('click', function (e) {
  var exp = e.target.closest('.thumb .expand[data-full]');
  if (exp) { e.preventDefault(); e.stopPropagation(); openLightbox(exp.getAttribute('data-full')); return; }
  if (e.target.closest('.thumb a')) return;            // real links keep working
  var card = e.target.closest('.thumb[data-pick]');
  if (card) {
    var cb = card.querySelector('input.pick');
    if (cb) cb.checked = !cb.checked;
  }
});

function openLightbox(src) {
  var d = document.getElementById('lightbox');
  if (!d || typeof d.showModal !== 'function') { window.open(src, '_blank'); return; }
  d.querySelector('img').src = src;
  d.showModal();
}

// Click the backdrop or the image to dismiss the lightbox (ESC also works).
(function () {
  var d = document.getElementById('lightbox');
  if (!d) return;
  d.addEventListener('click', function () { d.close(); });
  d.addEventListener('close', function () { d.querySelector('img').src = ''; });
})();

// Editor save: POST via fetch, note the edited image for the list, then go back.
function doSave(e) {
  e.preventDefault();
  var f = e.target, b = f.querySelector('button[type=submit]');
  b.disabled = true; b.textContent = 'Saving…';
  fetch(f.action, { method: 'POST', headers: { 'X-Requested-With': 'fetch' }, body: new FormData(f) })
    .then(function (r) {
      if (!r.ok) throw 0;
      var e2;
      try { e2 = JSON.parse(sessionStorage.getItem('edited') || '{}'); } catch (_) { e2 = {}; }
      e2[f.img.value] = Date.now();
      sessionStorage.setItem('edited', JSON.stringify(e2));
      history.back();
    })
    .catch(function () { b.disabled = false; b.textContent = 'Save'; alert('Save failed'); });
  return false;
}
