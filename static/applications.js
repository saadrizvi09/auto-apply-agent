// AutoApply — applications tracker page.

const $ = (id) => document.getElementById(id);

const STATUS_LABELS = {
  discovered: "Discovered",
  email_found: "Email found",
  drafted: "Drafted",
  approved: "Approved",
  sent: "Awaiting reply",
  replied_interview: "Interview",
  replied_rejection: "Rejection",
  replied_needinfo: "Needs info",
  auto_ack: "Auto-ack",
  bounced: "Bounced",
  no_reply: "No reply",
};

let ALL = [];
let activeFilter = "all";

function esc(v) {
  if (v == null) return "";
  return String(v).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function pill(status) {
  if (!status) return '<span class="cell-muted">—</span>';
  return `<span class="pill pill-${esc(status)}">${esc(STATUS_LABELS[status] || status)}</span>`;
}

function fmt(iso) {
  return iso ? iso.replace("T", " ").slice(0, 16) : "—";
}

// Opens the conversation in Gmail (searches All Mail by thread id).
const GMAIL_ICON =
  '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/></svg>';

function gmailLink(threadId) {
  if (!threadId) return '<span class="cell-muted">—</span>';
  const url = "https://mail.google.com/mail/u/0/#all/" + encodeURIComponent(threadId);
  return `<a class="gmail-link" href="${url}" target="_blank" rel="noopener" title="Open thread in Gmail">${GMAIL_ICON}</a>`;
}

function renderSummary(s) {
  $("s-total").textContent = s.total;
  $("s-applied").textContent = s.applied;
  $("s-awaiting").textContent = s.awaiting;
  $("s-interview").textContent = s.interview;
  $("s-needinfo").textContent = s.needinfo;
  $("s-rejection").textContent = s.rejection;
}

function renderInterviews(apps) {
  const interviews = apps.filter((a) => a.status === "replied_interview");
  $("interview-section").classList.toggle("hidden", interviews.length === 0);
  $("interview-count").textContent = interviews.length;
  $("interview-cards").innerHTML = interviews
    .map(
      (a) => `<div class="interview-card">
        <div class="ic-top">
          <span class="ic-company">${esc(a.company)}</span>
          <span class="pill pill-replied_interview">Interview</span>
        </div>
        <div class="ic-role">${esc(a.role) || ""}${a.salary ? ` · <span class="ic-salary">${esc(a.salary)}</span>` : ""}</div>
        <div class="ic-reply">${esc(a.reply_excerpt) || ""}</div>
        <div class="ic-foot">
          <span class="ic-meta">${esc(a.email) || ""} · ${fmt(a.last_checked_at)}</span>
          ${a.thread_id ? `<a class="gmail-link-text" href="https://mail.google.com/mail/u/0/#all/${encodeURIComponent(a.thread_id)}" target="_blank" rel="noopener">Open in Gmail ${GMAIL_ICON}</a>` : ""}
        </div>
      </div>`
    )
    .join("");
}

function contactCell(a) {
  if (a.email) {
    const v = a.verified
      ? '<span class="v-dot ok" title="verified"></span>'
      : '<span class="v-dot no" title="unverified"></span>';
    return `${v}${esc(a.email)}`;
  }
  if (a.apply_url) {
    return `<a class="apply-link" href="${esc(a.apply_url)}" target="_blank" rel="noopener" title="Open application page">Apply ${GMAIL_ICON}</a>`;
  }
  return '<span class="cell-muted">—</span>';
}

function renderTable() {
  const rows = activeFilter === "all" ? ALL : ALL.filter((a) => a.status === activeFilter);
  $("row-count").textContent = rows.length;
  const body = $("track-body");
  if (rows.length === 0) {
    body.innerHTML = '<tr><td colspan="9" class="empty">No applications in this view.</td></tr>';
    return;
  }
  body.innerHTML = rows
    .map(
      (a) => `<tr>
        <td class="cell-strong">${esc(a.company)}</td>
        <td>${esc(a.role)}</td>
        <td class="cell-salary">${a.salary ? esc(a.salary) : '<span class="cell-muted">—</span>'}</td>
        <td class="cell-contact">${contactCell(a)}</td>
        <td>${pill(a.status)}</td>
        <td class="cell-muted">${esc(fmt(a.sent_at))}</td>
        <td class="cell-muted">${esc(fmt(a.last_checked_at))}</td>
        <td class="cell-snippet">${esc(a.reply_excerpt) || '<span class="cell-muted">—</span>'}</td>
        <td class="cell-thread">${gmailLink(a.thread_id)}</td>
      </tr>`
    )
    .join("");
}

async function refresh() {
  try {
    const [data, state] = await Promise.all([
      fetch("/api/applications").then((r) => r.json()),
      fetch("/api/state").then((r) => r.json()),
    ]);
    ALL = data.applications || [];
    renderSummary(data.summary || {});
    renderInterviews(ALL);
    renderTable();
    $("dry-badge").classList.toggle("hidden", !state.dry_run);
  } catch (e) {
    /* keep last view on transient errors */
  }
}

function wireChips() {
  document.querySelectorAll(".chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      document.querySelectorAll(".chip").forEach((c) => c.classList.remove("active"));
      chip.classList.add("active");
      activeFilter = chip.dataset.filter;
      renderTable();
    });
  });
}

wireChips();
refresh();
setInterval(refresh, 10000);
