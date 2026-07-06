"use strict";
// zoopipe — one screen: pick photos per species while the worker processes
// earlier picks in the background. All rendering of a card's photos happens
// here from the PHOTOS map; the server only ships card shells + initial data.

const CSV = document.body.dataset.csv;
const PHOTOS = JSON.parse(document.getElementById("photos-data").textContent || "{}");
const grid = document.getElementById("grid");
let pollTimer = null;

const post = (url, data) =>
  fetch(url, { method: "POST", body: new URLSearchParams(data) });

// ---- render one photo -------------------------------------------------------
function photoEl(p) {
  const fig = document.createElement("figure");
  fig.className = "ph s-" + p.status + (p.uploaded ? " up" : "");
  fig.dataset.photo = p.id;

  const rel = p.frame || p.orig;
  if (rel) {
    const img = document.createElement("img");
    img.src = "/out/" + rel + (p._v ? "?v=" + p._v : "");
    img.loading = "lazy";
    fig.appendChild(img);
  }
  if (p.status === "ready" || p.status === "processing") {
    const s = document.createElement("span");
    s.className = "phspin";
    fig.appendChild(s);
  }

  const bar = document.createElement("div");
  bar.className = "pha";
  const btn = (label, act, cls) =>
    `<button type="button" data-act="${act}" data-id="${p.id}" class="${cls || ""}">${label}</button>`;

  if (p.status === "done" && p.frame) {
    bar.innerHTML =
      `<button type="button" data-act="view" data-src="/out/${p.frame}">view</button>` +
      btn("edit", "edit") +
      (p.uploaded ? `<span class="okmark">uploaded ✓</span>` : btn("upload", "upload", "prim")) +
      btn("✕", "delete", "x");
  } else if (p.status === "error") {
    bar.innerHTML =
      `<span class="errtxt" title="${(p.notes || "").replace(/"/g, "'")}">failed</span>` +
      btn("retry", "retry") + btn("✕", "delete", "x");
  } else {
    bar.innerHTML = `<span class="wtxt">processing…</span>` + btn("✕", "delete", "x");
  }
  fig.appendChild(bar);
  return fig;
}

// ---- render a card's frame strip --------------------------------------------
function cardState(list) {
  if (!list || !list.length) return "todo";
  if (list.some((p) => p.uploaded)) return "uploaded";
  if (list.some((p) => p.status === "done")) return "done";
  if (list.some((p) => p.status === "ready" || p.status === "processing")) return "working";
  if (list.some((p) => p.status === "error")) return "error";
  return "todo";
}

function renderCard(card) {
  if (!card) return;
  const idpr = card.dataset.idpr;
  const list = PHOTOS[idpr] || [];
  const frames = card.querySelector("[data-frames]");
  frames.innerHTML = "";
  list.forEach((p) => frames.appendChild(photoEl(p)));

  const st = cardState(list);
  card.dataset.state = st;
  card.className = "card state-" + st;
  const sb = card.querySelector("[data-search]");
  sb.textContent = list.length ? "Search again" : "Search ▸";
}

function signature(idpr) {
  return (PHOTOS[idpr] || [])
    .map((p) => p.id + p.status + (p.frame || "") + (p.uploaded ? "u" : "") + (p._v || ""))
    .join(",");
}

// ---- live "N processing" pill (whole CSV, from the last poll) ----------------
function recount() {
  let inflight = 0;
  Object.values(PHOTOS).forEach((l) =>
    l.forEach((p) => { if (p.status === "ready" || p.status === "processing") inflight++; }));
  const pill = document.getElementById("pill");
  if (inflight) {
    pill.hidden = false;
    document.getElementById("pill-txt").textContent = inflight + " processing";
  } else pill.hidden = true;
}

// ---- live nav-bar totals (whole CSV, from the server) -----------------------
// The tab counts and progress bar span the whole CSV, so we can't recompute
// them from PHOTOS (only the current page is loaded). Refetch from the server.
function applyCounts(counts) {
  if (!counts) return;
  document.querySelectorAll("[data-count]").forEach((b) => {
    const key = b.dataset.count;
    if (counts[key] != null) b.textContent = counts[key];
  });
  const span = document.getElementById("progspan");
  const txt = document.getElementById("progtxt");
  const prog = document.getElementById("prog");
  if (span) span.style.width = (counts.all ? counts.uploaded / counts.all * 100 : 0) + "%";
  if (txt) txt.textContent = counts.uploaded + "/" + counts.all + " uploaded";
  if (prog) prog.title = counts.uploaded + " of " + counts.all + " species uploaded";
}

async function refreshCounts() {
  try {
    applyCounts(await (await fetch("/counts?csv=" + encodeURIComponent(CSV))).json());
  } catch (e) { /* leave stale numbers rather than blank them */ }
}

// ---- search panel -----------------------------------------------------------
// The top "Search"/"Search again" button opens the panel (if needed) and runs
// a fresh search on every click.
function searchCard(card) {
  const panel = card.querySelector("[data-panel]");
  panel.hidden = false;
  if (!panel.dataset.loaded) {
    panel.innerHTML =
      `<div class="presults muted">searching…</div>` +
      `<button type="button" class="pmore" hidden>Search more</button>` +
      `<div class="pfoot"><button type="button" class="pproc" disabled>Process</button>` +
      `<span class="pcount"></span></div>`;
    panel.dataset.loaded = "1";
  }
  doSearch(card, false);
  card.scrollIntoView({ behavior: "smooth", block: "start" });
}

function resTile(res) {
  const t = document.createElement("figure");
  t.className = "res";
  t.dataset.url = res.image;
  t.innerHTML =
    `<img src="${res.thumb}" loading="lazy" alt="">` +
    `<button type="button" class="zoom" data-src="${res.image}" title="view full">⛶</button>` +
    `<figcaption>${res.w || "?"}×${res.h || "?"}` +
    (res.url ? ` · <a class="src" href="${res.url}" target="_blank" rel="noopener">${res.source || "source"} ↗</a>` : "") +
    `</figcaption>`;
  return t;
}

const cardQuery = (card) => (card.dataset.latin || card.dataset.name || "").trim();

// One search routine for both the first search and "Search more". DDG only
// takes a result count (no offset), so "more" refetches a bigger page and
// appends the tiles we don't already show; the first search replaces the box.
async function doSearch(card, append) {
  const panel = card.querySelector("[data-panel]");
  const box = panel.querySelector(".presults");
  const more = panel.querySelector(".pmore");
  const want = append ? (+panel.dataset.max || 20) + 20 : 20;
  panel.dataset.max = want;

  if (append) { more.disabled = true; more.textContent = "loading…"; }
  else { more.hidden = true; box.className = "presults muted"; box.textContent = "searching…"; }

  try {
    const data =
      await (await fetch("/search?q=" + encodeURIComponent(cardQuery(card)) + "&max=" + want)).json();
    if (!append && (data.error || !data.results.length)) {
      box.textContent = data.error || "no images";
      return;
    }
    const have = new Set([...box.querySelectorAll(".res")].map((e) => e.dataset.url));
    if (!append) { box.className = "presults"; box.innerHTML = ""; have.clear(); }
    let added = 0;
    (data.results || []).forEach((res) => {
      if (!have.has(res.image)) { box.appendChild(resTile(res)); added++; }
    });
    more.hidden = false;
    if (append && !added) { more.disabled = true; more.textContent = "no more results"; }
    else { more.disabled = false; more.textContent = "Search more"; }
    updatePicked(card);
  } catch (e) {
    if (append) { more.disabled = false; more.textContent = "Search more"; }
    else box.textContent = "search failed";
  }
}

function updatePicked(card) {
  const panel = card.querySelector("[data-panel]");
  const n = panel.querySelectorAll(".res.picked").length;
  panel.querySelector(".pcount").textContent = n ? n + " picked" : "";
  panel.querySelector(".pproc").disabled = !n;
}

async function processCard(card) {
  const panel = card.querySelector("[data-panel]");
  const urls = [...panel.querySelectorAll(".res.picked")].map((e) => e.dataset.url);
  if (!urls.length) return;
  const idpr = card.dataset.idpr;
  const body = new URLSearchParams();
  body.set("csv", CSV);
  body.set("idpr", idpr);
  urls.forEach((u) => body.append("url", u));
  const data = await (await fetch("/process", { method: "POST", body })).json();
  if (!data.ok) { alert(data.error || "process failed"); return; }
  const list = (PHOTOS[idpr] = PHOTOS[idpr] || []);
  data.ids.forEach((id) => list.push({ id, idpr, status: "ready", frame: "", orig: "" }));
  panel.hidden = true;
  panel.dataset.loaded = "";
  panel.innerHTML = "";
  renderCard(card);
  card.dataset.sig = signature(idpr);
  card.scrollIntoView({ block: "nearest" });   // panel just closed — keep card in view
  recount();
  refreshCounts();
  ensurePolling();
}

// ---- live status polling ----------------------------------------------------
async function poll() {
  let data;
  try {
    data = await (await fetch("/status?csv=" + encodeURIComponent(CSV))).json();
  } catch (e) { return; }

  // notify on any photo that just crossed into done (was in flight last poll)
  data.photos.forEach((p) => {
    const was = prevStatus[p.id];
    if (p.status === "done" && was && was !== "done" && !notified.has(p.id)) {
      notified.add(p.id);
      toastDone(p);
    }
    prevStatus[p.id] = p.status;
  });

  const byIdpr = {};
  data.photos.forEach((p) => (byIdpr[p.idpr] = byIdpr[p.idpr] || []).push(p));
  // carry over local cache-bust versions so freshly-edited frames don't flash
  const prevV = {};
  Object.values(PHOTOS).forEach((l) => l.forEach((p) => { if (p._v) prevV[p.id] = p._v; }));

  const touched = new Set(Object.keys(PHOTOS));
  Object.keys(PHOTOS).forEach((k) => delete PHOTOS[k]);
  Object.entries(byIdpr).forEach(([idpr, l]) => {
    l.forEach((p) => { if (prevV[p.id]) p._v = prevV[p.id]; });
    PHOTOS[idpr] = l;
    touched.add(idpr);
  });

  touched.forEach((idpr) => {
    const card = grid.querySelector(`.card[data-idpr="${CSS.escape(idpr)}"]`);
    if (card && card.dataset.sig !== signature(idpr)) {
      renderCard(card);
      card.dataset.sig = signature(idpr);
    }
  });
  recount();
  applyCounts(data.counts);
  if (data.active) ensurePolling();
  else if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}
function ensurePolling() {
  if (!pollTimer) pollTimer = setInterval(poll, 1500);
}

// ---- completion toasts ------------------------------------------------------
const prevStatus = {};        // photo id -> status seen on the previous poll
const notified = new Set();   // photo ids we've already toasted
const toasts = document.getElementById("toasts");

function jumpToCard(idpr) {
  const card = grid.querySelector(`.card[data-idpr="${CSS.escape(idpr)}"]`);
  if (card) {
    card.scrollIntoView({ behavior: "smooth", block: "start" });
    card.classList.remove("flash"); void card.offsetWidth; card.classList.add("flash");
  } else {
    // it's on another page/filter — load the Done view anchored to it
    location.href = `/?csv=${encodeURIComponent(CSV)}&filter=done#card-${idpr}`;
  }
}

function toastDone(p) {
  const t = document.createElement("button");
  t.className = "toast";
  t.innerHTML =
    (p.frame ? `<img src="/out/${p.frame}" alt="">` : "") +
    `<span class="tmsg"><b>${p.species || "Photo"}</b> ready<small>click to view</small></span>`;
  t.onclick = () => { jumpToCard(p.idpr); t.remove(); };
  toasts.appendChild(t);
  setTimeout(() => { t.classList.add("out"); setTimeout(() => t.remove(), 400); }, 6000);
}

// ---- editor + lightbox ------------------------------------------------------
const ed = {
  dlg: document.getElementById("editor"), img: document.getElementById("ed-img"),
  rot: document.getElementById("ed-rot"), deg: document.getElementById("ed-deg"),
  flip: 0, id: null,
};
function edApply() {
  ed.deg.textContent = ed.rot.value;
  ed.img.style.transform =
    (ed.flip ? "scaleX(-1) " : "") + "rotate(" + ed.rot.value + "deg)";
}
function openEditor(p) {
  if (!p) return;
  ed.id = p.id; ed.flip = 0; ed.rot.value = 0;
  ed.img.src = "/out/" + p.frame + "?v=" + Date.now();
  edApply();
  ed.dlg.showModal();
}
ed.rot.addEventListener("input", edApply);
document.getElementById("ed-mirror").onclick = () => { ed.flip ^= 1; edApply(); };
document.getElementById("ed-cancel").onclick = () => ed.dlg.close();
document.getElementById("ed-save").onclick = async () => {
  const r = await post("/edit", { id: ed.id, rotate: ed.rot.value, flip: ed.flip ? "1" : "" });
  if ((await r.json()).ok) {
    for (const [idpr, l] of Object.entries(PHOTOS))
      for (const p of l) if (p.id === ed.id) {
        p._v = Date.now();
        const card = grid.querySelector(`.card[data-idpr="${CSS.escape(idpr)}"]`);
        renderCard(card);
        if (card) card.dataset.sig = signature(idpr);
      }
  }
  ed.dlg.close();
};

const lb = document.getElementById("lightbox");
function openLightbox(src) { lb.querySelector("img").src = src; lb.showModal(); }
lb.onclick = () => lb.close();

// ---- one delegated click handler for the whole grid -------------------------
grid.addEventListener("click", async (e) => {
  const card = e.target.closest(".card");
  if (!card) return;

  if (e.target.closest("[data-search]")) return searchCard(card);
  if (e.target.closest(".pmore")) return doSearch(card, true);
  if (e.target.closest(".pproc")) return processCard(card);

  const res = e.target.closest(".res");
  if (res) {
    if (e.target.closest(".src")) return;          // let the source link open
    if (e.target.closest(".zoom")) return openLightbox(e.target.closest(".zoom").dataset.src);
    res.classList.toggle("picked");
    return updatePicked(card);
  }

  const act = e.target.closest("[data-act]");
  if (!act) return;
  const id = +act.dataset.id;
  const findPhoto = () => {
    for (const l of Object.values(PHOTOS)) for (const p of l) if (p.id === id) return p;
  };
  const kind = act.dataset.act;
  if (kind === "view") return openLightbox(act.dataset.src);
  if (kind === "edit") return openEditor(findPhoto());
  if (kind === "upload") {
    act.disabled = true; act.textContent = "…";
    const data = await (await post("/upload", { id })).json();
    if (data.ok) { findPhoto().uploaded = true; renderCard(card); card.dataset.sig = signature(card.dataset.idpr); recount(); refreshCounts(); }
    else { act.disabled = false; act.textContent = "upload"; alert(data.error || data.result); }
    return;
  }
  if (kind === "retry") {
    await post("/retry", { id });
    findPhoto().status = "ready"; renderCard(card); recount(); ensurePolling();
    return;
  }
  if (kind === "delete") {
    await post("/delete", { id });
    PHOTOS[card.dataset.idpr] = (PHOTOS[card.dataset.idpr] || []).filter((p) => p.id !== id);
    renderCard(card); card.dataset.sig = signature(card.dataset.idpr); recount(); refreshCounts();
    return;
  }
});

// ---- boot -------------------------------------------------------------------
grid.querySelectorAll(".card").forEach((card) => {
  renderCard(card);
  card.dataset.sig = signature(card.dataset.idpr);
});
recount();
poll();   // sync the whole-CSV "processing" pill and self-start polling if active

// arriving from a "ready" toast on another page -> scroll to & flash that card
if (location.hash.startsWith("#card-")) {
  const card = document.getElementById(location.hash.slice(1));
  if (card) {
    card.scrollIntoView({ block: "start" });
    card.classList.add("flash");
  }
}

// ---- back to top ------------------------------------------------------------
const toTop = document.getElementById("totop");
window.addEventListener("scroll", () => { toTop.hidden = window.scrollY < 400; }, { passive: true });
toTop.onclick = () => window.scrollTo({ top: 0, behavior: "smooth" });
