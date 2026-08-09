/* Vectra-180 dashcam UI.
 *
 * Plain ES2020, no build step and no dependencies: the Pi serves these files
 * straight off disk and may have no route to the internet.
 *
 * The token, when the service requires one, is read from the URL the operator
 * opened and appended to every request. It is kept in memory only -- writing
 * it to localStorage would leave it on any phone that ever paired with the
 * car.
 */

"use strict";

const TOKEN = new URLSearchParams(location.search).get("token") || "";
const STATUS_INTERVAL_MS = 1000;
const CLIP_INTERVAL_MS = 15000;

/** Append the auth token to a same-origin path. */
function url(path) {
  if (!TOKEN) return path;
  return path + (path.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(TOKEN);
}

async function api(path, options = {}) {
  const response = await fetch(url(path), { cache: "no-store", ...options });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      detail = (await response.json()).error || detail;
    } catch {
      /* a non-JSON error body is still worth reporting by status text */
    }
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

const $ = (id) => document.getElementById(id);

let toastTimer = 0;
function toast(message, isError = false) {
  const node = $("toast");
  node.textContent = message;
  node.classList.toggle("is-error", isError);
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    node.hidden = true;
  }, 3200);
}

/* --- tabs --------------------------------------------------------------- */

const TABS = [
  ["tab-live", "panel-live"],
  ["tab-clips", "panel-clips"],
  ["tab-system", "panel-system"],
];

function selectTab(activeId) {
  for (const [tabId, panelId] of TABS) {
    const isActive = tabId === activeId;
    const tab = $(tabId);
    tab.classList.toggle("is-active", isActive);
    tab.setAttribute("aria-selected", String(isActive));
    $(panelId).hidden = !isActive;
    $(panelId).classList.toggle("is-active", isActive);
  }
  if (activeId === "tab-clips") refreshClips();
}

for (const [tabId] of TABS) {
  $(tabId).addEventListener("click", () => selectTab(tabId));
}

/* --- live preview ------------------------------------------------------- */

const preview = $("preview");
let previewPaused = false;
// The recorder always writes the raw side-by-side frame; the panorama is a
// viewing choice made per request, so it lives here and not in the config.
let panorama = false;

function startPreview() {
  // A cache-busting parameter forces the browser to reopen the multipart
  // stream after a pause; without it Safari replays the closed response.
  const params = new URLSearchParams({ t: String(Date.now()) });
  if (panorama) params.set("view", "pano");
  preview.src = url("/stream.mjpg") + (TOKEN ? "&" : "?") + params;
  $("preview-error").hidden = true;
}

function setPaused(paused) {
  previewPaused = paused;
  const button = $("btn-pause");
  button.setAttribute("aria-pressed", String(paused));
  button.textContent = paused ? "Resume preview" : "Pause preview";
}

function stopPreview() {
  preview.removeAttribute("src");
}

preview.addEventListener("error", () => {
  if (!previewPaused) $("preview-error").hidden = false;
});

$("btn-pause").addEventListener("click", () => {
  setPaused(!previewPaused);
  if (previewPaused) stopPreview();
  else startPreview();
});

$("btn-pano").addEventListener("click", (event) => {
  panorama = !panorama;
  event.currentTarget.setAttribute("aria-pressed", String(panorama));
  // Switching the view is a request to look at it, so a paused preview -- or
  // one showing a depth map -- comes back to life.
  setPaused(false);
  startPreview();
});

// Releasing the stream while the tab is hidden stops the Pi encoding JPEGs
// nobody is looking at.
document.addEventListener("visibilitychange", () => {
  if (document.hidden) stopPreview();
  else if (!previewPaused) startPreview();
});

$("btn-lock").addEventListener("click", async () => {
  try {
    await api("/api/lock", { method: "POST" });
    toast("Current clip locked");
    refreshClips();
  } catch (error) {
    toast(error.message, true);
  }
});

$("btn-depth").addEventListener("click", () => {
  setPaused(true);
  preview.src = url("/depth.jpg") + (TOKEN ? "&" : "?") + "t=" + Date.now();
  toast("Depth map computed from the current frame");
});

$("btn-snapshot").addEventListener("click", () => {
  const link = document.createElement("a");
  link.href = url("/snapshot.jpg" + (panorama ? "?view=pano" : ""));
  link.download = "vectra-snapshot.jpg";
  link.click();
});

/* --- status ------------------------------------------------------------- */

function formatBytes(bytes) {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
  return (bytes / 1024 ** index).toFixed(index ? 1 : 0) + " " + units[index];
}

function formatDuration(seconds) {
  const total = Math.round(seconds || 0);
  const minutes = Math.floor(total / 60);
  return minutes + ":" + String(total % 60).padStart(2, "0");
}

function renderStatus(status) {
  const pill = $("rec-pill");
  const recording = status.recorder && status.recorder.written_frames >= 0 && status.recorder.current_clip;
  pill.className = "pill " + (recording ? "pill-rec" : status.error ? "pill-error" : "pill-idle");
  pill.textContent = recording ? "RECORDING" : status.error ? "ERROR" : "STANDBY";

  $("fps").textContent = (status.fps || 0).toFixed(1) + " fps";

  const orientation = (status.telemetry && status.telemetry.orientation) || {};
  $("t-roll").textContent = orientation.roll === undefined ? "--" : orientation.roll.toFixed(1) + "°";
  $("t-pitch").textContent = orientation.pitch === undefined ? "--" : orientation.pitch.toFixed(1) + "°";
  $("t-yaw").textContent = orientation.yaw === undefined ? "--" : orientation.yaw.toFixed(1) + "°";
  $("t-peak").textContent = status.incidents ? status.incidents.peak_g.toFixed(2) + " g" : "--";

  renderFacts(status);
  renderStorage(status.storage);
}

function renderFacts(status) {
  const camera = status.camera || {};
  const recorder = status.recorder || {};
  const telemetry = status.telemetry || {};
  const facts = [
    ["Uptime", formatDuration(status.uptime_seconds)],
    ["Camera", `${camera.width}×${camera.height} @ ${camera.fps} fps (${camera.backend})`],
    ["Encoder", recorder.encoder || "not started"],
    ["Current clip", recorder.current_clip || "—"],
    ["Segments written", recorder.segments_written],
    ["Frames written", recorder.written_frames],
    ["Frames dropped", recorder.dropped_frames],
    ["Telemetry", telemetry.present ? `present (${telemetry.decoded_frames} frames)` : "not detected"],
    ["Incidents", status.incidents ? status.incidents.count : 0],
  ];
  if (status.error) facts.push(["Error", status.error]);
  if (recorder.last_error) facts.push(["Recorder error", recorder.last_error]);

  const list = $("facts");
  list.textContent = "";
  for (const [term, value] of facts) {
    const dt = document.createElement("dt");
    dt.textContent = term;
    const dd = document.createElement("dd");
    dd.className = "mono";
    dd.textContent = value === undefined || value === null ? "—" : String(value);
    list.append(dt, dd);
  }
}

function renderStorage(storage) {
  if (!storage || !storage.total_bytes) return;
  const total = storage.total_bytes;
  $("bar-normal").style.width = ((storage.normal_bytes / total) * 100).toFixed(2) + "%";
  $("bar-events").style.width = ((storage.event_bytes / total) * 100).toFixed(2) + "%";
  $("bar-normal").title = `Loop footage: ${formatBytes(storage.normal_bytes)}`;
  $("bar-events").title = `Locked footage: ${formatBytes(storage.event_bytes)}`;
}

async function refreshStatus() {
  try {
    renderStatus(await api("/api/status"));
  } catch (error) {
    const pill = $("rec-pill");
    pill.className = "pill pill-error";
    pill.textContent = "OFFLINE";
    console.warn("status poll failed:", error.message);
  }
}

/* --- clips -------------------------------------------------------------- */

let clipFilter = "all";

for (const chip of document.querySelectorAll(".chip")) {
  chip.addEventListener("click", () => {
    clipFilter = chip.dataset.filter;
    for (const other of document.querySelectorAll(".chip")) {
      other.classList.toggle("is-active", other === chip);
    }
    refreshClips();
  });
}

function clipRow(clip) {
  const item = document.createElement("li");
  item.className = "clip";

  const main = document.createElement("div");
  main.className = "clip-main";

  const name = document.createElement("div");
  name.className = "clip-name mono";
  name.textContent = clip.name;
  main.append(name);

  const meta = document.createElement("div");
  meta.className = "clip-meta";
  const started = clip.started_at ? new Date(clip.started_at).toLocaleString() : "unknown time";
  meta.textContent = `${started} · ${formatDuration(clip.duration_seconds)} · ${formatBytes(clip.size_bytes)}`;
  main.append(meta);
  item.append(main);

  if (clip.protected) {
    const badge = document.createElement("span");
    badge.className = "badge";
    badge.textContent = "LOCKED";
    item.append(badge);
  }

  const actions = document.createElement("div");
  actions.className = "clip-actions";

  const download = document.createElement("a");
  download.className = "icon-btn";
  download.href = url("/api/clips/" + encodeURIComponent(clip.name));
  download.textContent = "Download";
  actions.append(download);

  if (!clip.protected) {
    const lock = document.createElement("button");
    lock.className = "icon-btn";
    lock.textContent = "Lock";
    lock.addEventListener("click", async () => {
      try {
        await api("/api/clips/" + encodeURIComponent(clip.name) + "/protect", { method: "POST" });
        toast(clip.name + " locked");
        refreshClips();
      } catch (error) {
        toast(error.message, true);
      }
    });
    actions.append(lock);
  }

  const remove = document.createElement("button");
  remove.className = "icon-btn danger";
  remove.textContent = "Delete";
  remove.addEventListener("click", async () => {
    if (!confirm(`Delete ${clip.name}? This cannot be undone.`)) return;
    try {
      await api("/api/clips/" + encodeURIComponent(clip.name), { method: "DELETE" });
      toast(clip.name + " deleted");
      refreshClips();
    } catch (error) {
      toast(error.message, true);
    }
  });
  actions.append(remove);

  item.append(actions);
  return item;
}

async function refreshClips() {
  try {
    const { clips } = await api("/api/clips");
    const visible = clipFilter === "all" ? clips : clips.filter((clip) => clip.category === clipFilter);
    const list = $("clip-list");
    list.textContent = "";
    for (const clip of visible) list.append(clipRow(clip));
    $("clip-empty").hidden = visible.length > 0;
  } catch (error) {
    toast(error.message, true);
  }
}

/* --- boot --------------------------------------------------------------- */

startPreview();
refreshStatus();
refreshClips();
setInterval(refreshStatus, STATUS_INTERVAL_MS);
setInterval(() => {
  if (!document.hidden && !$("panel-clips").hidden) refreshClips();
}, CLIP_INTERVAL_MS);
