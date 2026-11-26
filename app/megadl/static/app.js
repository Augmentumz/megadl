"use strict";

const $ = (sel, el = document) => el.querySelector(sel);

let redirecting = false;
async function api(method, url, body) {
  const opts = { method, headers: { "X-MegaDL": "1" } };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(url, opts);
  let data = {};
  try { data = await res.json(); } catch (_) { /* empty body */ }
  if (res.status === 401 && data.login) {
    redirecting = true;
    location.replace("/login");
  }
  if (!res.ok) {
    const err = new Error(data.error || data.message || `Request failed (${res.status})`);
    err.data = data;
    throw err;
  }
  return data;
}

/* ----------------------------------------------------------- formatting */
function fmtBytes(n) {
  if (!n || n < 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  const digits = i === 0 ? 0 : n < 10 ? 2 : n < 100 ? 1 : 0;
  return `${n.toFixed(digits)} ${units[i]}`;
}
const fmtSpeed = (n) => `${fmtBytes(n)}/s`;
function fmtEta(s) {
  if (s == null) return "";
  if (s < 60) return "under a minute left";
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min left`;
  const h = Math.floor(m / 60);
  if (h >= 48) return `about ${Math.round(h / 24)} days left`;
  return `${h} h ${m % 60} min left`;
}

const ACTIVE = new Set(["queued", "starting", "pending", "downloading", "waiting"]);
const STATUS_TEXT = {
  queued: "Queued",
  starting: "Starting",
  pending: "Waiting for a free slot",
  downloading: "Downloading",
  waiting: "Retrying",
  paused: "Paused",
  completed: "Done",
  failed: "Failed",
};
const STATUS_HELP = {
  waiting: "MEGA is retrying, usually because the transfer quota ran out or the connection dropped. It continues on its own.",
  pending: "MEGAcmd runs a few transfers at a time; this one starts when a slot frees up.",
  starting: "Handing the link to MEGAcmd.",
};

/* ----------------------------------------------------------------- state */
let state = null;
let addDest = null;           // { path, display, disk } chosen for the add form
let settingsDest = null;      // { path, display } chosen in settings
let settingsDirty = false;
let lastLimit = null;
let connectionLost = false;

/* ------------------------------------------------------------------ toasts */
function toast(message, kind = "ok") {
  const el = document.createElement("div");
  el.className = `toast ${kind === "error" ? "error" : ""}`;
  el.textContent = message;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), kind === "error" ? 7000 : 3500);
}

/* -------------------------------------------------------------------- tabs */
const tabs = [...document.querySelectorAll('[role="tab"]')];
function showView(name, focus = false) {
  if (!tabs.some((t) => t.dataset.view === name)) name = "downloads";
  for (const t of tabs) {
    const on = t.dataset.view === name;
    t.setAttribute("aria-selected", on ? "true" : "false");
    t.tabIndex = on ? 0 : -1;
    $(`#view-${t.dataset.view}`).hidden = !on;
    if (on && focus) t.focus();
  }
  if (name === "settings") { settingsDirty = false; fillSettings(true); }
  history.replaceState(null, "", name === "downloads" ? location.pathname : `#${name}`);
}
for (const t of tabs) {
  t.addEventListener("click", () => showView(t.dataset.view));
  t.addEventListener("keydown", (e) => {
    const i = tabs.indexOf(t);
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      const next = tabs[(i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
      showView(next.dataset.view, true);
      e.preventDefault();
    }
  });
}

/* ----------------------------------------------------------------- render */
function render() {
  if (!state) {
    const banner = $("#banner");
    banner.hidden = !connectionLost;
    banner.textContent = "Can't reach MegaDL. Trying again…";
    return;
  }
  $("#total-speed").textContent = fmtSpeed(state.speed);
  document.title = state.speed > 0 ? `MegaDL (${fmtSpeed(state.speed)})` : "MegaDL";
  renderBanner();
  renderQuickLimit();
  renderAddDest();
  renderJobs(state.jobs);
  renderSummary(state.jobs);
  renderAccount();
  fillSettings(false);
  $("#about-root").textContent = `${state.root_label} (inside the container: /data)`;
  $("#about-auth").textContent = state.auth_enabled
    ? "On"
    : "Off. Set Web GUI password in the container settings to require a sign-in.";
  $("#signout").hidden = !state.auth_enabled;
  $("#about-version").textContent = state.version;
}

function renderBanner() {
  const banner = $("#banner");
  let text = "", info = false;
  if (connectionLost) {
    text = "Lost connection to MegaDL. Trying again…";
  } else if (state.server.state === "starting") {
    text = "Starting MEGAcmd. Downloads will begin in a moment.";
    info = true;
  } else if (state.server.state === "down") {
    text = state.server.message || "MEGAcmd isn't running.";
  } else if (state.poll_error) {
    text = `Couldn't read transfers from MEGAcmd: ${state.poll_error}`;
  }
  banner.hidden = !text;
  banner.textContent = text;
  banner.classList.toggle("info", info);
}

const LIMIT_PRESETS = [0, 256, 512, 1024, 2048, 5120, 10240, 25600, 51200].map((k) => k * 1024);
function renderQuickLimit() {
  const select = $("#quick-limit");
  const limit = state.settings.speed_limit || 0;
  if (limit === lastLimit || document.activeElement === select) return;
  lastLimit = limit;
  select.innerHTML = "";
  const values = LIMIT_PRESETS.includes(limit) ? LIMIT_PRESETS : [...LIMIT_PRESETS, limit].sort((a, b) => a - b);
  for (const v of values) {
    const opt = new Option(v === 0 ? "Unlimited" : fmtSpeed(v), String(v));
    opt.selected = v === limit;
    select.add(opt);
  }
  select.add(new Option("Custom…", "custom"));
}
$("#quick-limit").addEventListener("change", async (e) => {
  const value = e.target.value;
  if (value === "custom") {
    lastLimit = null;
    showView("settings");
    $("#limit-value").focus();
    return;
  }
  try {
    const settings = await api("PUT", "/api/settings", { speed_limit: Number(value) });
    state.settings = settings;
    lastLimit = null;
    toast(Number(value) ? `Speed limited to ${fmtSpeed(Number(value))}` : "Speed limit removed");
  } catch (err) {
    toast(err.message, "error");
  }
  e.target.blur();
});

function renderAddDest() {
  const btn = $("#add-dest");
  const display = addDest ? addDest.display : state.default_dest_display;
  if (btn.textContent !== display) { btn.textContent = display; btn.title = `Downloads go to ${display}. Click to choose another folder.`; }
  const disk = addDest ? addDest.disk : state.disk;
  $("#add-free").textContent = disk ? `${fmtBytes(disk.free)} free` : "";
}

function renderSummary(jobs) {
  const count = (pred) => jobs.filter(pred).length;
  const parts = [
    [count((j) => j.status === "downloading"), "downloading"],
    [count((j) => ["queued", "starting", "pending", "waiting"].includes(j.status)), "waiting"],
    [count((j) => j.status === "paused"), "paused"],
    [count((j) => j.status === "failed"), "failed"],
    [count((j) => j.status === "completed"), "done"],
  ].filter(([n]) => n > 0);
  const el = $("#summary");
  el.innerHTML = "";
  parts.forEach(([n, label], i) => {
    const b = document.createElement("b");
    b.textContent = n;
    el.append(b, ` ${label}${i < parts.length - 1 ? ", " : ""}`);
  });
  const hasActive = jobs.some((j) => ACTIVE.has(j.status));
  const hasPaused = jobs.some((j) => j.status === "paused");
  $("#pause-all").hidden = !hasActive;
  $("#resume-all").hidden = !hasPaused;
  $("#clear-finished").hidden = !jobs.some((j) => j.status === "completed");
}

/* --------------------------------------------------------------- job rows */
const rows = new Map();

function icon(name, cls = "ic") {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", cls);
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}
function setIcon(svg, name) { svg.firstChild.setAttribute("href", `#i-${name}`); }
function span(cls) { const s = document.createElement("span"); s.className = cls; return s; }
function iconButton(act, iconName, cls = "") {
  const b = document.createElement("button");
  b.type = "button";
  b.className = `iconbtn ${cls}`;
  b.dataset.act = act;
  b.append(icon(iconName));
  return b;
}

function buildRow(job) {
  const li = document.createElement("li");
  li.className = "job";
  li.dataset.id = job.id;
  const fill = document.createElement("div");
  fill.className = "job-fill";
  const main = document.createElement("div");
  main.className = "job-main";
  const name = document.createElement("div");
  name.className = "job-name";
  const meta = document.createElement("div");
  meta.className = "job-meta";
  meta.append(span("job-state"), span("size"), span("files"), span("speed"), span("eta"), span("where"));
  main.append(name, meta);
  const pct = document.createElement("div");
  pct.className = "job-pct";
  const actions = document.createElement("div");
  actions.className = "job-actions";
  actions.append(iconButton("toggle", "pause"), iconButton("retry", "retry"), iconButton("delete", "trash", "del"));
  const error = document.createElement("p");
  error.className = "job-error";
  li.append(fill, icon("file", "job-kind"), main, pct, actions, error);
  return li;
}

function setText(el, text, keepSpace = false) {
  if (el.textContent !== text) el.textContent = text;
  if (!keepSpace) el.hidden = !text;
}

function updateRow(li, job) {
  li.dataset.status = job.status;
  li.style.setProperty("--pct", `${Math.min(100, job.percent).toFixed(2)}%`);
  setIcon(li.querySelector(".job-kind"), job.kind === "folder" ? "folder" : "file");

  const name = li.querySelector(".job-name");
  setText(name, job.name);
  name.title = job.named ? job.name : job.link;
  name.classList.toggle("provisional", !job.named);

  const q = (s) => li.querySelector(s);
  let stateText = STATUS_TEXT[job.status] || job.status;
  if (job.status === "starting" && job.kind === "folder") stateText = "Reading folder";
  setText(q(".job-state"), stateText);
  q(".job-state").title = STATUS_HELP[job.status] || "";
  setText(q(".size"), job.total ? (job.status === "completed" ? fmtBytes(job.total) : `${fmtBytes(job.done)} of ${fmtBytes(job.total)}`) : "");
  setText(q(".files"), job.kind === "folder" && job.files_total > 1 ? `${job.files_done} of ${job.files_total} files` : "");
  setText(q(".speed"), job.status === "downloading" && job.speed > 0 ? fmtSpeed(job.speed) : "");
  setText(q(".eta"), job.status === "downloading" ? fmtEta(job.eta) : "");
  setText(q(".where"), `to ${job.dest}`);
  q(".where").title = job.dest;
  setText(q(".job-pct"), job.total || job.status === "completed" ? `${Math.floor(job.percent)}%` : "", true);
  setText(q(".job-error"), job.status === "failed" && job.error ? job.error : "");

  const toggle = q('[data-act="toggle"]');
  const canPause = ACTIVE.has(job.status);
  const canResume = job.status === "paused";
  toggle.hidden = !(canPause || canResume);
  setIcon(toggle.firstChild, canPause ? "pause" : "play");
  const toggleLabel = `${canPause ? "Pause" : "Resume"} ${job.name}`;
  toggle.setAttribute("aria-label", toggleLabel);
  toggle.title = canPause ? "Pause" : "Resume";
  const retry = q('[data-act="retry"]');
  retry.hidden = job.status !== "failed";
  retry.setAttribute("aria-label", `Retry ${job.name}`);
  retry.title = "Retry";
  const del = q('[data-act="delete"]');
  del.setAttribute("aria-label", `Remove ${job.name}`);
  del.title = "Remove";
}

function renderJobs(jobs) {
  const list = $("#jobs");
  const seen = new Set();
  jobs.forEach((job, index) => {
    let li = rows.get(job.id);
    if (!li) { li = buildRow(job); rows.set(job.id, li); }
    updateRow(li, job);
    seen.add(job.id);
    if (list.children[index] !== li) list.insertBefore(li, list.children[index] || null);
  });
  for (const [id, li] of rows) {
    if (!seen.has(id)) { li.remove(); rows.delete(id); }
  }
  $("#empty").hidden = jobs.length > 0;
}

$("#jobs").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const id = Number(btn.closest(".job").dataset.id);
  const job = state.jobs.find((j) => j.id === id);
  if (!job) return;
  try {
    if (btn.dataset.act === "toggle") {
      await api("POST", `/api/downloads/${id}/${ACTIVE.has(job.status) ? "pause" : "resume"}`);
    } else if (btn.dataset.act === "retry") {
      await api("POST", `/api/downloads/${id}/retry`);
    } else if (btn.dataset.act === "delete") {
      openRemove(job);
      return;
    }
    refresh();
  } catch (err) {
    toast(err.message, "error");
  }
});

for (const [id, url, done] of [
  ["#pause-all", "/api/downloads/pause-all", "Paused all downloads"],
  ["#resume-all", "/api/downloads/resume-all", "Resumed all downloads"],
  ["#clear-finished", "/api/downloads/clear-finished", "Cleared finished downloads"],
]) {
  $(id).addEventListener("click", async () => {
    try { await api("POST", url); toast(done); refresh(); } catch (err) { toast(err.message, "error"); }
  });
}

/* --------------------------------------------------------------- add form */
const addForm = $("#add-form");
addForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const links = $("#links").value.trim();
  if (!links) { $("#links").focus(); return; }
  const submit = $("#add-submit");
  submit.disabled = true;
  try {
    const res = await api("POST", "/api/downloads", {
      links,
      dest: addDest ? addDest.path : null,
      password: $("#link-password").value || null,
    });
    $("#links").value = "";
    $("#link-password").value = "";
    const n = res.ids.length;
    let msg = n ? `Added ${n} download${n === 1 ? "" : "s"}` : "Nothing new to add";
    if (res.skipped) msg += `. ${res.skipped} already in the list for that folder.`;
    toast(msg);
    refresh();
  } catch (err) {
    toast(err.message, "error");
  } finally {
    submit.disabled = false;
  }
});
$("#links").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) addForm.requestSubmit();
});
$("#toggle-password").addEventListener("click", (e) => {
  const row = $("#password-row");
  row.hidden = !row.hidden;
  e.target.setAttribute("aria-expanded", String(!row.hidden));
  if (!row.hidden) $("#link-password").focus();
});
$("#add-dest").addEventListener("click", () => {
  openPicker(addDest ? addDest.path : state.settings.default_dest, (choice) => {
    addDest = choice;
    renderAddDest();
  });
});

/* ---------------------------------------------------------- folder picker */
const picker = $("#picker");
let pickerPath = "";
let pickerData = null;
let pickerDone = null;

async function loadFolder(path) {
  $("#picker-error").hidden = true;
  try {
    pickerData = await api("GET", `/api/folders?path=${encodeURIComponent(path || "")}`);
  } catch (err) {
    if (path) return loadFolder("");
    showPickerError(err.message);
    return;
  }
  pickerPath = pickerData.path;
  const crumbs = $("#picker-crumbs");
  crumbs.innerHTML = "";
  const parts = pickerPath ? pickerPath.split("/") : [];
  const crumb = (label, target, current) => {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = label;
    if (current) b.setAttribute("aria-current", "location");
    b.addEventListener("click", () => loadFolder(target));
    return b;
  };
  crumbs.append(crumb(state.root_label, "", parts.length === 0));
  parts.forEach((p, i) => {
    const slash = span("slash");
    slash.textContent = "/";
    crumbs.append(slash, crumb(p, parts.slice(0, i + 1).join("/"), i === parts.length - 1));
  });
  const list = $("#picker-list");
  list.innerHTML = "";
  const item = (label, iconName, target, cls = "") => {
    const li = document.createElement("li");
    const b = document.createElement("button");
    b.type = "button";
    b.className = cls;
    b.append(icon(iconName), document.createTextNode(label));
    b.addEventListener("click", () => loadFolder(target));
    li.append(b);
    list.append(li);
  };
  if (pickerData.parent !== null) item("Up one level", "up", pickerData.parent, "up");
  for (const f of pickerData.folders) item(f.name, "folder", f.path);
  if (!pickerData.folders.length) {
    const li = document.createElement("li");
    li.className = "none";
    li.textContent = "No folders inside. Choose this one or create a new folder.";
    list.append(li);
  }
  $("#picker-choose").disabled = !pickerData.writable;
  if (!pickerData.writable) showPickerError("MegaDL can't write to this folder. Check the share's permissions.");
}
function showPickerError(msg) {
  const el = $("#picker-error");
  el.textContent = msg;
  el.hidden = false;
}
function openPicker(start, done) {
  pickerDone = done;
  $("#picker-new").value = "";
  picker.showModal();
  loadFolder(start || "");
}
$("#picker-choose").addEventListener("click", () => {
  if (!pickerData) return;
  pickerDone && pickerDone({ path: pickerPath, display: pickerData.display, disk: pickerData.disk });
  picker.close();
});
async function createFolder() {
  const name = $("#picker-new").value.trim();
  if (!name) { $("#picker-new").focus(); return; }
  try {
    const res = await api("POST", "/api/folders", { path: pickerPath, name });
    $("#picker-new").value = "";
    loadFolder(res.path);
  } catch (err) {
    showPickerError(err.message);
  }
}
$("#picker-create").addEventListener("click", createFolder);
$("#picker-new").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); createFolder(); }
});

/* --------------------------------------------------------- remove dialog */
const removeDialog = $("#remove");
let removeJob = null;
function openRemove(job) {
  removeJob = job;
  $("#remove-name").textContent = job.name;
  const active = ACTIVE.has(job.status) || job.status === "paused";
  $("#remove-note").textContent = active
    ? "This stops the download. MEGAcmd discards the unfinished part of the file it is working on."
    : "This removes it from the list.";
  $("#remove-files-row").hidden = !job.can_delete_files;
  $("#remove-files").checked = false;
  removeDialog.showModal();
}
$("#remove-confirm").addEventListener("click", async () => {
  if (!removeJob) return;
  const files = !$("#remove-files-row").hidden && $("#remove-files").checked;
  removeDialog.close();
  try {
    await api("DELETE", `/api/downloads/${removeJob.id}?files=${files ? 1 : 0}`);
    toast(files ? "Removed the download and its files" : "Removed the download");
    refresh();
  } catch (err) {
    toast(err.message, "error");
  }
});

/* ---------------------------------------------------------------- account */
function renderAccount() {
  const { email, checked } = state.account;
  $("#account-in").hidden = !email;
  $("#login-form").hidden = !!email || !checked;
  $("#account-email").textContent = email || "";
}
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("#login-submit");
  const errEl = $("#login-error");
  errEl.hidden = true;
  btn.disabled = true;
  btn.textContent = "Signing in…";
  try {
    const res = await api("POST", "/api/account/login", {
      email: $("#email").value,
      secret: $("#password").value,
      code: $("#code").value,
    });
    $("#password").value = "";
    $("#code").value = "";
    toast(`Signed in as ${res.email}`);
    refresh();
  } catch (err) {
    const data = err.data || {};
    errEl.textContent = data.needs_code
      ? "Your account uses two-factor authentication. Enter the current code from your authenticator app and sign in again."
      : err.message || "Sign in failed.";
    errEl.hidden = false;
    if (data.needs_code) $("#code").focus();
  } finally {
    btn.disabled = false;
    btn.textContent = "Sign in";
  }
});
$("#logout").addEventListener("click", async () => {
  const active = state.jobs.some((j) => ACTIVE.has(j.status));
  if (active && !window.confirm("Signing out stops downloads in progress. Sign out anyway?")) return;
  try {
    await api("POST", "/api/account/logout");
    toast("Signed out");
    refresh();
  } catch (err) {
    toast(err.message, "error");
  }
});

/* --------------------------------------------------------------- settings */
function fillSettings(force) {
  if (!state || (settingsDirty && !force)) return;
  const s = state.settings;
  const limit = s.speed_limit || 0;
  const unit = limit && limit % 1048576 !== 0 && limit < 10 * 1048576 ? 1024 : 1048576;
  $("#limit-unit").value = String(unit);
  $("#limit-value").value = limit ? +(limit / unit).toFixed(2) : "";
  $("#connections").value = s.download_connections || "";
  settingsDest = { path: s.default_dest, display: state.default_dest_display };
  $("#settings-dest").textContent = settingsDest.display;
}
$("#settings-form").addEventListener("input", () => { settingsDirty = true; });
$("#settings-dest").addEventListener("click", () => {
  openPicker(settingsDest ? settingsDest.path : "", (choice) => {
    settingsDest = choice;
    settingsDirty = true;
    $("#settings-dest").textContent = choice.display;
  });
});
$("#settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const errEl = $("#settings-error");
  errEl.hidden = true;
  const value = parseFloat($("#limit-value").value);
  const body = {
    speed_limit: Number.isFinite(value) && value > 0 ? Math.round(value * Number($("#limit-unit").value)) : 0,
    download_connections: parseInt($("#connections").value, 10) || 0,
    default_dest: settingsDest ? settingsDest.path : "",
  };
  try {
    state.settings = await api("PUT", "/api/settings", body);
    settingsDirty = false;
    lastLimit = null;
    toast("Settings saved");
    refresh();
  } catch (err) {
    errEl.textContent = err.message;
    errEl.hidden = false;
  }
});

$("#signout").addEventListener("click", async () => {
  try { await api("DELETE", "/api/session"); } catch (_) { /* signing out anyway */ }
  location.replace("/login");
});

/* ---------------------------------------------------------------- polling */
let pollTimer = null;
async function refresh() {
  clearTimeout(pollTimer);
  if (redirecting) return;
  try {
    state = await api("GET", "/api/state");
    connectionLost = false;
  } catch (_) {
    connectionLost = true;
  }
  render();
  pollTimer = setTimeout(refresh, document.hidden ? 5000 : 1500);
}
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });

showView(location.hash.replace("#", "") || "downloads");
refresh();
