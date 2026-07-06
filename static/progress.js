// Live progress for a processing batch: poll /status and repaint the table
// until nothing is ready/processing, then reveal the "next batch" action.
(function () {
  var root = document.getElementById('prog');
  if (!root) return;
  var csv = root.getAttribute('data-csv');
  var batch = root.getAttribute('data-batch');
  var url = '/status?csv=' + encodeURIComponent(csv) + '&batch=' + encodeURIComponent(batch);

  var ICON = { done: '✓', processing: '⟳', ready: '•', error: '⚠' };

  // per-frame upload state, kept across repaints: jobId -> {state, text}
  // state: 'up' (in flight) | 'ok' | 'err'
  var uploaded = {};
  var jobsById = {};

  function actionsHtml(j) {
    if (j.status === 'error') {
      return '<button type="button" class="retry" data-id="' + j.id + '">↻ retry</button>';
    }
    if (!(j.status === 'done' && j.frame)) return '';
    var a = '<button type="button" class="rowbtn view" data-full="/out/' + j.frame + '">view</button>' +
            '<a class="rowbtn edit" href="/edit?img=' + encodeURIComponent(j.frame) + '">edit</a>';
    var u = uploaded[j.id];
    var label = u ? (u.state === 'up' ? '…' : (u.state === 'ok' ? '✓ uploaded' : '↻ upload')) : 'upload';
    a += '<button type="button" class="rowbtn upload" data-id="' + j.id + '"' +
         (u && u.state === 'up' ? ' disabled' : '') + '>' + label + '</button>';
    if (u && u.text) a += '<span class="up-msg ' + (u.state === 'err' ? 'err' : 'ok') + '">' + u.text + '</span>';
    return a;
  }

  function rowHtml(j) {
    var done = j.status === 'done' && j.frame;
    var img = done ? '/out/' + j.frame : '/out/' + j.orig;
    var thumb = '<img loading="lazy"' + (done ? ' data-rel="' + j.frame + '"' : '') +
                ' src="' + img + '" alt="">';
    var actions = actionsHtml(j);
    return '<td class="jt-thumb">' + thumb + '</td>' +
           '<td class="jt-name"><span class="muted">#' + j.n + '</span></td>' +
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
    var groupTot = {}, groupDone = {};
    d.jobs.forEach(function (j) {
      byId[j.id] = j;
      jobsById[j.id] = j;
      groupTot[j.slug] = (groupTot[j.slug] || 0) + 1;
      if (j.status === 'done') groupDone[j.slug] = (groupDone[j.slug] || 0) + 1;
    });
    // update existing rows in place
    Array.prototype.forEach.call(rows.querySelectorAll('tr[data-id]'), function (tr) {
      var j = byId[tr.getAttribute('data-id')];
      if (j) tr.innerHTML = rowHtml(j);
    });
    // refresh each species' done/total counter
    Array.prototype.forEach.call(rows.querySelectorAll('[data-group-count]'), function (el) {
      var s = el.getAttribute('data-group-count');
      el.textContent = (groupDone[s] || 0) + '/' + (groupTot[s] || 0);
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
    // push one finished frame to its product on minizoo
    var ub = e.target.closest('button.upload');
    if (ub) {
      var uid = ub.getAttribute('data-id');
      function repaintRow() {
        var tr = rows.querySelector('tr[data-id="' + uid + '"]');
        if (tr && jobsById[uid]) tr.querySelector('.jt-actions').innerHTML = actionsHtml(jobsById[uid]);
      }
      uploaded[uid] = { state: 'up', text: 'uploading…' };
      repaintRow();
      var ufd = new FormData(); ufd.append('csv', csv); ufd.append('id', uid);
      fetch('/upload', { method: 'POST', body: ufd })
        .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
        .then(function (res) {
          var d = res.d || {};
          if (res.ok && d.ok) {
            uploaded[uid] = { state: 'ok', text: 'nahrané ✓' };
          } else if (d.result === 'changed') {
            uploaded[uid] = { state: 'err', text: '⚠ nahrané, ale zmenené iné polia' };
          } else {
            uploaded[uid] = { state: 'err', text: d.error || d.result || 'zlyhalo' };
          }
          repaintRow();
        })
        .catch(function () {
          uploaded[uid] = { state: 'err', text: 'zlyhalo (sieť)' };
          repaintRow();
        });
      return;
    }
    // view a frame full-size in the shared lightbox (defined in app.js)
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
    if (lastErr > 0 && !confirm(lastErr + ' image(s) still errored (no clean frame). Confirm batch as done anyway?')) {
      e.preventDefault();
    }
  });

  tick();
})();
