// Collect page: per-fish selection counts, collapsible blocks, and a sidebar to
// navigate / expand them. The card click that toggles a pick lives in app.js and
// doesn't fire a 'change' event, so we recount on click (deferred) + on change.
(function () {
  var layout = document.querySelector('.collect-layout');
  if (!layout) return;

  function recount() {
    var total = 0;
    document.querySelectorAll('details.fish').forEach(function (d) {
      var idx = d.getAttribute('data-idx');
      var n = d.querySelectorAll('input.pick:checked').length;
      total += n;

      var badge = d.querySelector('.sel-badge');
      if (badge) { badge.hidden = n === 0; badge.textContent = n + ' picked'; }

      var sc = document.querySelector('.side-count[data-count="' + idx + '"]');
      if (sc) {
        sc.textContent = n;
        sc.classList.toggle('has', n > 0);
        var li = sc.closest('li');
        if (li) li.classList.toggle('picked', n > 0);
      }
    });
    var t = document.getElementById('sel-total');
    if (t) t.textContent = total + ' selected';
    var fab = document.querySelector('.process-fab');
    if (fab) fab.textContent = total ? ('Process ' + total + ' picked ▸') : 'Process picked images ▸';
  }

  // app.js toggles the checkbox on card click during bubbling; defer so we read
  // the post-toggle state. Also catch direct checkbox/keyboard changes.
  document.addEventListener('click', function (e) {
    if (e.target.closest('.collect-main')) setTimeout(recount, 0);
  });
  document.addEventListener('change', function (e) {
    if (e.target.classList && e.target.classList.contains('pick')) recount();
  });

  // sidebar: click a fish to toggle its block open/closed (+ scroll when opening)
  var list = document.querySelector('.side-list');
  if (list) list.addEventListener('click', function (e) {
    var a = e.target.closest('a[data-jump]');
    if (!a) return;
    e.preventDefault();
    var d = document.getElementById('fish-' + a.getAttribute('data-jump'));
    if (!d) return;
    d.open = !d.open;
    if (d.open) d.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });

  function setAll(open) {
    document.querySelectorAll('details.fish').forEach(function (d) { d.open = open; });
  }
  var ex = document.getElementById('expand-all');
  var col = document.getElementById('collapse-all');
  if (ex) ex.addEventListener('click', function () { setAll(true); });
  if (col) col.addEventListener('click', function () { setAll(false); });

  recount();
})();
