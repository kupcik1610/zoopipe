// Collect page: per-fish selection counts, collapsible blocks, and a sidebar to
// navigate / expand them. The card click that toggles a pick lives in app.js and
// doesn't fire a 'change' event, so we recount on click (deferred) + on change.
(function () {
  var layout = document.querySelector('.collect-layout');
  if (!layout) return;
  var csv = layout.getAttribute('data-csv');

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
    // leave the button alone once it's disabled for submit -- otherwise a click
    // on Process schedules a recount that clobbers the "Downloading…" label.
    if (fab && !fab.disabled) fab.textContent = total ? ('Process ' + total + ' picked ▸') : 'Process picked images ▸';
  }

  // app.js toggles the checkbox on card click during bubbling; defer so we read
  // the post-toggle state. Also catch direct checkbox/keyboard changes.
  document.addEventListener('click', function (e) {
    if (e.target.closest('.collect-main')) setTimeout(recount, 0);
  });
  document.addEventListener('change', function (e) {
    if (e.target.classList && e.target.classList.contains('pick')) recount();
  });

  // The fish summary is position:sticky, so collapsing a block you've scrolled
  // into would leave the summary stuck up top and strand you further down the
  // page. If a just-collapsed block's summary sits above the viewport, pull it
  // back into view so you stay oriented on the block you closed.
  function keepInView(d) {
    if (!d.open && d.getBoundingClientRect().top < 0) {
      d.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }

  // sidebar: click a fish to toggle its block open/closed (+ scroll either way)
  var list = document.querySelector('.side-list');
  if (list) list.addEventListener('click', function (e) {
    var a = e.target.closest('a[data-jump]');
    if (!a) return;
    e.preventDefault();
    var d = document.getElementById('fish-' + a.getAttribute('data-jump'));
    if (!d) return;
    d.open = !d.open;
    if (d.open) d.scrollIntoView({ behavior: 'smooth', block: 'start' });
    else keepInView(d);
  });

  // collapsing a block via its own summary: correct the scroll the same way.
  // (delegated on the container since /research swaps blocks in/out; the native
  // toggle fires first, so we read d.open on the next tick.)
  var main = document.querySelector('.collect-main');
  if (main) main.addEventListener('click', function (e) {
    var s = e.target.closest('summary');
    if (!s) return;
    var d = s.closest('details.fish');
    if (d) setTimeout(function () { keepInView(d); }, 0);
  });

  // Fetch one row's results and swap the (skeleton or old) block in place.
  // Returns a promise so both the initial streaming load and the retry button
  // can share it. Order in the DOM is fixed, so blocks fill in wherever they
  // sit regardless of which search finishes first.
  function loadFish(idx) {
    var block = document.getElementById('fish-' + idx);
    // keep the "download failed -- pick another" marker across the reload
    var repick = block && block.getAttribute('data-repick') ? '&repick=1' : '';
    return fetch('/research?csv=' + encodeURIComponent(csv) + '&idx=' + idx + repick)
      .then(function (r) { return r.text(); })
      .then(function (html) {
        var tmp = document.createElement('div');
        tmp.innerHTML = html.trim();
        var fresh = tmp.firstElementChild;
        var target = document.getElementById('fish-' + idx);
        if (fresh && target) { target.replaceWith(fresh); recount(); }
      });
  }

  // Stream the batch in: work through the pending rows (in order) with a small
  // pool of concurrent requests -- fast enough to fill quickly, gentle enough
  // that DDG doesn't choke on a burst. The user can pick from the first fish
  // while the rest are still arriving.
  var POOL = 4;
  var queue = [];
  document.querySelectorAll('details.fish[data-pending]').forEach(function (d) {
    queue.push(d.getAttribute('data-idx'));
  });
  function pump() {
    if (!queue.length) return;
    var idx = queue.shift();
    loadFish(idx).catch(function () { /* /research renders its own error card */ })
      .then(pump);
  }
  for (var i = 0; i < POOL; i++) pump();

  // retry the search for a single fish (a DDG timeout shouldn't cost the batch)
  document.addEventListener('click', function (e) {
    var rb = e.target.closest('button.research');
    if (!rb) return;
    e.preventDefault(); e.stopPropagation();   // don't toggle the <details>
    var idx = rb.getAttribute('data-idx');
    rb.disabled = true; rb.textContent = 'searching…';
    loadFish(idx).catch(function () {
      rb.disabled = false; rb.textContent = '↻ retry search';
    });
  });

  // Guard the Process submit: /process downloads every picked image before it
  // redirects, so it can take a while with no visible change -- which invites a
  // second click, and each submit advances the batch cursor, silently skipping a
  // whole batch. Disable the button on first submit (and block re-entry) so the
  // form can only be sent once; relabel it so it's clear something's happening.
  var procForm = document.querySelector('form.has-fab');
  if (procForm) procForm.addEventListener('submit', function (e) {
    if (procForm.dataset.submitting) { e.preventDefault(); return; }
    var fab = procForm.querySelector('.process-fab');
    if (!fab) return;                       // no button -> let it submit natively
    procForm.dataset.submitting = '1';
    // A native submit locks the page for navigation and the browser skips
    // repainting the button change -- so hold the submit, update the button,
    // let one frame paint (double rAF), THEN submit for real.
    e.preventDefault();
    var n = procForm.querySelectorAll('input.pick:checked').length;
    fab.disabled = true;
    fab.textContent = 'Downloading ' + n + ' image' + (n === 1 ? '' : 's') + '…';
    requestAnimationFrame(function () {
      requestAnimationFrame(function () { procForm.submit(); });
    });
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
