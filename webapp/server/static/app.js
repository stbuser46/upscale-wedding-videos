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

function discCard(disc, payload, index) {
  const card = el("article", "disc-card");
  const header = el("div", "disc-card-header");
  const heading = el("div");
  heading.append(el("p", "eyebrow", disc.slug.toUpperCase()), el("h2", "", disc.label || disc.source_filename));
  heading.append(el("span", `status ${disc.scan_status === "failed" ? "failed" : ""}`, disc.scan_status));
  header.append(heading, el("span", "disc-number", String(index).padStart(2, "0")));
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
  card.append(header, stats, titles);
  return card;
}

// The catalog now spans several unrelated DVDs/events (weddings, birthdays,
// VHS transfers), so the library groups discs by their collection label
// (discs.collection) instead of assuming a single event's disc set.
async function loadLibrary() {
  const root = document.getElementById("library");
  const rescan = document.getElementById("rescan");
  rescan?.addEventListener("click", async () => {
    rescan.disabled = true;
    rescan.textContent = "Scanning read-only…";
    try { await api("/api/discs/scan", {method: "POST"}); notify("ISOs scanned successfully"); await loadLibrary(); }
    catch (error) { notify(error.message, true); }
    finally { rescan.disabled = false; rescan.textContent = "Re-scan ISOs"; }
  }, {once: true});
  try {
    const discs = await api("/api/discs");
    root.replaceChildren();
    if (!discs.length) { root.append(el("div", "empty-state", "No discs are cataloged yet. Use re-scan to inventory the configured ISOs.")); return; }
    const groups = new Map();
    for (const disc of discs) {
      const key = disc.collection || disc.label || disc.slug;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(disc);
    }
    for (const [collection, discsInGroup] of groups) {
      const section = el("section", "collection-group");
      const headingRow = el("div", "section-heading");
      const headingText = el("div");
      headingText.append(el("p", "eyebrow", `${discsInGroup.length} disc${discsInGroup.length === 1 ? "" : "s"}`), el("h2", "", collection || "Uncatalogued"));
      headingRow.append(headingText);
      section.append(headingRow);
      const grid = el("div", "disc-grid");
      for (const [index, disc] of discsInGroup.entries()) {
        const payload = await api(`/api/discs/${disc.id}/titles`);
        grid.append(discCard(disc, payload, index + 1));
      }
      section.append(grid);
      root.append(section);
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

function discLabel(job) {
  return job.disc_collection ? `${job.disc_collection} (${job.disc_slug.toUpperCase()})` : job.disc_slug.toUpperCase();
}

const ACTIVE_JOB_STATES = new Set(["preparing", "running", "assembling", "cancel_requested", "resuming"]);

// Human label for the log-tail-derived phase (see webapp/server/logtail.py);
// shared by the queue card's live strip and the job detail page.
function livePhaseLabel(phase) {
  return {prepare: "Preparing unit", encode: "VAE encode", dit: "DiT upscale", decode: "VAE decode"}[phase] || null;
}

// The one line + bar that visibly moves every poll even while frames_done is
// stuck mid-unit — combines the log-tail phase/batch/write with a compact
// per-unit chip strip, both sourced from GET /api/jobs/<id>/live.
function liveDetailNodes(live) {
  const bits = [];
  const label = live.log?.phase ? livePhaseLabel(live.log.phase) : null;
  if (label) bits.push(label);
  if (live.log?.batch) bits.push(`batch ${live.log.batch.current}/${live.log.batch.total}`);
  if (live.log?.last_write) bits.push(`wrote frames ${live.log.last_write.start}–${live.log.last_write.end}`);
  const lineText = bits.length ? bits.join(" · ") : (live.log?.tail_line || (live.log?.available === false ? "No log output yet." : "Waiting for live detail…"));
  const line = el("div", "queue-live-line", lineText);
  const bar = el("div", "live-bar"); const fill = el("div", "live-bar-fill");
  if (live.log?.batch) fill.style.width = `${Math.round((live.log.batch.current / live.log.batch.total) * 100)}%`;
  bar.append(fill);
  const units = el("div", "queue-live-units");
  units.append(...(live.units?.length ? live.units.map(unitChip) : [el("span", "muted", "No units recorded yet.")]));
  return {line, bar, units};
}

function queueCard(job, live) {
  const active = ACTIVE_JOB_STATES.has(job.state);
  const card = el("article", `queue-card${active ? " active" : ""}`);
  const identity = el("div"); identity.append(el("span", `state-pill ${job.state}`, job.state.replaceAll("_"," ")));
  // Where is this job physically running? live.executor comes from the job's
  // /live payload (cloud = RunPod pods, local = this machine's GPU).
  if (live?.executor) identity.append(el("span", `exec-badge ${live.executor}`, live.executor === "cloud" ? "cloud gpus" : "local gpu"));
  const heading = el("h2"); const link = el("a", "", job.display_name); link.href = `/jobs/${job.public_id}`; heading.append(link); identity.append(heading, el("div", "queue-source", `${discLabel(job)} · title ${job.title_number} · ${formatTime(job.source_start_ms)}–${formatTime(job.source_end_ms)}`));
  const stage = el("div"); stage.append(el("p", "eyebrow", "Current stage"), el("strong", "", job.stage || (job.start_requested ? "Awaiting worker" : "Not started")), el("div", "queue-source", `${job.frames_done.toLocaleString()} / ${job.frames_total.toLocaleString()} frames`));
  const progress = el("div"); const track = el("progress", "progress-native"); track.max = 100; track.value = Math.min(100, job.progress_percent);
  const etaText = job.eta_seconds ? `ETA ${formatTime(job.eta_seconds * 1000)}`
    : job.estimated_restore_seconds ? `Est. restore ~${formatTime(job.estimated_restore_seconds * 1000)}`
    : "ETA pending";
  const meta = el("div", "progress-meta"); meta.append(el("span", "", `${job.progress_percent}%`), el("span", "", etaText)); progress.append(track, meta);
  const actions = el("div", "queue-actions");
  const action = (label, method, path, body, cls="button small") => { const button = el("button", cls, label); button.addEventListener("click", async () => { button.disabled = true; try { await api(path, {method, body}); await loadQueue(); } catch (error) { notify(error.message, true); button.disabled = false; } }); actions.append(button); };
  if (job.can_start) action("Start", "POST", `/api/jobs/${job.public_id}/start`);
  if (job.can_retry) action("Retry", "POST", `/api/jobs/${job.public_id}/retry`);
  if (job.state === "queued") { action("↑", "POST", `/api/jobs/${job.public_id}/move`, {direction:"up"}); action("↓", "POST", `/api/jobs/${job.public_id}/move`, {direction:"down"}); }
  if (job.can_cancel && job.state !== "cancel_requested") {
    const cancel = el("button", "button small danger", active ? "Cancel after stage" : "Cancel"); cancel.addEventListener("click", async () => { if (!confirm(active ? "Finish the current pipeline stage, then cancel?" : "Cancel this queued job?")) return; try { await api(`/api/jobs/${job.public_id}/cancel`, {method:"POST"}); await loadQueue(); } catch (error) { notify(error.message,true); } }); actions.append(cancel);
  }
  card.append(identity, stage, progress, actions);
  if (active && live) {
    const {line, bar, units} = liveDetailNodes(live);
    const liveBox = el("div", "queue-live"); liveBox.append(line, bar, units);
    card.append(liveBox);
  }
  return card;
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
    // Live detail (log tail-parse + unit strip) is only fetched for jobs that
    // are actually running — usually 1-2 at a time (one local, one cloud) —
    // so this stays cheap even though it re-fetches every 2s poll.
    const liveByJob = {};
    await Promise.all(visible.filter(job => ACTIVE_JOB_STATES.has(job.state)).map(async job => {
      try { liveByJob[job.public_id] = await api(`/api/jobs/${job.public_id}/live`); }
      catch (_) { /* live detail is auxiliary; the card just renders without it */ }
    }));
    root.replaceChildren(...(visible.length ? visible.map(job => queueCard(job, liveByJob[job.public_id])) : [el("div", "empty-state", emptyMessage)]));
    updateLocalStatus(jobs, liveByJob);
  }
  catch (error) { notify(error.message, true); }
  finally { queueLoading = false; }
}

// The local panel's live status line: what this machine's GPU is doing right
// now, derived from the same /live payloads the queue cards already fetch
// (live.executor distinguishes the local GPU from cloud pods).
let localActiveJob = null; // "Segment 15 (…) — VAE decode" while a local job runs

function updateLocalStatus(jobs, liveByJob) {
  const activeJobs = jobs.filter(job => ACTIVE_JOB_STATES.has(job.state));
  const local = activeJobs.find(job => liveByJob[job.public_id]?.executor === "local");
  const cloudCount = activeJobs.filter(job => liveByJob[job.public_id]?.executor === "cloud").length;
  const phase = local ? livePhaseLabel(liveByJob[local.public_id].log?.phase) : null;
  localActiveJob = local ? `${local.display_name}${phase ? ` — ${phase}` : ""}` : null;
  const node = document.getElementById("metrics-local-status");
  if (node) {
    const cloudNote = cloudCount ? ` · ${cloudCount} other job${cloudCount === 1 ? "" : "s"} on cloud pods` : "";
    node.textContent = localActiveJob
      ? `Restoring on local GPU: ${localActiveJob}${cloudNote}`
      : `Local GPU idle — no local restoration job running${cloudNote}`;
  }
  renderResourcesNow();
}

// ---- "Compute now" strip (top of queue page) ----
// One glanceable row: the local machine plus every live cloud pod, each with
// state + GPU/CPU/RAM. The detailed charts/ledger stay in the panels below.
function localNowTile() {
  const card = el("article", "cloud-now-card local-now-card");
  const head = el("div", "cloud-now-head");
  head.append(el("strong", "", "local gpu"), el("span", `cloud-state ${localActiveJob ? "running" : "idle"}`, localActiveJob ? "restoring" : "idle"));
  card.append(head);
  const latest = metricsSamples.at(-1);
  if (!latest) {
    card.append(el("div", "cloud-now-gpu muted", "—"));
    return card;
  }
  const gpu = latest.gpu_pct === null || latest.gpu_pct === undefined ? null : Math.round(latest.gpu_pct);
  card.append(el("div", `cloud-now-gpu${gpu !== null && gpu >= 95 ? " full" : ""}`, gpu === null ? "—" : `${gpu}%`));
  const meta = el("div", "cloud-meta");
  meta.append(
    cloudStat("CPU", `${Math.round(latest.cpu_pct)}%`),
    cloudStat("RAM", `${Math.round(latest.mem_pct)}%`),
    cloudStat("VRAM", latest.gpu_mem_mib === null ? "—" : `${(latest.gpu_mem_mib / 1024).toFixed(0)} GiB`),
  );
  card.append(meta);
  if (localActiveJob) card.append(el("div", "resource-job muted", localActiveJob));
  return card;
}

function renderResourcesNow() {
  const root = document.getElementById("resources-now-grid");
  if (!root) return;
  const tiles = [localNowTile()];
  const pods = lastPodMetricsPayload ? livePods(lastPodMetricsPayload.pods) : [];
  tiles.push(...pods.map(cloudNowTile));
  if (!pods.length && lastCloudFleet?.summary?.active) {
    // Ledger says pods are billing but telemetry hasn't reported them yet.
    tiles.push(el("div", "empty-state", `${lastCloudFleet.summary.active} cloud pod(s) starting…`));
  }
  root.replaceChildren(...tiles);
}

function jobPodCard(pod) {
  const card = el("article", "cloud-card");
  const head = el("div", "cloud-card-head");
  head.append(el("span", "cloud-pod-id", (pod.pod_id || pod.name || "pod").slice(0, 16)), el("span", `cloud-state ${pod.state}`, pod.state.replaceAll("_"," ")));
  const meta = el("div", "cloud-meta");
  meta.append(cloudStat("Rate", `$${(pod.rate_per_hr ?? 0).toFixed(2)}/h`), cloudStat("Uptime", formatUptime(pod.uptime_s)), cloudStat("Accrued", `$${pod.cost_usd.toFixed(2)}`));
  card.append(head, el("div", "cloud-gpu", pod.gpu_type || "GPU"), meta);
  return card;
}

// Per-segment drill-down: the log-tail live detail + unit strip + (for cloud
// jobs) which pods actually ran this job's units. Called from loadJob()'s
// existing poll loop, so it shares that cadence rather than its own timer.
async function renderJobLive(publicId) {
  const panel = document.getElementById("job-live");
  const podsPanel = document.getElementById("job-pods");
  try {
    const live = await api(`/api/jobs/${publicId}/live`);
    panel.classList.remove("hidden");
    const {line, bar: liveBar} = liveDetailNodes(live);
    document.getElementById("job-live-line").textContent = line.textContent;
    document.getElementById("job-live-bar-fill").style.width = liveBar.firstChild.style.width || "0%";
    const updated = document.getElementById("job-live-updated");
    updated.textContent = live.log?.updated_at ? `log updated ${new Date(live.log.updated_at).toLocaleTimeString()}` : "";
    document.getElementById("job-units").replaceChildren(...(live.units?.length ? live.units.map(unitChip) : [el("span", "muted", "No units recorded yet.")]));
    if (live.executor === "cloud" && live.pods?.length) {
      podsPanel.classList.remove("hidden");
      document.getElementById("job-pods-body").replaceChildren(...live.pods.map(jobPodCard));
    } else {
      podsPanel.classList.add("hidden");
    }
  } catch (_) {
    panel.classList.add("hidden");
    podsPanel.classList.add("hidden");
  }
}

let lastEventId = 0;
async function loadJob() {
  const publicId = document.body.dataset.publicId;
  try {
    const job = await api(`/api/jobs/${publicId}`);
    document.getElementById("job-name").textContent = job.display_name;
    document.getElementById("job-source").textContent = `${discLabel(job)} · title ${job.title_number} · ${formatTime(job.source_start_ms)}–${formatTime(job.source_end_ms)} (${formatTime(job.duration_ms)})`;
    const errorPanel = document.getElementById("job-error");
    if (job.error) { errorPanel.textContent = job.error; errorPanel.classList.remove("hidden"); }
    else errorPanel.classList.add("hidden");
    const summary = el("div", "summary-card");
    for (const [label,value] of [["State",job.state.replaceAll("_"," ")],["Stage",job.stage||"Not started"],["Progress",`${job.progress_percent}%`],["FPS",job.fps?job.fps.toFixed(2):"—"],["Elapsed",formatTime(job.elapsed_seconds*1000)],["ETA",job.eta_seconds?formatTime(job.eta_seconds*1000):(job.estimated_restore_seconds?`~${formatTime(job.estimated_restore_seconds*1000)} (est.)`:"Pending")]]) { const cell=el("div"); cell.append(el("span","",label),el("strong","",value)); summary.append(cell); }
    document.getElementById("job-summary").replaceChildren(summary);
    const settings = document.getElementById("job-settings"); settings.replaceChildren();
    for (const [key,value] of Object.entries(job.settings)) { settings.append(el("dt","",key.replaceAll("_"," ")),el("dd","",typeof value === "object" && value !== null ? JSON.stringify(value) : String(value))); }
    if (job.artifacts.length) { const panel=document.getElementById("job-artifacts"); const links=document.getElementById("artifact-links"); links.replaceChildren(); for (const artifact of job.artifacts) { const link=el("a","button secondary artifact-link",artifact.kind.replaceAll("_"," ")); link.href=`/media/${artifact.id}`; links.append(link); } panel.classList.remove("hidden"); }
    const events = await api(`/api/jobs/${publicId}/events?after=${lastEventId}`); const list=document.getElementById("job-events");
    for (const event of events) { lastEventId=Math.max(lastEventId,event.id); const item=el("li"); const date=new Date(event.created_at); item.append(el("time","",date.toLocaleTimeString()),el("p","",event.message || `${event.event_type}: ${event.state || event.stage || "update"}`)); list.prepend(item); }
    await renderJobLive(publicId);
  } catch (error) { notify(error.message, true); }
}

// ---- Cloud fleet panel (queue page) ----
// cloud_pods timestamps are UTC but arrive in two spellings: Python's
// aware ISO ("…T…+00:00") and SQLite's naive "YYYY-MM-DD HH:MM:SS" from
// datetime('now'). Date.parse treats the naive form as LOCAL time, skewing
// every lifecycle display by the UTC offset — so force a Z when unzoned.
function parseUtcTs(ts) {
  if (!ts) return null;
  let text = String(ts).replace(" ", "T");
  if (!/(Z|[+-]\d\d:?\d\d)$/.test(text)) text += "Z";
  const ms = Date.parse(text);
  return Number.isNaN(ms) ? null : ms;
}

function clockTime(ts) {
  const ms = parseUtcTs(ts);
  return ms === null ? "?" : new Date(ms).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
}

function formatUptime(seconds) {
  const total = Math.max(0, Math.round(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}

function cloudStat(label, value) {
  const cell = el("div", "cloud-stat");
  cell.append(el("strong", "", value), el("span", "", label));
  return cell;
}

// The pod's whole life on one line: when it was created, how long
// provisioning took to reach ready, and when it was torn down — so a pod can
// be watched (or audited) from provisioning through completion.
function podLifecycleLine(pod) {
  const phases = [];
  if (pod.created_at) phases.push(`created ${clockTime(pod.created_at)}`);
  if (pod.ready_at) {
    const createdMs = parseUtcTs(pod.created_at), readyMs = parseUtcTs(pod.ready_at);
    const took = createdMs !== null && readyMs !== null ? ` after ${formatUptime((readyMs - createdMs) / 1000)}` : "";
    phases.push(`ready ${clockTime(pod.ready_at)}${took}`);
  } else if (pod.state === "creating") {
    phases.push("provisioning…");
  }
  if (pod.terminated_at) phases.push(`terminated ${clockTime(pod.terminated_at)}`);
  if (!phases.length) return null;
  return el("div", "pod-lifecycle muted", phases.join(" → "));
}

function cloudPodCard(pod) {
  const card = el("article", `cloud-card${pod.error ? " errored" : ""}`);
  const head = el("div", "cloud-card-head");
  const shortId = (pod.pod_id || pod.name || "pod").slice(0, 12);
  head.append(el("span", "cloud-pod-id", shortId), el("span", `cloud-state ${pod.state}`, pod.state.replaceAll("_", " ")));
  const job = pod.job_public_id ? `job ${pod.job_public_id}` : "unassigned";
  const meta = el("div", "cloud-meta");
  meta.append(
    cloudStat("Rate", `$${(pod.rate_per_hr ?? 0).toFixed(2)}/h`),
    cloudStat("Uptime", formatUptime(pod.uptime_s)),
    cloudStat("Accrued", `$${pod.cost_usd.toFixed(2)}`),
  );
  if (pod.unit !== null && pod.unit !== undefined) meta.append(cloudStat("Unit", `#${pod.unit + 1}`));
  card.append(head, el("div", "cloud-gpu", pod.gpu || "GPU"), el("div", "cloud-job muted", job), meta);
  const lifecycle = podLifecycleLine(pod);
  if (lifecycle) card.append(lifecycle);
  if (pod.provisioning_hint) card.append(el("p", "cloud-hint", pod.provisioning_hint));
  if (pod.error) card.append(el("p", "cloud-error", pod.error));
  return card;
}

// One small chip per restoration unit — a live "unit 3/5" strip for the
// active cloud job. State colours mirror the queue's own .state-pill palette
// (valid=completed green, running=in-progress green, invalid=failed red).
function unitChip(unit) {
  const chip = el("span", `unit-chip ${unit.state}`, String(unit.sequence + 1));
  const frames = unit.frame_count ? `, ${unit.frame_count} frames` : "";
  const pod = unit.pod ? `, pod ${unit.pod.name || unit.pod.pod_id}` : "";
  chip.title = `Unit ${unit.sequence + 1}: ${unit.state}${frames}${pod}`;
  return chip;
}

function renderCloudFleet(fleet) {
  const s = fleet.summary;
  const pill = document.getElementById("cloud-summary");
  if (pill) {
    pill.textContent = `${s.active}/${s.max_slots} GPUs active · $${s.rate_per_hr.toFixed(2)}/h · $${s.spend_usd.toFixed(2)} spent / $${s.spend_cap_usd.toFixed(0)} cap`;
    const ratio = s.spend_cap_usd > 0 ? s.spend_usd / s.spend_cap_usd : 0;
    pill.dataset.level = ratio >= 1 ? "danger" : ratio >= 0.75 ? "warn" : "ok";
  }
  // Terminated pods accumulate in the ledger across runs and drown out the
  // live ones — hide them by default, with an explicit count so the spend
  // history is still one click away.
  const terminated = fleet.pods.filter(pod => pod.state === "terminated").length;
  const shown = cloudPodFilter === "all" ? fleet.pods : fleet.pods.filter(pod => pod.state !== "terminated");
  const filterNote = document.getElementById("cloud-pod-filter-note");
  if (filterNote) filterNote.textContent = cloudPodFilter === "all" || !terminated ? "" : `${terminated} terminated pod${terminated === 1 ? "" : "s"} hidden`;
  const grid = document.getElementById("cloud-pods");
  const emptyText = terminated ? `No live pods — ${terminated} terminated pod${terminated === 1 ? "" : "s"} hidden.` : "No pods reported yet.";
  if (grid) grid.replaceChildren(...(shown.length ? shown.map(cloudPodCard) : [el("div", "empty-state", emptyText)]));

  const jobPanel = document.getElementById("cloud-job");
  const job = fleet.active_job;
  if (jobPanel) jobPanel.classList.toggle("hidden", !job);
  if (job) {
    const title = document.getElementById("cloud-job-title");
    if (title) {
      const c = job.units_summary || {};
      const doneOf = job.units_total ? ` · unit ${(c.valid || 0) + (c.running || 0)}/${job.units_total}` : "";
      const detail = job.worker_detail ? ` — ${job.worker_detail}` : "";
      title.textContent = `Job ${job.public_id}${doneOf}${detail}`;
    }
    const spend = document.getElementById("cloud-job-spend");
    if (spend) spend.textContent = `$${job.spend_usd.toFixed(2)} this job`;
    const units = document.getElementById("cloud-units");
    if (units) units.replaceChildren(...(job.units.length ? job.units.map(unitChip) : [el("span", "muted", "No units recorded yet.")]));
  }
}

// Which pods the fleet grid shows: "active" (default) hides terminated pods,
// "all" includes the full spend history. Mirrors the queue's own filter.
let cloudPodFilter = localStorage.getItem("cloud-pod-filter") || "active";
let lastCloudFleet = null;

function initCloudPodFilter() {
  const toolbar = document.getElementById("cloud-pod-filter");
  if (!toolbar) return;
  const sync = () => { for (const button of toolbar.querySelectorAll("button")) button.classList.toggle("active", button.dataset.podfilter === cloudPodFilter); };
  toolbar.addEventListener("click", event => {
    const button = event.target.closest("button[data-podfilter]");
    if (!button) return;
    cloudPodFilter = button.dataset.podfilter;
    localStorage.setItem("cloud-pod-filter", cloudPodFilter);
    sync();
    if (lastCloudFleet) renderCloudFleet(lastCloudFleet);
  });
  sync();
}

let cloudLoading = false;
async function loadCloudFleet() {
  if (cloudLoading || document.hidden) return;
  const panel = document.getElementById("cloud-fleet");
  if (!panel) return;
  cloudLoading = true;
  try {
    const fleet = await api("/api/cloud/fleet");
    lastCloudFleet = fleet;
    // Keep the panel on screen whenever the cloud has ever been used —
    // fleet.enabled only means "a pod is billing right now", and hiding the
    // whole section between pod waves made the run look like it vanished.
    const show = fleet.enabled || fleet.pods.length > 0 || !!fleet.active_job;
    panel.classList.toggle("hidden", !show);
    if (show) { renderCloudFleet(fleet); loadCloudPodMetrics(); renderResourcesNow(); }
  } catch (error) { /* cloud fleet is auxiliary; never toast-spam the queue page */ }
  finally { cloudLoading = false; }
}

// ---- Live cloud pod telemetry charts (queue page) ----
// Colour-blind-safe categorical palette (validated: light-surface CVD floor
// 24.2 dE, three slots need visible labels rather than colour alone — hence
// the always-present text legend below the charts). Assigned in a stable
// order per pod id so a pod keeps its colour across redraws even as other
// pods in the fleet retire/recycle mid-run.
const CLOUD_POD_COLORS = ["#2a78d6", "#1baf7a", "#eda100", "#008300", "#4a3aa7", "#e34948", "#e87ba4", "#eb6834"];
let cloudPodColorAssignments = {};
function colorForPod(podId) {
  if (!(podId in cloudPodColorAssignments)) {
    const used = new Set(Object.values(cloudPodColorAssignments));
    const free = CLOUD_POD_COLORS.find(c => !used.has(c));
    cloudPodColorAssignments[podId] = free || CLOUD_POD_COLORS[Object.keys(cloudPodColorAssignments).length % CLOUD_POD_COLORS.length];
  }
  return cloudPodColorAssignments[podId];
}

// One entry per small-multiple chart: which series key to plot, and its y
// axis (percentages share a fixed 0-100 axis; cost is unbounded so scales to
// the fleet's current max).
const CLOUD_METRIC_CHARTS = [
  {canvas: "cloud-chart-gpu", key: "gpu_pct", fixedMax: 100},
  {canvas: "cloud-chart-gpumem", key: "gpu_mem_pct", fixedMax: 100},
  {canvas: "cloud-chart-cpu", key: "cpu_pct", fixedMax: 100},
  {canvas: "cloud-chart-cost", key: "cost_usd", fixedMax: null},
];

// Sizes a chart canvas's backing bitmap to match its CSS box, measuring the
// parent's own padding so the canvas never gets set wider than the space
// actually available to it. Without the padding correction, a canvas inside
// a padded CSS Grid item (which defaults to min-width:auto) gets sized to
// the parent's clientWidth (content + padding) and so ends up wider than the
// parent's content box; the grid item then grows to fit that oversized
// canvas, the next poll reads the grown clientWidth, and the canvas grows
// again — an unbounded feedback loop. Also skips touching canvas.width /
// canvas.height (which always resets the 2D context, per the HTML spec)
// unless the measured size actually changed, so idle polling is a no-op.
function sizeChartCanvas(canvas, cssHeight) {
  const wrap = canvas.parentElement;
  const wrapStyle = getComputedStyle(wrap);
  const paddingX = parseFloat(wrapStyle.paddingLeft || "0") + parseFloat(wrapStyle.paddingRight || "0");
  const width = Math.max(0, Math.round(wrap.clientWidth - paddingX));
  const height = Math.round(cssHeight);
  const dpr = window.devicePixelRatio || 1;
  const bitmapWidth = Math.round(width * dpr), bitmapHeight = Math.round(height * dpr);
  if (canvas.width !== bitmapWidth || canvas.height !== bitmapHeight) {
    canvas.width = bitmapWidth;
    canvas.height = bitmapHeight;
  }
  const widthPx = `${width}px`, heightPx = `${height}px`;
  if (canvas.style.width !== widthPx) canvas.style.width = widthPx;
  if (canvas.style.height !== heightPx) canvas.style.height = heightPx;
  const ctx = canvas.getContext("2d");
  // setTransform (absolute) rather than scale (relative-and-compounding):
  // since we may skip the width/height reassignment above, a repeated
  // ctx.scale(dpr, dpr) would multiply the scale again on every redraw.
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return {ctx, width, height};
}

function drawCloudMetricChart(spec, pods) {
  const canvas = document.getElementById(spec.canvas);
  if (!canvas) return;
  const {ctx, width, height} = sizeChartCanvas(canvas, 140);
  ctx.clearRect(0, 0, width, height);
  const pad = {top: 8, right: 8, bottom: 8, left: 8};
  const plotW = width - pad.left - pad.right, plotH = height - pad.top - pad.bottom;
  const muted = "#746d66", gridLine = "#ded7cc";

  const series = pods.map(pod => ({
    pod, points: pod.series.filter(s => s[spec.key] !== null && s[spec.key] !== undefined),
  }));
  const allPoints = series.flatMap(s => s.points);
  if (allPoints.length < 2) {
    ctx.fillStyle = muted; ctx.font = "11px Inter, sans-serif"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText("Collecting samples…", width / 2, height / 2);
    return;
  }
  const first = Math.min(...allPoints.map(p => Date.parse(p.ts)));
  const last = Math.max(...allPoints.map(p => Date.parse(p.ts)));
  const spanX = Math.max(1, last - first);
  const maxY = spec.fixedMax ?? Math.max(0.01, ...allPoints.map(p => p[spec.key])) * 1.15;
  const xFor = ts => pad.left + plotW * ((Date.parse(ts) - first) / spanX);
  const yFor = value => pad.top + plotH * (1 - Math.min(maxY, Math.max(0, value)) / maxY);

  for (const frac of [0, 0.5, 1]) {
    const y = pad.top + plotH * (1 - frac);
    ctx.strokeStyle = gridLine; ctx.lineWidth = frac === 0 ? 1 : 0.5;
    ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(pad.left + plotW, y); ctx.stroke();
  }

  for (const {pod, points} of series) {
    if (points.length < 2) continue;
    ctx.strokeStyle = colorForPod(pod.pod_id); ctx.lineWidth = 2; ctx.lineJoin = "round";
    ctx.beginPath();
    let started = false;
    for (const point of points) {
      const x = xFor(point.ts), y = yFor(point[spec.key]);
      if (started) ctx.lineTo(x, y); else { ctx.moveTo(x, y); started = true; }
    }
    ctx.stroke();
    const lastPoint = points.at(-1);
    ctx.fillStyle = colorForPod(pod.pod_id);
    ctx.beginPath(); ctx.arc(xFor(lastPoint.ts), yFor(lastPoint[spec.key]), 3, 0, Math.PI * 2); ctx.fill();
  }
}

// Colour alone never carries identity (three palette slots fail the 3:1
// contrast check on the light card surface) — this text legend is the
// always-on relief, not decoration.
function renderCloudMetricsLegend(pods) {
  const root = document.getElementById("cloud-metrics-legend");
  if (!root) return;
  root.replaceChildren(...(pods.length ? pods.map(pod => {
    const item = el("span", "cloud-metrics-legend-item");
    const dot = el("span", "cloud-metrics-legend-dot");
    dot.style.background = colorForPod(pod.pod_id);
    const last = pod.series.at(-1) || {};
    const gpu = last.gpu_pct === null || last.gpu_pct === undefined ? "—" : `${Math.round(last.gpu_pct)}%`;
    item.append(dot, el("strong", "", pod.label), el("span", "muted", ` GPU ${gpu} · $${pod.cost_usd.toFixed(2)}`));
    return item;
  }) : [el("span", "muted", "No pod telemetry yet.")]));
}

// A pod counts toward "at full GPU" the same way the per-pod tile row does
// (>=95%, see cloudNowTile) — keep the two thresholds identical so the
// headline count and the tiles it summarizes never visibly disagree.
function renderCloudMetricsTiles(fleet) {
  const root = document.getElementById("cloud-metrics-tiles");
  if (!root) return;
  root.replaceChildren();
  const gpuHeadline = fleet.ready_count
    ? `${fleet.full_gpu_count}/${fleet.ready_count} pods ≥95% GPU`
    : "no active pods";
  for (const [label, value] of [
    ["Pods at full GPU", gpuHeadline],
    ["Avg GPU (active pods)", fleet.avg_gpu_pct === null ? "—" : `${Math.round(fleet.avg_gpu_pct)}%`],
    ["Avg CPU (active pods)", fleet.avg_cpu_pct === null ? "—" : `${Math.round(fleet.avg_cpu_pct)}%`],
    ["Fleet cost", `$${fleet.cost_usd.toFixed(2)}`],
    ["Fleet rate", `$${fleet.rate_per_hr.toFixed(2)}/h`],
  ]) {
    const cell = el("div", "metrics-tile");
    cell.append(el("strong", "", value), el("span", "", label));
    root.append(cell);
  }
}

// Prominent per-pod "right now" row: what nvidia-smi would show on each pod
// at this instant, not diluted by provisioning or already-retired pods (see
// the ready-only average above for why that dilution was the original bug).
// Provisioning pods show "provisioning" rather than a misleading 0%.
function cloudNowTile(pod) {
  const card = el("article", `cloud-now-card cloud-now-${pod.state}`);
  const head = el("div", "cloud-now-head");
  head.append(el("strong", "", pod.label), el("span", `cloud-state ${pod.state}`, pod.state.replaceAll("_", " ")));
  card.append(head);

  // The fleet ledger poll carries what the telemetry payload doesn't: the
  // live provisioning step (tailed from the pod's own provision log) and
  // wall-clock uptime — merge them in so a creating pod shows real progress.
  const info = lastCloudFleet?.pods?.find(p => p.pod_id === pod.pod_id);
  const last = pod.series.at(-1);
  if (pod.state === "creating") {
    const up = info ? ` · ${formatUptime(info.uptime_s)}` : "";
    card.append(el("div", "cloud-now-gpu muted", `provisioning${up}`));
    if (info?.provisioning_hint) card.append(el("div", "resource-job muted provision-step", info.provisioning_hint));
  } else if (!last || last.gpu_pct === null || last.gpu_pct === undefined) {
    card.append(el("div", "cloud-now-gpu muted", "—"));
  } else {
    const gpu = Math.round(last.gpu_pct);
    card.append(el("div", `cloud-now-gpu${gpu >= 95 ? " full" : ""}`, `${gpu}%`));
  }
  const gpuMem = last && last.gpu_mem_pct !== null && last.gpu_mem_pct !== undefined ? `${Math.round(last.gpu_mem_pct)}%` : "—";
  const cpu = last && last.cpu_pct !== null && last.cpu_pct !== undefined ? `${Math.round(last.cpu_pct)}%` : "—";
  const ram = last && last.mem_pct !== null && last.mem_pct !== undefined ? `${Math.round(last.mem_pct)}%` : "—";
  const meta = el("div", "cloud-meta");
  meta.append(cloudStat("GPU mem", gpuMem), cloudStat("CPU", cpu), cloudStat("RAM", ram), cloudStat("Accrued", `$${pod.cost_usd.toFixed(2)}`));
  if (pod.current_unit !== null && pod.current_unit !== undefined) meta.append(cloudStat("Unit", `#${pod.current_unit + 1}`));
  card.append(meta);
  return card;
}

// Every non-terminated pod is mid-lifecycle (creating/ready/terminating) and
// worth a live tile — filtering to ready-only would hide the provisioning
// and teardown steps the tiles exist to make watchable.
function livePods(pods) {
  return pods.filter(pod => pod.state !== "terminated");
}

function renderCloudPodNowTiles(pods) {
  const root = document.getElementById("cloud-pod-now");
  if (!root) return;
  const live = livePods(pods);
  root.replaceChildren(...(live.length ? live.map(cloudNowTile) : [el("div", "empty-state", "No live pods.")]));
}

let cloudMetricsLoading = false;
let lastPodMetricsPayload = null;
async function loadCloudPodMetrics() {
  const panel = document.getElementById("cloud-metrics");
  if (!panel || cloudMetricsLoading || document.hidden) return;
  cloudMetricsLoading = true;
  try {
    const payload = await api(`/api/cloud/pod-metrics?minutes=${metricsMinutes}`);
    lastPodMetricsPayload = payload;
    renderResourcesNow();
    panel.classList.toggle("hidden", !payload.enabled);
    if (payload.enabled) {
      for (const spec of CLOUD_METRIC_CHARTS) drawCloudMetricChart(spec, payload.pods);
      renderCloudPodNowTiles(payload.pods);
      renderCloudMetricsLegend(payload.pods);
      renderCloudMetricsTiles(payload.fleet);
      const note = document.getElementById("cloud-metrics-note");
      if (note) {
        const when = payload.last_sample_at ? new Date(payload.last_sample_at).toLocaleTimeString() : "never";
        const age = payload.last_sample_age_s;
        const ageText = age === null || age === undefined ? "" : ` (as of ${Math.round(age)}s ago)`;
        note.textContent = payload.stale
          ? `Stale — last sample ${when}${ageText}${payload.last_error ? ` (${payload.last_error})` : ""}`
          : `Updated ${when}${ageText}`;
        note.classList.toggle("stale", payload.stale);
      }
    }
  } catch (error) { /* pod metrics are auxiliary; never toast-spam the queue page */ }
  finally { cloudMetricsLoading = false; }
}

// ---- System load graph (queue page) ----
const METRIC_SERIES = [
  {key: "gpu_pct", label: "GPU", color: "#1baf7a"},
  {key: "cpu_pct", label: "CPU", color: "#2a78d6"},
  // GPU temperature in °C, plotted on the shared 0–100 axis (GPU temps live in
  // that range; the throttle threshold ~85 reads near the top). Warm colour.
  {key: "gpu_temp_c", label: "Temp°C", color: "#d1663a"},
];
let metricsMinutes = 60;
let metricsSamples = [];

function drawMetricsChart() {
  const canvas = document.getElementById("metrics-chart");
  if (!canvas) return;
  const {ctx, width, height} = sizeChartCanvas(canvas, 220);
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
    renderResourcesNow();
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
    loadCloudPodMetrics();
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
else if (page === "queue") {
  initQueueFilter(); loadQueue(); window.setInterval(loadQueue, 2000); initMetrics();
  initCloudPodFilter(); loadCloudFleet(); window.setInterval(loadCloudFleet, 3000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) loadCloudFleet(); });
}
