const state = { cases: [], selected: null };
const $ = (selector) => document.querySelector(selector);

function reviewerHeaders() {
  const token = $("#reviewer-key").value.trim();
  sessionStorage.setItem("medicare_reviewer_key", token);
  return { "X-Reviewer-Key": token, "Content-Type": "application/json" };
}

function setMessage(text, isError = false) {
  const node = $("#message");
  node.textContent = text;
  node.style.color = isError ? "var(--red)" : "var(--amber)";
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { ...reviewerHeaders(), ...(options.headers || {}) },
  });
  const payload = await response.json();
  if (!response.ok || !payload.success) {
    throw new Error(payload.error?.message || payload.detail || `HTTP ${response.status}`);
  }
  return payload.data;
}

function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = String(value ?? "");
  return div.innerHTML;
}

function concise(value) {
  return String(value || "").replaceAll("_", " ");
}

function formatDate(value) {
  return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(new Date(value));
}

function renderCases() {
  const list = $("#case-list");
  if (!state.cases.length) {
    list.innerHTML = `<div class="empty-state"><span class="empty-icon">✓</span><h2>Queue clear</h2><p>No cases match this status.</p></div>`;
    return;
  }
  list.innerHTML = state.cases.map((item) => `
    <button class="case-row" type="button" data-case-id="${escapeHtml(item.id)}" aria-label="Open ${escapeHtml(item.customer_id)}">
      <span class="identity"><strong>${escapeHtml(item.customer_id)}</strong><span>${escapeHtml(item.id.slice(0, 8))}</span></span>
      <span class="signal"><em class="tier ${escapeHtml(item.rule_tier)}">${escapeHtml(item.rule_tier)}</em><strong>${escapeHtml(concise(item.rule_reasons[0]))}</strong><span>${escapeHtml(concise(item.status))}</span></span>
      <span class="created">${formatDate(item.created_at)}</span><span class="chevron" aria-hidden="true">›</span>
    </button>`).join("");
  list.querySelectorAll("[data-case-id]").forEach((row) => row.addEventListener("click", () => selectCase(row.dataset.caseId)));
}

function closeDrawer() {
  $("#review-drawer").classList.remove("open");
  $("#review-drawer").setAttribute("aria-hidden", "true");
  $("#drawer-backdrop").hidden = true;
  document.body.classList.remove("drawer-open");
  state.selected = null;
}

function openDrawer() {
  $("#drawer-backdrop").hidden = false;
  $("#review-drawer").classList.add("open");
  $("#review-drawer").setAttribute("aria-hidden", "false");
  document.body.classList.add("drawer-open");
  $("#close-drawer").focus();
}

async function loadCases() {
  setMessage("Loading…");
  try {
    const status = $("#status-filter").value;
    const data = await api(`/api/v1/cases?limit=100&status=${encodeURIComponent(status)}`);
    state.cases = data.items;
    $("#case-count").textContent = data.total;
    $("#count-label").textContent = data.total === 1 ? "case" : "cases";
    $("#last-updated").textContent = `Updated ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
    $("#access-controls").classList.add("connected");
    $("#refresh").textContent = "Refresh";
    closeDrawer();
    renderCases();
    setMessage("");
  } catch (error) {
    $("#access-controls").classList.remove("connected");
    $("#refresh").textContent = "Connect";
    setMessage(error.message, true);
  }
}

async function selectCase(caseId) {
  const item = state.cases.find((candidate) => candidate.id === caseId);
  if (!item) return;
  state.selected = item;
  const proposal = item.proposal;
  const list = (values) => values.length
    ? `<ul>${values.map((value) => `<li>${escapeHtml(concise(value))}</li>`).join("")}</ul>`
    : "<p>None</p>";
  $("#drawer-title").textContent = item.customer_id;
  $("#case-detail").innerHTML = `
    <section class="detail-section"><div class="detail-grid"><div><span>Rule tier</span><strong>${escapeHtml(item.rule_tier)}</strong></div><div><span>Case version</span><strong>${item.version}</strong></div></div></section>
    <section class="detail-section"><h3>Deterministic signals</h3>${list(item.rule_reasons)}</section>
    <section class="detail-section"><h3>Policy holds</h3>${list(item.policy_holds)}</section>
    <section class="detail-section"><h3>Agent summary</h3><p>${escapeHtml(proposal?.summary || "No proposal available.")}</p></section>
    <section class="detail-section"><h3>Proposed next step</h3><p>${escapeHtml(proposal?.recommended_action || "None")}</p></section>
    <section class="detail-section"><h3>Audit chain</h3><p id="audit-status">Verifying…</p></section>`;
  $("#review-form").hidden = item.status !== "pending_review";
  openDrawer();
  try {
    const audit = await api(`/api/v1/cases/${item.id}/audit-verification`);
    $("#audit-status").textContent = audit.valid ? "Valid — hash chain intact" : "Invalid — stop processing";
  } catch (error) {
    $("#audit-status").textContent = "Verification unavailable";
    setMessage(error.message, true);
  }
}

$("#review-form").addEventListener("click", (event) => {
  if (event.target.matches("button[data-decision]")) {
    $("#review-form").dataset.decision = event.target.dataset.decision;
  }
});

$("#review-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const decision = event.currentTarget.dataset.decision;
  if (!state.selected || !decision) return;
  try {
    const data = await api(`/api/v1/cases/${state.selected.id}/reviews`, {
      method: "POST",
      body: JSON.stringify({ decision, rationale: $("#rationale").value, expected_version: state.selected.version }),
    });
    setMessage(`Decision recorded · ${concise(data.case.status)}`);
    $("#rationale").value = "";
    await loadCases();
  } catch (error) {
    setMessage(error.message, true);
  }
});

$("#refresh").addEventListener("click", loadCases);
$("#status-filter").addEventListener("change", () => {
  if ($("#access-controls").classList.contains("connected")) loadCases();
});
$("#close-drawer").addEventListener("click", closeDrawer);
$("#drawer-backdrop").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeDrawer(); });
$("#reviewer-key").value = sessionStorage.getItem("medicare_reviewer_key") || "";
