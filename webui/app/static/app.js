"use strict";

const LETTERS = "ABCDEFGHIJKLMNOP";
const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, cls, txt) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (txt != null) n.textContent = txt;
  return n;
};

function shuffle(arr) {
  const a = arr.slice();
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

async function getManifest() {
  const r = await fetch("/api/manifest", { cache: "no-store" });
  if (!r.ok) throw new Error("manifest load failed");
  return r.json();
}
async function getProgress() {
  const r = await fetch("/api/progress", { cache: "no-store" });
  if (!r.ok) return {};
  return r.json();
}

function toast(msg) {
  const t = el("div", "toast", msg);
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 2200);
}

/* ------------------------------------------------------------------ */
/* Routing                                                             */
/* ------------------------------------------------------------------ */
function route() {
  const h = location.hash.replace(/^#/, "");
  const m = h.match(/^\/set\/(.+)$/);
  if (m) return renderSet(decodeURIComponent(m[1]));
  return renderLanding();
}
window.addEventListener("hashchange", route);

/* ------------------------------------------------------------------ */
/* Landing                                                             */
/* ------------------------------------------------------------------ */
async function renderLanding() {
  const app = $("#app");
  app.innerHTML = "";
  let manifest, progress;
  try {
    [manifest, progress] = await Promise.all([getManifest(), getProgress()]);
  } catch (e) {
    app.appendChild(el("p", "muted", "Could not load manifest: " + e.message));
    return;
  }
  $("#site-title").textContent = manifest.title || "Wedding Upscale";
  $("#site-note").textContent = manifest.note || "";
  document.title = manifest.title || "Wedding Upscale";

  const sets = manifest.sets || [];
  const done = sets.filter((s) => progress[s.id]).length;
  app.appendChild(el("p", "muted", `${done} of ${sets.length} sets voted.`));

  if (!sets.length) {
    app.appendChild(el("p", "muted", "No sets in the manifest yet."));
    return;
  }

  for (const s of sets) {
    const card = el("div", "setcard");
    card.tabIndex = 0;
    const meta = el("div", "meta");
    meta.appendChild(el("div", "t", s.title || s.id));
    const nvar = (s.variants || []).length;
    meta.appendChild(el("div", "s", `${s.kind || "image"} · ${nvar} variant${nvar === 1 ? "" : "s"}`));
    card.appendChild(meta);

    const v = progress[s.id];
    const badge = el("span", "badge " + (v ? "done" : "todo"));
    if (v) {
      const label = labelForKey(s, v.best) || v.best;
      badge.textContent = "✓ best: " + label;
    } else {
      badge.textContent = "not voted";
    }
    card.appendChild(badge);

    const go = () => (location.hash = "#/set/" + encodeURIComponent(s.id));
    card.addEventListener("click", go);
    card.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); }
    });
    app.appendChild(card);
  }
}

function labelForKey(set, key) {
  const v = (set.variants || []).find((x) => x.key === key);
  return v ? v.label : null;
}

/* ------------------------------------------------------------------ */
/* Set comparison view                                                 */
/* ------------------------------------------------------------------ */
async function renderSet(setId) {
  const app = $("#app");
  app.innerHTML = "";
  let manifest, progress;
  try {
    [manifest, progress] = await Promise.all([getManifest(), getProgress()]);
  } catch (e) {
    app.appendChild(el("p", "muted", "Load error: " + e.message));
    return;
  }
  const sets = manifest.sets || [];
  const set = sets.find((s) => s.id === setId);
  if (!set) {
    app.appendChild(el("p", "muted", "Set not found: " + setId));
    app.appendChild(backLink());
    return;
  }

  const prev = progress[setId];

  // Header row
  const top = el("div", "row");
  top.appendChild(backLink());
  top.appendChild(el("div", "spacer"));
  const idx = sets.findIndex((s) => s.id === setId);
  top.appendChild(el("span", "hint", `Set ${idx + 1} of ${sets.length}`));
  app.appendChild(top);

  app.appendChild(el("h2", null, set.title || set.id));
  if (prev) {
    const pn = el("p", "hint", `Previously chose: ${labelForKey(set, prev.best) || prev.best}. Re-voting will append a new choice (latest wins).`);
    app.appendChild(pn);
  }
  app.appendChild(el("p", "hint", "Blind: variants shown as A/B/C in random order. Click a picture to zoom to 100% and pan. Click ‘pick’ on cards best→worst; the first is the winner."));

  // Blind randomized order
  const variants = shuffle(set.variants || []);
  const shown = variants.map((v, i) => ({ letter: LETTERS[i], key: v.key, label: v.label, file: v.file }));

  const state = { ranking: [], revealed: false };

  const grid = el("div", "grid");
  const cardByKey = {};
  shown.forEach((sv) => {
    const card = el("div", "variant");
    cardByKey[sv.key] = card;

    const head = el("div", "head");
    head.appendChild(el("span", "letter", sv.letter));
    const reveal = el("span", "reveal hidden", sv.label);
    head.appendChild(reveal);
    card.appendChild(head);

    const media = el("div", "media");
    if ((set.kind || "image") === "video") {
      const vid = document.createElement("video");
      vid.controls = true;
      vid.preload = "metadata";
      vid.src = "/assets/" + relFromFile(sv.file);
      media.style.cursor = "default";
      media.appendChild(vid);
    } else {
      const img = document.createElement("img");
      img.loading = "lazy";
      img.src = "/assets/" + relFromFile(sv.file);
      img.alt = sv.letter;
      media.appendChild(img);
      const hint = el("span", "zoomhint", "🔍 zoom");
      media.appendChild(hint);
      media.addEventListener("click", () => openLightbox(img.src, state.revealed ? `${sv.letter} — ${sv.label}` : sv.letter));
    }
    card.appendChild(media);

    const foot = el("div", "foot");
    const pill = el("span", "rankpill", "pick");
    pill.addEventListener("click", () => {
      const i = state.ranking.indexOf(sv.key);
      if (i >= 0) state.ranking.splice(i, 1);
      else state.ranking.push(sv.key);
      refresh();
    });
    foot.appendChild(pill);
    card._pill = pill;
    card._reveal = reveal;
    card.appendChild(foot);

    grid.appendChild(card);
  });
  app.appendChild(grid);

  // Submit bar
  const bar = el("div", "submitbar");
  const barWrap = el("div", "wrap");
  const note = el("textarea");
  note.id = "note";
  note.placeholder = "Optional note (e.g. ‘B: sharpest eyes, slight halo’)…";
  barWrap.appendChild(note);
  const status = el("span", "hint", "Pick a winner to enable submit.");
  barWrap.appendChild(status);
  barWrap.appendChild(el("div", "spacer"));
  const clearBtn = el("button", "btn ghost small", "clear picks");
  clearBtn.addEventListener("click", () => { state.ranking = []; refresh(); });
  barWrap.appendChild(clearBtn);
  const submit = el("button", "btn primary", "Submit & next →");
  submit.disabled = true;
  barWrap.appendChild(submit);
  bar.appendChild(barWrap);
  app.appendChild(bar);

  function refresh() {
    // reveal once at least one pick
    const shouldReveal = state.ranking.length >= 1;
    if (shouldReveal && !state.revealed) state.revealed = true;
    state.revealed = shouldReveal ? true : false;

    shown.forEach((sv) => {
      const card = cardByKey[sv.key];
      const rank = state.ranking.indexOf(sv.key);
      if (rank >= 0) {
        card._pill.textContent = rank === 0 ? "① BEST" : "#" + (rank + 1);
        card._pill.classList.add("set");
      } else {
        card._pill.textContent = "pick";
        card._pill.classList.remove("set");
      }
      card.classList.toggle("chosen", rank === 0);
      card._reveal.classList.toggle("hidden", !state.revealed);
    });

    submit.disabled = state.ranking.length < 1;
    if (state.ranking.length < 1) status.textContent = "Pick a winner to enable submit.";
    else if (state.ranking.length === 1) status.textContent = "Winner set. Optionally rank the rest.";
    else status.textContent = `Ranked ${state.ranking.length}. #1 = winner.`;
  }

  submit.addEventListener("click", async () => {
    if (state.ranking.length < 1) return;
    submit.disabled = true;
    const body = {
      set: setId,
      best: state.ranking[0],
      order: shown.map((s) => ({ letter: s.letter, key: s.key })),
    };
    if (state.ranking.length > 1) body.ranking = state.ranking.slice();
    if (note.value.trim()) body.note = note.value.trim();
    try {
      const r = await fetch("/api/vote", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok) throw new Error("HTTP " + r.status);
      toast("Saved: " + (labelForKey(set, state.ranking[0]) || state.ranking[0]));
      // advance to next unvoted set, else back to landing
      const prog2 = await getProgress();
      const next = sets.find((s) => s.id !== setId && !prog2[s.id]);
      window.scrollTo(0, 0);
      location.hash = next ? "#/set/" + encodeURIComponent(next.id) : "#/";
      if (location.hash === "#/") renderLanding();
    } catch (e) {
      toast("Save failed: " + e.message);
      submit.disabled = false;
    }
  });

  refresh();
}

function relFromFile(file) {
  // manifest 'file' is like "assets/foo/bar.png"; strip a leading assets/
  return file.replace(/^assets\//, "");
}
function backLink() {
  const a = el("a", "btn ghost small", "← all sets");
  a.href = "#/";
  return a;
}

/* ------------------------------------------------------------------ */
/* Lightbox: fit ⇄ 100% zoom + pan                                     */
/* ------------------------------------------------------------------ */
const LB = {
  box: null, stage: null, img: null, label: null,
  natW: 0, natH: 0, scale: 1, fitScale: 1, tx: 0, ty: 0, mode: "fit",
  dragging: false, sx: 0, sy: 0, otx: 0, oty: 0,
};

function lbApply() {
  LB.img.style.transform = `translate(${LB.tx}px, ${LB.ty}px) scale(${LB.scale})`;
}
function lbClamp() {
  const rect = LB.stage.getBoundingClientRect();
  const w = LB.natW * LB.scale, h = LB.natH * LB.scale;
  if (w <= rect.width) LB.tx = (rect.width - w) / 2;
  else LB.tx = Math.min(0, Math.max(rect.width - w, LB.tx));
  if (h <= rect.height) LB.ty = (rect.height - h) / 2;
  else LB.ty = Math.min(0, Math.max(rect.height - h, LB.ty));
}
function lbFit() {
  const rect = LB.stage.getBoundingClientRect();
  LB.fitScale = Math.min(rect.width / LB.natW, rect.height / LB.natH) || 1;
  LB.scale = LB.fitScale;
  LB.mode = "fit";
  lbClamp();
  lbApply();
  LB.stage.classList.add("fit");
  LB.stage.classList.remove("full");
}
function lbZoomTo(targetScale, cx, cy) {
  const rect = LB.stage.getBoundingClientRect();
  const px = cx - rect.left, py = cy - rect.top;
  const ix = (px - LB.tx) / LB.scale, iy = (py - LB.ty) / LB.scale;
  LB.scale = targetScale;
  LB.tx = px - ix * LB.scale;
  LB.ty = py - iy * LB.scale;
  LB.mode = targetScale > LB.fitScale + 1e-6 ? "full" : "fit";
  LB.stage.classList.toggle("full", LB.mode === "full");
  LB.stage.classList.toggle("fit", LB.mode === "fit");
  lbClamp();
  lbApply();
}

function openLightbox(src, label) {
  if (!LB.box) initLightbox();
  LB.label.textContent = label || "";
  LB.box.classList.remove("hidden");
  const probe = new Image();
  probe.onload = () => {
    LB.natW = probe.naturalWidth; LB.natH = probe.naturalHeight;
    LB.img.src = src;
    LB.img.style.width = LB.natW + "px";
    LB.img.style.height = LB.natH + "px";
    lbFit();
  };
  probe.src = src;
}
function closeLightbox() {
  LB.box.classList.add("hidden");
  LB.img.src = "";
}
function initLightbox() {
  LB.box = $("#lightbox");
  LB.stage = $("#lb-stage");
  LB.img = $("#lb-img");
  LB.label = $("#lb-label");
  $("#lb-close").addEventListener("click", closeLightbox);
  LB.box.addEventListener("click", (e) => { if (e.target === LB.box) closeLightbox(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !LB.box.classList.contains("hidden")) closeLightbox();
  });

  // click: toggle fit <-> 100%
  LB.stage.addEventListener("click", (e) => {
    if (LB.dragging) return;
    if (LB.mode === "fit") lbZoomTo(1, e.clientX, e.clientY);
    else lbFit();
  });
  // wheel zoom
  LB.stage.addEventListener("wheel", (e) => {
    e.preventDefault();
    const factor = e.deltaY < 0 ? 1.2 : 1 / 1.2;
    const ns = Math.min(4, Math.max(LB.fitScale, LB.scale * factor));
    lbZoomTo(ns, e.clientX, e.clientY);
  }, { passive: false });

  // drag to pan (mouse)
  LB.stage.addEventListener("mousedown", (e) => {
    if (LB.mode !== "full") return;
    LB.dragging = false; LB._down = true;
    LB.sx = e.clientX; LB.sy = e.clientY; LB.otx = LB.tx; LB.oty = LB.ty;
    LB.stage.classList.add("dragging");
  });
  window.addEventListener("mousemove", (e) => {
    if (!LB._down) return;
    if (Math.abs(e.clientX - LB.sx) + Math.abs(e.clientY - LB.sy) > 3) LB.dragging = true;
    LB.tx = LB.otx + (e.clientX - LB.sx);
    LB.ty = LB.oty + (e.clientY - LB.sy);
    lbClamp(); lbApply();
  });
  window.addEventListener("mouseup", () => {
    LB._down = false; LB.stage.classList.remove("dragging");
    setTimeout(() => (LB.dragging = false), 0);
  });

  // touch pan + pinch
  let pinch = null;
  LB.stage.addEventListener("touchstart", (e) => {
    if (e.touches.length === 1) {
      const t = e.touches[0];
      LB._down = true; LB.dragging = false;
      LB.sx = t.clientX; LB.sy = t.clientY; LB.otx = LB.tx; LB.oty = LB.ty;
    } else if (e.touches.length === 2) {
      const [a, b] = e.touches;
      pinch = { d: Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY), s: LB.scale,
                cx: (a.clientX + b.clientX) / 2, cy: (a.clientY + b.clientY) / 2 };
    }
  }, { passive: true });
  LB.stage.addEventListener("touchmove", (e) => {
    if (e.touches.length === 2 && pinch) {
      const [a, b] = e.touches;
      const d = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
      const ns = Math.min(4, Math.max(LB.fitScale, pinch.s * (d / pinch.d)));
      lbZoomTo(ns, pinch.cx, pinch.cy);
      e.preventDefault();
    } else if (e.touches.length === 1 && LB._down && LB.mode === "full") {
      const t = e.touches[0];
      LB.tx = LB.otx + (t.clientX - LB.sx);
      LB.ty = LB.oty + (t.clientY - LB.sy);
      lbClamp(); lbApply();
      e.preventDefault();
    }
  }, { passive: false });
  LB.stage.addEventListener("touchend", (e) => {
    if (e.touches.length === 0) { LB._down = false; pinch = null; }
  });
}

/* boot */
route();
