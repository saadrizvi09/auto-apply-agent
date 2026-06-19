// AutoApply — referral-digest ingestion + Google-Forms auto-apply review.
// Self-contained module (does not touch app.js).

(function () {
  const $ = (id) => document.getElementById(id);
  const esc = (v) =>
    v == null ? "" : String(v).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

  const STATUS = {
    form_found: { label: "Not filled", cls: "fs-found" },
    form_filled: { label: "Filled · review & submit", cls: "fs-filled" },
    form_error: { label: "Needs attention", cls: "fs-error" },
    form_submitted: { label: "Submitted", cls: "fs-done" },
  };

  let pollTimer = null;

  async function postJSON(url, body) {
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    return r.json();
  }

  function answersTable(json) {
    let rows;
    try {
      rows = JSON.parse(json || "[]");
    } catch {
      return "";
    }
    if (!rows.length) return "";
    const cells = rows
      .map((p) => {
        let val = esc(p.answer) || '<span class="ans-empty">—</span>';
        let tag = "";
        if (p.blocked) tag = '<span class="ans-tag tag-warn">upload manually</span>';
        else if (p.source === "missing")
          tag = `<span class="ans-tag tag-miss">fill profile: ${esc(p.missing_field)}</span>`;
        else if (p.source === "llm") tag = '<span class="ans-tag tag-ai">AI</span>';
        else if (p.source === "option") tag = '<span class="ans-tag tag-opt">choice</span>';
        return `<tr><td class="ans-q">${esc(p.title)}</td><td class="ans-a">${val} ${tag}</td></tr>`;
      })
      .join("");
    return `<table class="ans-table">${cells}</table>`;
  }

  function card(f) {
    // A dry-run "submit" only simulates — never show it as a real submission.
    const simulated = f.status === "form_submitted" && /DRY_RUN/i.test(f.note || "");
    const st = simulated
      ? { label: "Simulated (dry run)", cls: "fs-error" }
      : STATUS[f.status] || { label: f.status, cls: "" };
    const meta = [f.stipend, f.location].filter(Boolean).map(esc).join(" · ");
    const shot = f.screenshot
      ? `<a class="form-shot" href="/form_shots/${esc(f.screenshot)}" target="_blank" rel="noopener">
           <img src="/form_shots/${esc(f.screenshot)}" alt="form preview" loading="lazy" /></a>`
      : "";
    const note = f.note ? `<p class="form-note">${esc(f.note)}</p>` : "";
    const isSubmitted = f.status === "form_submitted" && !simulated;
    const submitBtn = isSubmitted
      ? `<span class="form-done">✓ submitted</span>`
      : `<button class="link-btn btn-autosubmit" data-id="${f.id}" title="Try to submit automatically — won't work if the form requires a CV upload">try auto-submit</button>
         <button class="btn-primary btn-mark-submitted" data-id="${f.id}">✓ Mark as submitted</button>`;
    const simWarn = simulated
      ? `<span class="form-sim">⚠ dry-run only — NOT actually submitted</span>`
      : "";
    const open = f.prefill_url
      ? `<a class="btn-primary apply-prefill" href="${esc(f.prefill_url)}" target="_blank" rel="noopener" title="Opens the form already filled in — just attach your CV and submit">Open pre-filled form ↗</a>`
      : `<a class="link-btn" href="${esc(f.form_url)}" target="_blank" rel="noopener">open blank form ↗</a>`;
    return `<div class="form-card">
      <div class="form-card-top">
        <div>
          <div class="form-company">${esc(f.company)}</div>
          <div class="form-role">${esc(f.role)}${meta ? ` · ${meta}` : ""}</div>
        </div>
        <div class="form-top-right">
          <span class="form-status ${st.cls}">${esc(st.label)}</span>
          <button class="form-x" data-id="${f.id}" title="Archive — hides this card but keeps it in your Applications history. Shift-click to delete permanently.">&times;</button>
        </div>
      </div>
      ${note}
      <div class="form-card-body">
        ${shot}
        <div class="form-answers">${answersTable(f.answers)}</div>
      </div>
      <div class="form-card-foot">${open}${simWarn}<span class="spacer"></span>${submitBtn}</div>
    </div>`;
  }

  async function refreshForms() {
    let data;
    try {
      data = await fetch("/api/forms").then((r) => r.json());
    } catch {
      return;
    }
    const forms = data.forms || [];
    $("forms-block").classList.toggle("hidden", forms.length === 0);
    $("forms-count").textContent = forms.length;
    $("forms-list").innerHTML = forms.map(card).join("");

    const running = data.fill_status && data.fill_status.running;
    $("btn-forms-prefill").disabled = !!running;
    $("btn-forms-prefill").textContent = running ? "Building…" : "Prepare pre-filled links";
    const pending = forms.filter((f) => f.status === "form_found").length;
    $("fill-hint").textContent = running
      ? "reading forms & building links…"
      : pending
      ? `${pending} form(s) need links`
      : "";

    // Keep polling while a fill run is active.
    if (running && !pollTimer) {
      pollTimer = setInterval(refreshForms, 3000);
    } else if (!running && pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }

    // Wire buttons.
    document.querySelectorAll(".btn-autosubmit").forEach((b) => {
      b.addEventListener("click", () => submitForm(b, "/api/forms/submit", "Submitting…"));
    });
    document.querySelectorAll(".btn-mark-submitted").forEach((b) => {
      b.addEventListener("click", () => submitForm(b, "/api/forms/mark-submitted", "Marking…"));
    });
    document.querySelectorAll(".form-x").forEach((b) => {
      b.addEventListener("click", (e) => removeForm(b, e.shiftKey));
    });
  }

  async function removeForm(btn, permanent) {
    const id = Number(btn.dataset.id);
    if (permanent) {
      if (!confirm("Permanently DELETE this? It will be gone for good — no history kept.")) return;
      const res = await postJSON("/api/forms/delete-permanent", { application_id: id });
      setMsg(res.message || "", res.ok ? "ok" : "err");
      return refreshForms();
    }
    if (!confirm("Archive this card? It stays in your Applications history.")) return;
    const res = await postJSON("/api/forms/delete", { application_id: id });
    setMsg(res.message || "", res.ok ? "ok" : "err");
    refreshForms();
  }

  async function submitForm(btn, url, busy) {
    const id = Number(btn.dataset.id);
    btn.disabled = true;
    btn.textContent = busy;
    const res = await postJSON(url, { application_id: id });
    setMsg(res.message || (res.ok ? "Done." : "Failed."), res.ok ? "ok" : "err");
    refreshForms();
  }

  function setMsg(text, kind) {
    const el = $("ref-msg");
    el.textContent = text || "";
    el.className = "ref-msg" + (kind ? ` ref-${kind}` : "");
  }

  async function parseDigest() {
    const text = $("ref-text").value.trim();
    if (!text) {
      setMsg("Paste a referral email first.", "err");
      return;
    }
    setMsg("Parsing…");
    const res = await postJSON("/api/referrals/ingest", { text });
    setMsg(res.message || "Done.", "ok");
    if (res.added) $("ref-text").value = "";
    refreshForms();
  }

  async function scanGmail() {
    setMsg("Scanning Gmail…");
    const res = await postJSON("/api/referrals/scan", {});
    setMsg(res.message || "Done.", res.ok === false ? "err" : "ok");
    refreshForms();
  }

  async function prefillForms() {
    $("btn-forms-prefill").disabled = true;
    $("btn-forms-prefill").textContent = "Building…";
    const res = await postJSON("/api/forms/prefill", {});
    setMsg(res.message || "", "ok");
    if (!pollTimer) pollTimer = setInterval(refreshForms, 3000);
    refreshForms();
  }

  async function refreshProfile() {
    try {
      const p = await fetch("/api/profile").then((r) => r.json());
      const el = $("profile-status");
      if (p.missing && p.missing.length) {
        el.textContent = "profile incomplete — missing: " + p.missing.join(", ");
        el.className = "profile-status prof-warn";
      } else {
        const name = (p.profile && p.profile.full_name) || "you";
        el.textContent = `profile ready (${name})`;
        el.className = "profile-status prof-ok";
      }
    } catch {}
  }

  async function readResume() {
    const btn = $("btn-read-resume");
    btn.disabled = true;
    const old = btn.textContent;
    btn.textContent = "reading cv.pdf…";
    try {
      const res = await postJSON("/api/profile/read-resume", {});
      setMsg(res.message || "Done.", "ok");
    } catch {
      setMsg("Couldn't read résumé.", "err");
    }
    btn.disabled = false;
    btn.textContent = old;
    refreshProfile();
  }

  function init() {
    $("btn-ref-parse").addEventListener("click", parseDigest);
    $("btn-ref-scan").addEventListener("click", scanGmail);
    $("btn-forms-prefill").addEventListener("click", prefillForms);
    $("btn-read-resume").addEventListener("click", readResume);
    refreshProfile();
    refreshForms();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
