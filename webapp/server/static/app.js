"use strict";

const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";
const toast = document.getElementById("toast");

function notify(message, error = false) {
  if (!toast) return;
  toast.textContent = message;
  toast.className = `toast visible${error ? " error" : ""}`;
  window.setTimeout(() => { toast.className = "toast"; }, 3500);
}

async function api(path, options = {}) {
  const method = options.method || "GET";
  const headers = {Accept: "application/json", ...(options.headers || {})};
  if (method !== "GET" && method !== "HEAD") headers["X-CSRF-Token"] = csrf;
  if (options.body && typeof options.body !== "string") {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, {...options, method, headers});
  const data = response.headers.get("content-type")?.includes("json") ? await response.json() : null;
  if (!response.ok) throw new Error(data?.error || `${response.status} ${response.statusText}`);
  return data;
}

function formatTime(ms) {
  const total = Math.max(0, Math.round(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h ? `${h}:${String(m).padStart(2,"0")}:${String(s).padStart(2,"0")}` : `${m}:${String(s).padStart(2,"0")}`;
}

function parseTime(value) {
  const text = value.trim();
  if (/^\d+(\.\d+)?$/.test(text)) return Number(text);
  const parts = text.split(":").map(Number);
  if (parts.some(Number.isNaN) || parts.length < 2 || parts.length > 3) throw new Error("Use seconds or HH:MM:SS timecode");
  if (parts.some(part => part < 0) || parts.slice(1).some(part => part >= 60)) throw new Error("Invalid timecode");
  return parts.reduce((total, part) => total * 60 + part, 0);
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function loadLibrary() {
  const root = document.getElementById("library");
  const rescan = document.getElementById("rescan");
  rescan?.addEventListener("click", async () => {
    rescan.disabled = true;
    rescan.textContent = "Scanning read-only…";
    try { await api("/api/discs/scan", {method: "POST"}); notify("Both ISOs scanned successfully"); await loadLibrary(); }
    catch (error) { notify(error.message, true); }
    finally { rescan.disabled = false; rescan.textContent = "Re-scan both ISOs"; }
  }, {once: true});
  try {
    const discs = await api("/api/discs");
    root.replaceChildren();
    if (!discs.length) { root.append(el("div", "empty-state", "No discs are cataloged yet. Use re-scan to inventory the configured ISOs.")); return; }
    for (const disc of discs) {
      const payload = await api(`/api/discs/${disc.id}/titles`);
      const card = el("article", "disc-card");
      const header = el("div", "disc-card-header");
      const heading = el("div");
      heading.append(el("p", "eyebrow", disc.slug.toUpperCase()), el("h2", "", disc.label || disc.source_filename));
      heading.append(el("span", `status ${disc.scan_status === "failed" ? "failed" : ""}`, disc.scan_status));
      header.append(heading, el("span", "disc-number", disc.slug.replace("dvd", "0")));
      const stats = el("div", "disc-stats");
      for (const [value, label, cls] of [
        [disc.title_count,"titles"],
        [disc.chapter_count,"chapters"],
        [`${disc.proxies_ready}/${disc.chapter_count}`,"proxies ready"],
        [`${disc.chapters_restored}/${disc.chapter_count}`,"restored", disc.chapters_restored ? (disc.chapters_restored === disc.chapter_count ? "stat-done" : "stat-partial") : ""],
      ]) {
        const cell = el("div", cls || ""); cell.append(el("strong", "", value), el("span", "", label)); stats.append(cell);
      }
      const titles = el("div");
      for (const title of payload.titles) {
        const row = el("div", "title-row");
        const link = el("a", "", `Title ${String(title.title_number).padStart(2,"0")}`); link.href = `/titles/${title.id}`;
        const left = el("div"); left.append(link, el("small", "", `${formatTime(title.duration_ms)} · ${title.chapter_count} chapters · ${title.video.aspect || "unknown aspect"}`));
        const badges = el("div");
        if (title.chapter_count) {
          const cls = title.chapters_restored === title.chapter_count ? "all" : title.chapters_restored ? "some" : "none";
          badges.append(el("span", `badge restored-count ${cls}`, `${title.chapters_restored}/${title.chapter_count} restored`));
        }
        if (title.likely_menu) badges.append(el("span", "badge", "likely menu"));
        if (title.likely_duplicate) badges.append(el("span", "badge", "possible duplicate"));
        if (title.likely_short) badges.append(el("span", "badge", "very short"));
        row.append(left, badges); titles.append(row);
      }
      card.append(header, stats, titles); root.append(card);
    }
  } catch (error) { root.replaceChildren(el("div", "empty-state", error.message)); notify(error.message, true); }
}

function chapterCard(chapter, selectionChanged) {
  const card = el("article", "chapter-card");
  const status = chapter.restoration || {};
  const activeWorking = ["preparing","running","assembling","cancel_requested"].includes(status.active_state);
  const thumb = el("div", "chapter-thumb");
  const flags = el("div", "restore-flags");
  if (status.restored) {
    card.classList.add("restored");
    flags.append(el("span", "restore-flag done", "✓ Restored"));
  }
  if (status.active_state) {
    card.classList.add(activeWorking ? "in-progress" : "in-queue");
    flags.append(el("span", `restore-flag ${activeWorking ? "working" : "waiting"}`,
      activeWorking ? `Restoring ${status.active_progress ?? 0}%` : "In queue"));
  }
  if (!status.restored && !status.active_state) {
    if (status.last_state === "failed") flags.append(el("span", "restore-flag failed", "Failed"));
    else flags.append(el("span", "restore-flag todo", "Not restored"));
  }
  thumb.append(flags);
  if (chapter.thumbnail_artifact_id) { const image = el("img"); image.src = `/media/${chapter.thumbnail_artifact_id}`; image.alt = ""; image.loading = "lazy"; thumb.append(image); }
  else thumb.append(el("div", "proxy-missing", "Thumbnail not generated"));
  const checkbox = el("input", "select-check"); checkbox.type = "checkbox"; checkbox.ariaLabel = `Select chapter ${chapter.chapter_number}`;
  checkbox.addEventListener("change", () => { card.classList.toggle("selected", checkbox.checked); selectionChanged(); });
  thumb.append(checkbox);
  if (chapter.proxy_artifact_id) {
    const play = el("button", "play-button", "▶"); play.type = "button"; play.ariaLabel = `Play chapter ${chapter.chapter_number}`;
    play.addEventListener("click", () => {
      const video = document.getElementById("chapter-player");
      video.src = `/media/${chapter.proxy_artifact_id}`; video.classList.add("visible");
      document.getElementById("player-empty").classList.add("hidden");
      document.getElementById("now-playing").textContent = chapter.user_label || chapter.generated_label;
      video.play(); document.querySelector(".player-panel").scrollIntoView({behavior: "smooth", block: "center"});
    });
    thumb.append(play);
  }
  const content = el("div", "chapter-content");
  // ~55 GPU-minutes per footage-minute on the local pipeline (VAE-compile path).
  const gpuHours = chapter.duration_ms * 55 / 3_600_000;
  const top = el("div", "chapter-topline"); top.append(el("span", "", `Chapter ${String(chapter.chapter_number).padStart(2,"0")}`), el("span", "", `${formatTime(chapter.duration_ms)} · ≈${gpuHours < 10 ? gpuHours.toFixed(1) : Math.round(gpuHours)}h GPU`));
  const name = el("input", "chapter-name"); name.value = chapter.user_label || chapter.generated_label; name.maxLength = 120; name.ariaLabel = "Chapter name";
  const fields = el("div", "chapter-fields");
  const priorityLabel = el("label", "", "Priority"); const select = el("select");
  for (const value of ["high","normal","low","skip"]) { const option = el("option", "", value); option.value = value; option.selected = chapter.priority === value; select.append(option); }
  priorityLabel.append(select);
  const noteLabel = el("label", "", "Notes"); const notes = el("textarea"); notes.value = chapter.notes; notes.maxLength = 2000; notes.placeholder = "Ceremony, speeches, exposure…"; noteLabel.append(notes);
  fields.append(priorityLabel, noteLabel);
  let timer;
  const save = () => { clearTimeout(timer); timer = setTimeout(async () => {
    try { await api(`/api/chapters/${chapter.id}`, {method: "PATCH", body: {name: name.value, priority: select.value, notes: notes.value}}); notify("Chapter details saved"); }
    catch (error) { notify(error.message, true); }
  }, 450); };
  name.addEventListener("change", save); select.addEventListener("change", save); notes.addEventListener("change", save);
  content.append(top, name, fields); card.append(thumb, content);
  card.chapterId = chapter.id; card.checkbox = checkbox; card.restored = !!status.restored;
  return card;
}

async function loadTitle() {
  const titleId = document.body.dataset.titleId;
  try {
    const payload = await api(`/api/titles/${titleId}/chapters`);
    const title = payload.title;
    document.getElementById("title-kicker").textContent = `${title.disc_slug.toUpperCase()} · DVD title ${String(title.title_number).padStart(2,"0")}`;
    document.getElementById("title-name").textContent = `${title.disc_label} — Title ${title.title_number}`;
    const restoredCount = payload.chapters.filter(ch => ch.restoration?.restored).length;
    document.getElementById("title-meta").textContent = `${formatTime(title.duration_ms)} · ${payload.chapters.length} chapters · ${restoredCount}/${payload.chapters.length} restored · ${title.video.format} ${title.video.width}×${title.video.height} · ${title.video.aspect}`;
    const grid = document.getElementById("chapters"); const queueButton = document.getElementById("queue-selected"); const count = document.getElementById("selected-count");
    const updateSelection = () => { const selected = [...grid.querySelectorAll(".select-check:checked")]; count.textContent = selected.length; queueButton.disabled = !selected.length; };
    const sortSelect = document.getElementById("chapter-sort");
    const renderChapters = () => {
      const chapters = [...payload.chapters];
      if (sortSelect.value === "shortest") chapters.sort((a, b) => a.duration_ms - b.duration_ms);
      else if (sortSelect.value === "longest") chapters.sort((a, b) => b.duration_ms - a.duration_ms);
      grid.replaceChildren(...chapters.map(ch => chapterCard(ch, updateSelection)));
      updateSelection();
    };
    sortSelect.addEventListener("change", renderChapters);
    renderChapters();
    queueButton.addEventListener("click", async () => {
      const selected = [...grid.querySelectorAll(".chapter-card")].filter(card => card.checkbox.checked);
      const ids = selected.map(card => card.chapterId);
      const alreadyDone = selected.filter(card => card.restored).length;
      if (alreadyDone && !confirm(`${alreadyDone} of the selected chapter(s) ${alreadyDone === 1 ? "is" : "are"} ALREADY RESTORED. Restoring again will redo hours of GPU work. Queue anyway?`)) return;
      try { const result = await api("/api/jobs", {method: "POST", body: {chapter_ids: ids}}); notify(`${result.created.length} chapter job(s) queued`); window.location.href = "/queue"; }
      catch (error) { notify(error.message, true); }
    });
    const form = document.getElementById("slice-form");
    form.addEventListener("submit", async event => {
      event.preventDefault(); const formData = new FormData(form);
      try {
        const start = parseTime(formData.get("start")); const end = parseTime(formData.get("end"));
        const result = await api("/api/slices", {method: "POST", body: {title_id: Number(titleId), start_seconds: start, end_seconds: end, name: formData.get("name"), note: formData.get("note"), priority: formData.get("priority")}});
        await api("/api/jobs", {method: "POST", body: {target_type: "slice", target_id: result.slice.id}});
        notify(result.duplicate ? "Existing slice added to queue" : "Slice created and queued"); window.location.href = "/queue";
      } catch (error) { notify(error.message, true); }
    });
  } catch (error) { notify(error.message, true); }
}

function queueCard(job) {
  const active = ["preparing","running","assembling","cancel_requested"].includes(job.state);
  const card = el("article", `queue-card${active ? " active" : ""}`);
  const identity = el("div"); identity.append(el("span", `state-pill ${job.state}`, job.state.replaceAll("_"," ")));
  const heading = el("h2"); const link = el("a", "", job.display_name); link.href = `/jobs/${job.public_id}`; heading.append(link); identity.append(heading, el("div", "queue-source", `${job.disc_slug.toUpperCase()} · title ${job.title_number} · ${formatTime(job.source_start_ms)}–${formatTime(job.source_end_ms)}`));
  const stage = el("div"); stage.append(el("p", "eyebrow", "Current stage"), el("strong", "", job.stage || (job.start_requested ? "Awaiting worker" : "Not started")), el("div", "queue-source", `${job.frames_done.toLocaleString()} / ${job.frames_total.toLocaleString()} frames`));
  const progress = el("div"); const track = el("progress", "progress-native"); track.max = 100; track.value = Math.min(100, job.progress_percent);
  const meta = el("div", "progress-meta"); meta.append(el("span", "", `${job.progress_percent}%`), el("span", "", job.eta_seconds ? `ETA ${formatTime(job.eta_seconds * 1000)}` : "ETA pending")); progress.append(track, meta);
  const actions = el("div", "queue-actions");
  const action = (label, method, path, body, cls="button small") => { const button = el("button", cls, label); button.addEventListener("click", async () => { button.disabled = true; try { await api(path, {method, body}); await loadQueue(); } catch (error) { notify(error.message, true); button.disabled = false; } }); actions.append(button); };
  if (job.can_start) action("Start", "POST", `/api/jobs/${job.public_id}/start`);
  if (job.can_retry) action("Retry", "POST", `/api/jobs/${job.public_id}/retry`);
  if (job.state === "queued") { action("↑", "POST", `/api/jobs/${job.public_id}/move`, {direction:"up"}); action("↓", "POST", `/api/jobs/${job.public_id}/move`, {direction:"down"}); }
  if (job.can_cancel && job.state !== "cancel_requested") {
    const cancel = el("button", "button small danger", active ? "Cancel after stage" : "Cancel"); cancel.addEventListener("click", async () => { if (!confirm(active ? "Finish the current pipeline stage, then cancel?" : "Cancel this queued job?")) return; try { await api(`/api/jobs/${job.public_id}/cancel`, {method:"POST"}); await loadQueue(); } catch (error) { notify(error.message,true); } }); actions.append(cancel);
  }
  card.append(identity, stage, progress, actions); return card;
}

const QUEUE_FINISHED_STATES = new Set(["completed", "cancelled"]);
let queueFilter = localStorage.getItem("queue-filter") || "active";

function initQueueFilter() {
  const toolbar = document.getElementById("queue-filter");
  if (!toolbar) return;
  const sync = () => { for (const button of toolbar.querySelectorAll("button")) button.classList.toggle("active", button.dataset.filter === queueFilter); };
  toolbar.addEventListener("click", event => {
    const button = event.target.closest("button[data-filter]");
    if (!button) return;
    queueFilter = button.dataset.filter;
    localStorage.setItem("queue-filter", queueFilter);
    sync(); loadQueue();
  });
  sync();
}

let queueLoading = false;
async function loadQueue() {
  if (queueLoading) return; queueLoading = true;
  const root = document.getElementById("queue-list");
  try {
    const jobs = await api("/api/jobs");
    const finished = jobs.filter(job => QUEUE_FINISHED_STATES.has(job.state)).length;
    const visible = queueFilter === "all" ? jobs : jobs.filter(job => !QUEUE_FINISHED_STATES.has(job.state));
    const note = document.getElementById("queue-filter-note");
    if (note) note.textContent = queueFilter === "all" || !finished ? "" : `${finished} finished job(s) hidden`;
    const emptyMessage = jobs.length
      ? `No active or queued jobs — ${finished} finished job(s) hidden.`
      : "The restoration queue is empty. Choose chapters or a custom slice from the library.";
    root.replaceChildren(...(visible.length ? visible.map(queueCard) : [el("div", "empty-state", emptyMessage)]));
  }
  catch (error) { notify(error.message, true); }
  finally { queueLoading = false; }
}

let lastEventId = 0;
async function loadJob() {
  const publicId = document.body.dataset.publicId;
  try {
    const job = await api(`/api/jobs/${publicId}`);
    document.getElementById("job-name").textContent = job.display_name;
    document.getElementById("job-source").textContent = `${job.disc_slug.toUpperCase()} · title ${job.title_number} · ${formatTime(job.source_start_ms)}–${formatTime(job.source_end_ms)} (${formatTime(job.duration_ms)})`;
    const summary = el("div", "summary-card");
    for (const [label,value] of [["State",job.state.replaceAll("_"," ")],["Stage",job.stage||"Not started"],["Progress",`${job.progress_percent}%`],["Elapsed",formatTime(job.elapsed_seconds*1000)],["ETA",job.eta_seconds?formatTime(job.eta_seconds*1000):"Pending"]]) { const cell=el("div"); cell.append(el("span","",label),el("strong","",value)); summary.append(cell); }
    document.getElementById("job-summary").replaceChildren(summary);
    const settings = document.getElementById("job-settings"); settings.replaceChildren();
    for (const [key,value] of Object.entries(job.settings)) { settings.append(el("dt","",key.replaceAll("_"," ")),el("dd","",String(value))); }
    if (job.artifacts.length) { const panel=document.getElementById("job-artifacts"); const links=document.getElementById("artifact-links"); links.replaceChildren(); for (const artifact of job.artifacts) { const link=el("a","button secondary artifact-link",artifact.kind.replaceAll("_"," ")); link.href=`/media/${artifact.id}`; links.append(link); } panel.classList.remove("hidden"); }
    const events = await api(`/api/jobs/${publicId}/events?after=${lastEventId}`); const list=document.getElementById("job-events");
    for (const event of events) { lastEventId=Math.max(lastEventId,event.id); const item=el("li"); const date=new Date(event.created_at); item.append(el("time","",date.toLocaleTimeString()),el("p","",event.message || `${event.event_type}: ${event.state || event.stage || "update"}`)); list.prepend(item); }
  } catch (error) { notify(error.message, true); }
}

// ---- System load graph (queue page) ----
const METRIC_SERIES = [
  {key: "gpu_pct", label: "GPU", color: "#1baf7a"},
  {key: "cpu_pct", label: "CPU", color: "#2a78d6"},
];
let metricsMinutes = 60;
let metricsSamples = [];

function drawMetricsChart() {
  const canvas = document.getElementById("metrics-chart");
  if (!canvas) return;
  const wrap = canvas.parentElement;
  const dpr = window.devicePixelRatio || 1;
  const width = wrap.clientWidth, height = 220;
  canvas.width = width * dpr; canvas.height = height * dpr;
  canvas.style.width = `${width}px`; canvas.style.height = `${height}px`;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, width, height);
  const pad = {top: 12, right: 52, bottom: 24, left: 12};
  const plotW = width - pad.left - pad.right, plotH = height - pad.top - pad.bottom;
  const ink = "#24201d", muted = "#746d66", line = "#ded7cc";
  ctx.font = "11px Inter, sans-serif";
  for (const value of [0, 25, 50, 75, 100]) {
    const y = pad.top + plotH * (1 - value / 100);
    ctx.strokeStyle = line; ctx.lineWidth = value === 0 ? 1 : 0.5;
    ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(pad.left + plotW, y); ctx.stroke();
    ctx.fillStyle = muted; ctx.textAlign = "left"; ctx.textBaseline = "middle";
    ctx.fillText(`${value}%`, pad.left + plotW + 8, y);
  }
  if (metricsSamples.length < 2) {
    ctx.fillStyle = muted; ctx.textAlign = "center";
    ctx.fillText("Collecting samples…", width / 2, height / 2);
    return;
  }
  const first = Date.parse(metricsSamples[0].ts), last = Date.parse(metricsSamples.at(-1).ts);
  const spanX = Math.max(1, last - first);
  const xFor = ts => pad.left + plotW * ((Date.parse(ts) - first) / spanX);
  const yFor = value => pad.top + plotH * (1 - Math.min(100, value) / 100);
  ctx.fillStyle = muted; ctx.textBaseline = "top";
  ctx.textAlign = "left"; ctx.fillText(new Date(first).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"}), pad.left, pad.top + plotH + 8);
  ctx.textAlign = "right"; ctx.fillText(new Date(last).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"}), pad.left + plotW, pad.top + plotH + 8);
  for (const series of METRIC_SERIES) {
    ctx.strokeStyle = series.color; ctx.lineWidth = 2; ctx.lineJoin = "round";
    ctx.beginPath();
    let started = false;
    for (const sample of metricsSamples) {
      const value = sample[series.key];
      if (value === null || value === undefined) { started = false; continue; }
      const x = xFor(sample.ts), y = yFor(value);
      if (started) ctx.lineTo(x, y); else { ctx.moveTo(x, y); started = true; }
    }
    ctx.stroke();
    const lastSample = [...metricsSamples].reverse().find(sample => sample[series.key] !== null && sample[series.key] !== undefined);
    if (lastSample) {
      const x = xFor(lastSample.ts), y = yFor(lastSample[series.key]);
      ctx.fillStyle = series.color; ctx.beginPath(); ctx.arc(x, y, 3.5, 0, Math.PI * 2); ctx.fill();
      ctx.strokeStyle = "#fffdf9"; ctx.lineWidth = 2; ctx.stroke();
      ctx.fillStyle = ink; ctx.textAlign = "right"; ctx.textBaseline = "bottom";
      ctx.fillText(series.label, x - 6, y - 5);
    }
  }
}

function metricsTiles() {
  const root = document.getElementById("metrics-tiles");
  const latest = metricsSamples.at(-1);
  if (!root || !latest) return;
  root.replaceChildren();
  const tiles = [
    ["GPU", latest.gpu_pct === null ? "—" : `${Math.round(latest.gpu_pct)}%`],
    ["CPU", `${Math.round(latest.cpu_pct)}%`],
    ["VRAM", latest.gpu_mem_mib === null ? "—" : `${(latest.gpu_mem_mib / 1024).toFixed(1)} GiB`],
    ["GPU power", latest.gpu_power_w === null ? "—" : `${Math.round(latest.gpu_power_w)} W`],
    ["GPU temp", latest.gpu_temp_c === null || latest.gpu_temp_c === undefined ? "—" : `${Math.round(latest.gpu_temp_c)}°C`],
    ["RAM", `${Math.round(latest.mem_pct)}%`],
  ];
  for (const [label, value] of tiles) {
    const cell = el("div", "metrics-tile");
    cell.append(el("strong", "", value), el("span", "", label));
    root.append(cell);
  }
}

function metricsHover(event) {
  const canvas = document.getElementById("metrics-chart");
  const tooltip = document.getElementById("metrics-tooltip");
  if (!canvas || !tooltip || metricsSamples.length < 2) return;
  const rect = canvas.getBoundingClientRect();
  const pad = {left: 12, right: 52};
  const plotW = rect.width - pad.left - pad.right;
  const ratio = Math.min(1, Math.max(0, (event.clientX - rect.left - pad.left) / plotW));
  const first = Date.parse(metricsSamples[0].ts), last = Date.parse(metricsSamples.at(-1).ts);
  const target = first + ratio * (last - first);
  let nearest = metricsSamples[0];
  for (const sample of metricsSamples) {
    if (Math.abs(Date.parse(sample.ts) - target) < Math.abs(Date.parse(nearest.ts) - target)) nearest = sample;
  }
  const when = new Date(Date.parse(nearest.ts)).toLocaleTimeString();
  const gpu = nearest.gpu_pct === null ? "n/a" : `${Math.round(nearest.gpu_pct)}%`;
  const vram = nearest.gpu_mem_mib === null ? "n/a" : `${(nearest.gpu_mem_mib / 1024).toFixed(1)} GiB`;
  const temp = nearest.gpu_temp_c === null || nearest.gpu_temp_c === undefined ? "n/a" : `${Math.round(nearest.gpu_temp_c)}°C`;
  tooltip.textContent = `${when} · GPU ${gpu} · CPU ${Math.round(nearest.cpu_pct)}% · VRAM ${vram} · ${temp}`;
  tooltip.classList.remove("hidden");
  tooltip.style.left = `${Math.min(rect.width - 220, Math.max(0, event.clientX - rect.left - 110))}px`;
}

let metricsLoading = false;
async function loadMetrics() {
  if (metricsLoading || !document.getElementById("metrics-chart")) return;
  metricsLoading = true;
  try {
    const payload = await api(`/api/metrics?minutes=${metricsMinutes}`);
    metricsSamples = payload.samples;
    drawMetricsChart();
    metricsTiles();
    const updated = document.getElementById("metrics-updated");
    if (updated) updated.textContent = `Sampled every 5 s · updated ${new Date().toLocaleTimeString()}`;
  } catch (error) { /* metrics are auxiliary; never toast-spam the queue page */ }
  finally { metricsLoading = false; }
}

function initMetrics() {
  const ranges = document.getElementById("metrics-ranges");
  if (!ranges) return;
  ranges.addEventListener("click", event => {
    const button = event.target.closest("button[data-minutes]");
    if (!button) return;
    metricsMinutes = Number(button.dataset.minutes);
    for (const other of ranges.querySelectorAll("button")) other.classList.toggle("active", other === button);
    loadMetrics();
  });
  const canvas = document.getElementById("metrics-chart");
  canvas.addEventListener("mousemove", metricsHover);
  canvas.addEventListener("mouseleave", () => document.getElementById("metrics-tooltip").classList.add("hidden"));
  window.addEventListener("resize", drawMetricsChart);
  loadMetrics();
  window.setInterval(loadMetrics, 10000);
}

async function refreshWorkerHealth() {
  const pill = document.getElementById("worker-health");
  if (!pill) return;
  try {
    const h = await api("/api/worker/health");
    let label, state;
    if (!h.present) { label = "worker: n/a"; state = "unknown"; }
    else if (h.alive && h.activity === "waiting") { label = h.detail || "worker: waiting"; state = "warn"; }
    else if (h.alive) { label = `worker: ${h.activity}`; state = h.activity === "db_error" ? "warn" : "ok"; }
    else { label = h.activity === "stopped" ? "worker: stopped" : "worker: down"; state = "down"; }
    pill.textContent = label;
    pill.dataset.state = state;
    const bits = [];
    if (h.detail) bits.push(h.detail);
    if (h.last_beat_at) bits.push(`last beat ${h.last_beat_at}`);
    if (h.active_job_id) bits.push(`job #${h.active_job_id}`);
    if (h.last_error) bits.push(`last error: ${h.last_error}`);
    pill.title = bits.join(" · ") || "GPU worker status";
  } catch (_) {
    pill.textContent = "worker: ?";
    pill.dataset.state = "unknown";
  }
}
refreshWorkerHealth();
window.setInterval(refreshWorkerHealth, 10000);

const page = document.body.dataset.page;
if (page === "library" && document.body.dataset.titleId) loadTitle();
else if (page === "library") loadLibrary();
else if (page === "queue" && document.body.dataset.publicId) { loadJob(); window.setInterval(loadJob, 2000); }
else if (page === "queue") { initQueueFilter(); loadQueue(); window.setInterval(loadQueue, 2000); initMetrics(); }
