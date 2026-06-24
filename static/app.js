// AutoApply dashboard logic. Drives the 5-stage pipeline, polls /api/state,
// and renders status pills, quota cards, and the draft review panel.

const $ = (id) => document.getElementById(id);

const STATUS_LABELS = {
  discovered: "Discovered",
  email_found: "Email found",
  drafted: "Drafted",
  approved: "Approved",
  sent: "Sent",
  replied_interview: "Interview",
  replied_rejection: "Rejection",
  replied_needinfo: "Needs info",
  auto_ack: "Auto-ack",
  bounced: "Bounced",
  no_reply: "No reply",
};

function esc(v) {
  if (v == null) return "";
  return String(v).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function setStatus(msg, isError = false) {
  const el = $("status-msg");
  el.textContent = msg || "";
  el.classList.toggle("error", !!isError);
}

function statusPill(status) {
  if (!status) return '<span class="cell-muted">—</span>';
  const label = STATUS_LABELS[status] || status;
  return `<span class="pill pill-${esc(status)}">${esc(label)}</span>`;
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body.detail) detail = body.detail;
    } catch (_) { /* non-JSON */ }
    throw new Error(detail);
  }
  return res.json();
}

// ===== Rendering =====================================================

function renderState(state) {
  $("banner").textContent = state.banner || "";
  $("dry-badge").classList.toggle("hidden", !state.dry_run);
  $("pause-banner").classList.toggle("hidden", !state.paused);

  // Send budget + usage bar
  const b = state.send_budget || {};
  if (b.cap_today != null) {
    $("q-send").textContent = `${b.remaining} / ${b.cap_today}`;
    const pct = b.cap_today ? Math.min(100, (b.sent_today / b.cap_today) * 100) : 0;
    $("q-send-bar").style.width = `${pct}%`;
  }

  // Bounce rate
  if (state.bounce_rate != null) {
    $("q-bounce").textContent = `${(state.bounce_rate * 100).toFixed(1)}%`;
  }

  // Applications table
  const apps = state.applications || [];
  $("apps-count").textContent = apps.length;
  const body = $("apps-body");
  if (apps.length === 0) {
    body.innerHTML =
      '<tr><td colspan="5" class="empty">No applications yet — start with ① Find Jobs.</td></tr>';
    return;
  }
  body.innerHTML = apps
    .map(
      (a) => `<tr>
        <td class="cell-strong">${esc(a.company)}</td>
        <td>${esc(a.role)}</td>
        <td>${statusPill(a.status)}</td>
        <td class="cell-muted">${esc(fmtDate(a.last_checked_at))}</td>
        <td class="cell-snippet">${esc(a.reply_excerpt) || '<span class="cell-muted">—</span>'}</td>
      </tr>`
    )
    .join("");
}

function fmtDate(iso) {
  if (!iso) return "—";
  return iso.replace("T", " ").slice(0, 16);
}

function renderQuota(q) {
  if (q.cse) {
    $("q-cse").textContent =
      q.cse.used_today != null
        ? `${q.cse.limit_per_day - q.cse.used_today} / ${q.cse.limit_per_day}`
        : `${q.cse.limit_per_day}`;
  }
  if (q.hunter) {
    $("q-hunter").textContent =
      q.hunter.used_this_month != null
        ? `${q.hunter.limit_per_month - q.hunter.used_this_month} / ${q.hunter.limit_per_month}`
        : `${q.hunter.limit_per_month}`;
  }
}

async function refresh() {
  try {
    const [state, quota] = await Promise.all([api("/api/state"), api("/api/quota")]);
    renderState(state);
    renderQuota(quota);
    renderLiStatus(state);
    refreshLiCount();
  } catch (e) {
    setStatus(`Failed to load state: ${e.message}`, true);
  }
}

// ===== Pipeline actions ==============================================

function busy(btn, on) {
  btn.disabled = on;
  btn.classList.toggle("busy", on);
}

function readFilters() {
  const minLpaEl = $("f-min-lpa");
  const maxHcEl = $("f-max-headcount");
  return {
    role: $("f-role").value.trim() || null,
    location: $("f-location").value.trim() || null,
    keywords: $("f-keywords").value.trim() || null,
    remote: $("f-remote").checked,
    min_lpa: minLpaEl && minLpaEl.value.trim() ? Number(minLpaEl.value) : null,
    max_headcount: maxHcEl && maxHcEl.value.trim() ? Number(maxHcEl.value) : null,
  };
}

function resetFilters() {
  ["f-role", "f-location", "f-keywords", "f-min-lpa", "f-max-headcount"].forEach((id) => {
    const el = $(id);
    if (el) el.value = "";
  });
  $("f-remote").checked = true;
  const box = $("find-summary");
  if (box) box.classList.add("hidden");
}

// Render the breakdown so the effect of each filter is visible.
function renderFindSummary(res) {
  const box = $("find-summary");
  if (!box) return;
  box.classList.remove("hidden", "fs-err");

  if (res.fetched === 0 && !res.dry_run) {
    box.classList.add("fs-err");
    box.innerHTML =
      `<div class="fs-headline">No jobs returned</div>
       <div class="fs-sub">${res.message || "LinkedIn may be rate-limiting this IP — wait a few minutes and retry."}</div>`;
    return;
  }

  const roles = (res.roles || []).join(", ");
  const locs = (res.locations || []).join(", ");
  const chip = (label, n) =>
    `<span class="fs-chip ${n ? "" : "fs-zero"}">${label} <b>${n || 0}</b></span>`;

  box.innerHTML =
    `<div class="fs-headline"><strong>${res.new || 0}</strong> new job(s) found · <strong>${res.hr_emails || 0}</strong> with an HR email</div>
     <div class="fs-sub">Searched ${roles || "default roles"} in ${locs || "default geos"}${res.remote ? " · remote only" : ""} · scanned ${res.fetched || 0} listings · Hunter left: ${res.hunter_remaining ?? "—"}</div>
     <div class="fs-chips">
       ${chip("big company", res.big_co_skipped)}
       ${chip("too large", res.too_big_dropped)}
       ${chip("too senior", res.senior_skipped)}
       ${chip("internship", res.interns_skipped)}
       ${chip(`below ${res.min_lpa || 8} LPA`, res.below_salary)}
       ${chip("duplicate", res.duplicates)}
     </div>`;
}

async function runDiscover(btn) {
  const filters = readFilters();
  busy(btn, true);
  setStatus("Finding jobs…");
  try {
    const res = await api("/api/discover", { method: "POST", body: JSON.stringify(filters) });
    setStatus(res.message || "Discovery complete.");
    renderFindSummary(res);
    await refresh();
  } catch (e) {
    setStatus(`Find Jobs failed: ${e.message}`, true);
    const box = $("find-summary");
    if (box) {
      box.classList.remove("hidden");
      box.classList.add("fs-err");
      box.innerHTML = `<div class="fs-headline">Find Jobs failed</div><div class="fs-sub">${e.message}</div>`;
    }
  } finally {
    busy(btn, false);
  }
}

async function refreshLiCount() {
  const pill = $("li-count");
  const queue = $("li-queue");
  if (!pill && !queue) return;
  try {
    const res = await fetch("/api/linkedin/targets").then((r) => r.json());
    if (pill) pill.textContent = res.count ?? 0;
    if (queue) renderLiQueue(queue, res.jobs || []);
  } catch {
    /* ignore */
  }
}

// The actual jobs the agent will apply to, in apply order (urgent/hiring first).
function renderLiQueue(box, jobs) {
  if (!jobs.length) {
    box.innerHTML = '<div class="li-queue-empty">No LinkedIn jobs queued. Run ① Find Jobs to fill this list.</div>';
    return;
  }
  const badge = (u) =>
    u >= 2 ? '<span class="li-tag li-tag-urgent">🔥 Urgent</span>'
    : u === 1 ? '<span class="li-tag li-tag-hiring">Hiring</span>'
    : "";
  const rows = jobs.map((j, i) => {
    const loc = j.location ? `<span class="li-loc">${esc(j.location)}</span>` : "";
    return (
      `<li class="li-row">` +
      `<span class="li-num">${i + 1}</span>` +
      `<div class="li-main"><div class="li-role">${esc(j.role || "Role")} ${badge(j.urgent || 0)}</div>` +
      `<div class="li-sub">${esc(j.company || "Unknown")} ${loc}</div></div>` +
      `<a class="li-open" href="${esc(j.url)}" target="_blank" rel="noopener">open ↗</a>` +
      `</li>`
    );
  });
  box.innerHTML =
    `<div class="li-queue-head">Apply queue — ${jobs.length} job(s), urgent first</div>` +
    `<ol class="li-queue-list">${rows.join("")}</ol>`;
}

async function runLiApply(btn) {
  if (!confirm(
    "This opens your matching LinkedIn Easy-Apply jobs in your logged-in Chrome and " +
    "PRE-FILLS them. You review each (answer any screening questions) and click Submit " +
    "yourself — it never submits for you.\n\nMake sure you've run `formtool.py lilogin` once. Continue?"
  )) return;
  busy(btn, true);
  setStatus("Opening LinkedIn Easy Apply in your browser…");
  try {
    const res = await api("/api/linkedin/apply", { method: "POST", body: JSON.stringify({ limit: 10 }) });
    setStatus(res.message || "LinkedIn apply started.");
  } catch (e) {
    setStatus(`LinkedIn apply failed: ${e.message}`, true);
  } finally {
    busy(btn, false);
  }
}

function renderLiStatus(state) {
  const s = state && state.li_apply_status;
  const box = $("li-result");
  const stop = $("btn-li-stop");
  const auto = $("btn-li-auto");
  if (!box) return;
  const running = !!(s && s.running);
  if (stop) stop.classList.toggle("hidden", !running);
  if (auto) auto.disabled = running;
  if (running) {
    box.classList.remove("hidden");
    box.innerHTML = '<div class="fs-headline">Auto-apply running… watch the browser window. (Stop halts it after the current job.)</div>';
    return;
  }
  const res = s && s.result;
  if (res && res.message) {
    box.classList.remove("hidden");
    const isHard = res.submitted == null && res.count != null;
    const n = res.submitted != null ? res.submitted : (res.count != null ? res.count : "?");
    const verb = isHard ? "opened to fill" : "applied";
    box.innerHTML = `<div class="fs-headline">Last run: <strong>${n}</strong> ${verb}</div>` +
                    `<div class="fs-sub">${esc(res.message)}</div>`;
  }
}

async function stopLiAuto() {
  const btn = $("btn-li-stop");
  if (btn) btn.disabled = true;
  try {
    const r = await api("/api/linkedin/stop", { method: "POST", body: "{}" });
    setStatus(r.message || "Stop requested.");
  } catch (e) {
    setStatus(`Stop failed: ${e.message}`, true);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function runLiHardApply(btn) {
  if (!confirm(
    "HARD APPLY (assisted).\n\n" +
    "This opens your NON-Easy-Apply LinkedIn jobs, walks into each company's application " +
    "site (Greenhouse / Lever / Ashby / …), and AI-FILLS everything it knows — résumé, " +
    "dropdowns, self-identification, screening answers. It then HOLDS each window open for " +
    "you to review and click Submit yourself. It never submits for you.\n\n" +
    "Make sure you've run `formtool.py lilogin` once. Continue?"
  )) return;
  busy(btn, true);
  setStatus("Opening company-site applications and AI-filling — review + Submit each…");
  try {
    const res = await api("/api/linkedin/hardapply", { method: "POST", body: JSON.stringify({ limit: 12 }) });
    setStatus(res.message || "Hard apply started.");
  } catch (e) {
    setStatus(`Hard apply failed: ${e.message}`, true);
  } finally {
    busy(btn, false);
  }
}

async function runLiAutoApply(btn) {
  if (!confirm(
    "AUTONOMOUS LinkedIn auto-apply.\n\n" +
    "It will SUBMIT applications to India Easy-Apply jobs on your behalf, end-to-end, " +
    "using your profile answer bank. It skips any job it can't answer confidently and " +
    "stops if LinkedIn shows a security check.\n\n" +
    "⚠️ Automated applying can get your LinkedIn account restricted, and submitted " +
    "applications can't be undone. Make sure you ran `formtool.py lilogin`. Continue?"
  )) return;
  busy(btn, true);
  setStatus("Auto-applying on LinkedIn… watch the window.");
  try {
    const res = await api("/api/linkedin/autoapply", { method: "POST", body: "{}" });
    setStatus(res.message || "Auto-apply started.");
  } catch (e) {
    setStatus(`Auto-apply failed: ${e.message}`, true);
  } finally {
    busy(btn, false);
  }
}

// ===== Other platforms: YC / Cutshort / ZipRecruiter ================

const PLATFORM_LABEL = { yc: "Y Combinator", cutshort: "Cutshort", ziprecruiter: "ZipRecruiter",
  wellfound: "Wellfound", instahyre: "Instahyre" };

async function runPlatformApply(platform, query, btn) {
  const label = PLATFORM_LABEL[platform] || platform;
  const nInput = document.querySelector(`[data-n="${platform}"]`);
  const limit = nInput && nInput.value ? parseInt(nInput.value, 10) : null;
  const howMany = limit ? `${limit} job(s)` : "up to the daily cap";
  if (!confirm(
    `AUTONOMOUS auto-apply on ${label} (${howMany}).\n\n` +
    "It will SUBMIT applications on your behalf in your logged-in Chrome window, " +
    "low-volume, and stop on any captcha/security check.\n\n" +
    `⚠️ ${label} prohibits automation in its Terms — this can get the account restricted, ` +
    "and submitted applications can't be undone. Make sure you ran " +
    `\`formtool.py platlogin ${platform}\`. Continue?`
  )) return;
  busy(btn, true);
  setStatus(`Auto-applying on ${label}… watch the window.`);
  const box = document.querySelector(`[data-result="${platform}"]`);
  if (box) { box.classList.remove("hidden"); box.textContent = "Running…"; }
  try {
    const res = await api("/api/platforms/autoapply", {
      method: "POST",
      body: JSON.stringify({ platform, query: query || "", remote: true, limit }),
    });
    setStatus(res.message || "Started.");
  } catch (e) {
    setStatus(`${label} auto-apply failed: ${e.message}`, true);
    if (box) box.textContent = `Error: ${e.message}`;
  } finally {
    busy(btn, false);
  }
}

function renderPlatformRun(run) {
  if (!run || !run.platform) return;
  const box = document.querySelector(`[data-result="${run.platform}"]`);
  if (!box) return;
  box.classList.remove("hidden");
  if (run.running) {
    box.innerHTML = '<span class="aac-spin"></span> Running… watch the Chrome window.';
  } else if (run.result && run.result.message) {
    const r = run.result;
    const cls = r.captcha_stop ? "warn" : (r.submitted ? "ok" : "");
    box.innerHTML = `<span class="aac-dot ${cls}"></span>${r.message}`;
  }
}

async function refreshPlatforms() {
  try {
    const s = await api("/api/platforms/status");
    if (s && s.run) renderPlatformRun(s.run);
  } catch (_) { /* ignore */ }
}

async function runStage(btn, path, busyLabel) {
  busy(btn, true);
  setStatus(busyLabel);
  try {
    const res = await api(path, { method: "POST", body: "{}" });
    setStatus(res.message || "Done.");
    await refresh();
  } catch (e) {
    setStatus(`${btn.querySelector(".step-label").textContent} failed: ${e.message}`, true);
  } finally {
    busy(btn, false);
  }
}

// ===== Review & Send =================================================

function updateApprovedCount() {
  const n = document.querySelectorAll(".draft-approve:checked").length;
  $("btn-send-approved").textContent = `Send approved (${n})`;
  $("btn-send-approved").disabled = n === 0;
}

function renderDrafts(drafts) {
  const list = $("review-list");
  if (!drafts.length) {
    list.innerHTML = '<p class="status-msg">No drafts to review. Run ③ Draft Emails first.</p>';
    $("btn-send-approved").disabled = true;
    return;
  }
  list.innerHTML = drafts
    .map((d) => {
      const vtag = d.verified
        ? '<span class="v-tag ok">verified</span>'
        : '<span class="v-tag no">unverified</span>';
      const checked = d.verified ? "checked" : "";
      return `<div class="draft-card">
        <div class="draft-top">
          <input type="checkbox" class="draft-approve" data-id="${d.id}" data-verified="${d.verified ? 1 : 0}" ${checked} />
          <span class="draft-company">${esc(d.company)}</span>
          <span class="draft-to">→ ${esc(d.to) || "(no email)"}</span>
          ${vtag}
        </div>
        <div class="draft-subject">${esc(d.subject)}</div>
        <div class="draft-body">${esc(d.body)}</div>
      </div>`;
    })
    .join("");
  list.querySelectorAll(".draft-approve").forEach((cb) =>
    cb.addEventListener("change", updateApprovedCount)
  );
  updateApprovedCount();
}

async function openReview(btn) {
  busy(btn, true);
  setStatus("Loading drafts…");
  try {
    const { drafts } = await api("/api/drafts");
    renderDrafts(drafts);
    $("review-panel").classList.remove("hidden");
    $("review-panel").scrollIntoView({ behavior: "smooth", block: "nearest" });
    setStatus(
      drafts.length
        ? "Review the drafts and approve the ones to send."
        : "No drafts yet — run ③ Draft Emails."
    );
  } catch (e) {
    setStatus(`Could not load drafts: ${e.message}`, true);
  } finally {
    busy(btn, false);
  }
}

async function sendApproved() {
  const checkedBoxes = Array.from(document.querySelectorAll(".draft-approve:checked"));
  const ids = checkedBoxes.map((cb) => Number(cb.dataset.id));
  if (!ids.length) return;

  // Operator explicitly checked some unverified recipients — confirm, then allow them.
  const unverified = checkedBoxes.filter((cb) => cb.dataset.verified === "0").length;
  let allowUnverified = false;
  if (unverified > 0) {
    const ok = confirm(
      `${unverified} of ${ids.length} selected recipient(s) are unverified — their address ` +
      `couldn't be confirmed and may bounce. Send to them anyway?`
    );
    if (!ok) return;
    allowUnverified = true;
  }

  const sendBtn = $("btn-send-approved");
  sendBtn.disabled = true;
  setStatus(`Sending ${ids.length} approved email(s)…`);
  try {
    const res = await api("/api/send", {
      method: "POST",
      body: JSON.stringify({ application_ids: ids, allow_unverified: allowUnverified }),
    });
    setStatus(res.message || "Send started.");
    await refresh();
  } catch (e) {
    setStatus(`Send failed: ${e.message}`, true);
  } finally {
    sendBtn.disabled = false;
  }
}

async function resumeSending() {
  try {
    await api("/api/settings", { method: "POST", body: JSON.stringify({ sending_paused: false }) });
    setStatus("Sending resumed.");
    await refresh();
  } catch (e) {
    setStatus(`Could not resume: ${e.message}`, true);
  }
}

// ===== Import from file ==============================================

let pendingFile = null;

function showImportBar(file) {
  pendingFile = file;
  $("import-file").textContent = file.name;
  $("import-bar").classList.remove("hidden");
}

function resetImport() {
  pendingFile = null;
  $("file-input").value = "";
  $("import-bar").classList.add("hidden");
}

async function doImport() {
  if (!pendingFile) return;
  const fd = new FormData();
  fd.append("file", pendingFile);
  const btn = $("btn-import");
  btn.disabled = true;
  setStatus(`Importing ${pendingFile.name}…`);
  try {
    const res = await fetch("/api/import", { method: "POST", body: fd });
    const j = await res.json();
    if (!res.ok) throw new Error(j.detail || res.statusText);
    setStatus(j.message || "Imported.");
    resetImport();
    await refresh();
  } catch (e) {
    setStatus(`Import failed: ${e.message}`, true);
  } finally {
    btn.disabled = false;
  }
}

function wireDropzone() {
  const dz = $("dropzone");
  const fi = $("file-input");
  if (!dz) return;
  dz.addEventListener("click", () => fi.click());
  dz.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fi.click(); }
  });
  fi.addEventListener("change", () => { if (fi.files[0]) showImportBar(fi.files[0]); });
  ["dragover", "dragenter"].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); })
  );
  ["dragleave", "drop"].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); })
  );
  dz.addEventListener("drop", (e) => {
    const f = e.dataTransfer.files[0];
    if (f) showImportBar(f);
  });
  $("btn-import").addEventListener("click", doImport);
  $("btn-import-cancel").addEventListener("click", resetImport);
}

// ===== Wiring ========================================================

function wireButtons() {
  document.querySelectorAll(".pipeline .step").forEach((btn) => {
    btn.addEventListener("click", () => {
      const stage = btn.dataset.stage;
      if (stage === "discover") return runDiscover(btn);
      if (stage === "contacts") return runStage(btn, "/api/contacts", "Finding contacts…");
      if (stage === "draft") return runStage(btn, "/api/draft", "Drafting emails…");
      if (stage === "send") return openReview(btn);
      if (stage === "scan") return runStage(btn, "/api/scan", "Scanning replies…");
    });
  });
  $("btn-send-approved").addEventListener("click", sendApproved);
  $("btn-resume").addEventListener("click", resumeSending);

  const liBtn = $("btn-li-apply");
  if (liBtn) liBtn.addEventListener("click", () => runLiApply(liBtn));
  const liHard = $("btn-li-hard");
  if (liHard) liHard.addEventListener("click", () => runLiHardApply(liHard));
  const liAuto = $("btn-li-auto");
  if (liAuto) liAuto.addEventListener("click", () => runLiAutoApply(liAuto));
  const liStop = $("btn-li-stop");
  if (liStop) liStop.addEventListener("click", stopLiAuto);

  // Other-platform auto-apply buttons (YC / Cutshort / ZipRecruiter)
  document.querySelectorAll(".aac-run").forEach((b) => {
    b.addEventListener("click", () => {
      const platform = b.getAttribute("data-run");
      const input = document.querySelector(`[data-q="${platform}"]`);
      runPlatformApply(platform, input ? input.value.trim() : "", b);
    });
  });

  // In-panel Find button mirrors pipeline step ①; Reset clears the filters.
  const findBtn = $("btn-find");
  if (findBtn) findBtn.addEventListener("click", () => runDiscover(findBtn));
  const resetBtn = $("btn-reset-filters");
  if (resetBtn) resetBtn.addEventListener("click", resetFilters);
}

wireButtons();
wireDropzone();
refresh();
refreshPlatforms();
setInterval(refresh, 5000);
setInterval(refreshPlatforms, 5000);
