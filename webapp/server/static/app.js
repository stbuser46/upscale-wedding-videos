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
      for (const [value, label] of [[disc.title_count,"titles"],[disc.chapter_count,"chapters"],[`${disc.proxies_ready}/${disc.chapter_count}`,"proxies ready"]]) {
        const cell = el("div"); cell.append(el("strong", "", value), el("span", "", label)); stats.append(cell);
      }
      const titles = el("div");
      for (const title of payload.titles) {
        const row = el("div", "title-row");
        const link = el("a", "", `Title ${String(title.title_number).padStart(2,"0")}`); link.href = `/titles/${title.id}`;
        const left = el("div"); left.append(link, el("small", "", `${formatTime(title.duration_ms)} · ${title.chapter_count} chapters · ${title.video.aspect || "unknown aspect"}`));
        const badges = el("div");
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
  const thumb = el("div", "chapter-thumb");
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
  const top = el("div", "chapter-topline"); top.append(el("span", "", `Chapter ${String(chapter.chapter_number).padStart(2,"0")}`), el("span", "", formatTime(chapter.duration_ms)));
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
  card.chapterId = chapter.id; card.checkbox = checkbox;
  return card;
}

async function loadTitle() {
  const titleId = document.body.dataset.titleId;
  try {
    const payload = await api(`/api/titles/${titleId}/chapters`);
    const title = payload.title;
    document.getElementById("title-kicker").textContent = `${title.disc_slug.toUpperCase()} · DVD title ${String(title.title_number).padStart(2,"0")}`;
    document.getElementById("title-name").textContent = `${title.disc_label} — Title ${title.title_number}`;
    document.getElementById("title-meta").textContent = `${formatTime(title.duration_ms)} · ${payload.chapters.length} chapters · ${title.video.format} ${title.video.width}×${title.video.height} · ${title.video.aspect}`;
    const grid = document.getElementById("chapters"); const queueButton = document.getElementById("queue-selected"); const count = document.getElementById("selected-count");
    const updateSelection = () => { const selected = [...grid.querySelectorAll(".select-check:checked")]; count.textContent = selected.length; queueButton.disabled = !selected.length; };
    grid.replaceChildren(...payload.chapters.map(ch => chapterCard(ch, updateSelection)));
    queueButton.addEventListener("click", async () => {
      const ids = [...grid.querySelectorAll(".chapter-card")].filter(card => card.checkbox.checked).map(card => card.chapterId);
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

let queueLoading = false;
async function loadQueue() {
  if (queueLoading) return; queueLoading = true;
  const root = document.getElementById("queue-list");
  try { const jobs = await api("/api/jobs"); root.replaceChildren(...(jobs.length ? jobs.map(queueCard) : [el("div", "empty-state", "The restoration queue is empty. Choose chapters or a custom slice from the library.")])); }
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

const page = document.body.dataset.page;
if (page === "library" && document.body.dataset.titleId) loadTitle();
else if (page === "library") loadLibrary();
else if (page === "queue" && document.body.dataset.publicId) { loadJob(); window.setInterval(loadJob, 2000); }
else if (page === "queue") { loadQueue(); window.setInterval(loadQueue, 2000); }
