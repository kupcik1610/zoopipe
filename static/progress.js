// Live progress for a processing batch: poll /status and repaint the table
// until nothing is ready/processing, then reveal the "next batch" action.
(function () {
  var root = document.getElementById('prog');
  if (!root) return;
  var csv = root.getAttribute('data-csv');
  var batch = root.getAttribute('data-batch');
  var url = '/status?csv=' + encodeURIComponent(csv) + '&batch=' + encodeURIComponent(batch);

  var ICON = { done: '✓', processing: '⟳', ready: '•', error: '⚠' };

  function rowHtml(j) {
    var done = j.status === 'done' && j.plate;
    var img = done ? '/out/' + j.plate : '/out/' + j.orig;
    var thumb = '<img loading="lazy"' + (done ? ' data-rel="' + j.plate + '"' : '') +
                ' src="' + img + '" alt="">';
    var actions = '';
    if (done) {
      actions = '<button type="button" class="rowbtn view" data-full="/out/' + j.plate + '">view</button>' +
                '<a class="rowbtn edit" href="/edit?img=' + encodeURIComponent(j.plate) + '">edit</a>';
    } else if (j.status === 'error') {
      actions = '<button type="button" class="retry" data-id="' + j.id + '">↻ retry</button>';
    }
    return '<td class="jt-thumb">' + thumb + '</td>' +
           '<td class="jt-name">' + j.slug + ' <span class="muted">' + j.n + '</span></td>' +
           '<td class="jt-status status-' + j.status + '">' + (ICON[j.status] || '') + ' ' + j.status + '</td>' +
           '<td class="jt-time">' + (j.secs != null ? j.secs + 's' : '—') + '</td>' +
           '<td class="jt-notes">' + (j.notes || '') + '</td>' +
           '<td class="jt-actions">' + actions + '</td>';
  }

  function paint(d) {
    var c = d.counts;
    var tot = c.total || 0, done = c.done + c.error;
    document.getElementById('sum-bar').style.width = (tot ? 100 * done / tot : 0) + '%';
    document.getElementById('sum-counts').innerHTML =
      '<span class="ok">✓ <b>' + c.done + '</b> done</span>' +
      '<span>⟳ <b>' + c.processing + '</b> processing</span>' +
      '<span>• <b>' + c.ready + '</b> ready</span>' +
      '<span class="err">⚠ <b>' + c.error + '</b> error</span>';

    var rows = document.getElementById('jobrows');
    var byId = {};
    d.jobs.forEach(function (j) { byId[j.id] = j; });
    // update existing rows in place
    Array.prototype.forEach.call(rows.querySelectorAll('tr[data-id]'), function (tr) {
      var j = byId[tr.getAttribute('data-id')];
      if (j) tr.innerHTML = rowHtml(j);
    });

    var state = document.getElementById('sum-state');
    var actions = document.getElementById('sum-actions');
    var resumeBtn = document.getElementById('resume-btn');
    if (d.active) {
      // active queue but no live worker -> it died/never started; offer resume
      var stalled = !d.worker_running;
      state.textContent = stalled ? 'paused — worker not running' : 'processing…';
      if (resumeBtn) resumeBtn.hidden = !stalled;
    } else {
      state.textContent = c.error ? ('complete · ' + c.error + ' need a retry') : 'complete ✓';
      if (resumeBtn) resumeBtn.hidden = true;
      document.getElementById('sumstrip').classList.add('done');
      actions.hidden = false;
    }
    return d.active;
  }

  var lastErr = 0;

  function tick() {
    fetch(url).then(function (r) { return r.json(); }).then(function (d) {
      lastErr = d.counts.error;
      if (paint(d)) setTimeout(tick, 2000);
    }).catch(function () { setTimeout(tick, 4000); });
  }

  document.addEventListener('click', function (e) {
    // retry an errored job, then resume polling
    var b = e.target.closest('button.retry');
    if (b) {
      var fd = new FormData();
      fd.append('csv', csv); fd.append('batch', batch); fd.append('id', b.getAttribute('data-id'));
      b.disabled = true; b.textContent = '…';
      fetch('/retry', { method: 'POST', body: fd }).then(function () {
        document.getElementById('sumstrip').classList.remove('done');
        document.getElementById('sum-actions').hidden = true;
        setTimeout(tick, 600);
      });
      return;
    }
    // view a plate full-size in the shared lightbox (defined in app.js)
    var v = e.target.closest('.view[data-full]');
    if (v) {
      e.preventDefault();
      var src = v.getAttribute('data-full');
      if (window.openLightbox) window.openLightbox(src); else window.open(src, '_blank');
      return;
    }
    // relaunch a dead/never-started worker for the remaining queue
    var rb = e.target.closest('#resume-btn');
    if (rb) {
      var fd = new FormData(); fd.append('csv', csv);
      rb.disabled = true; rb.textContent = 'starting…';
      fetch('/resume', { method: 'POST', body: fd }).then(function () {
        rb.disabled = false; rb.textContent = 'Resume processing ▸'; rb.hidden = true;
        setTimeout(tick, 800);
      });
    }
  });

  // confirming a batch with still-errored images asks first
  var cform = document.querySelector('.confirm-form');
  if (cform) cform.addEventListener('submit', function (e) {
    if (lastErr > 0 && !confirm(lastErr + ' image(s) still errored (no clean plate). Confirm batch as done anyway?')) {
      e.preventDefault();
    }
  });

  tick();
})();
