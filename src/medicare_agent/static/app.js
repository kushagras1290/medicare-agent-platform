const PAGE_SIZE = 25;
const REVIEWABLE = new Set(["pending_review", "on_hold"]);
const ESCALATED = "human_escalated";
const DECISIONS_BY_STATUS = {
  pending_review: ["approve", "hold", "reject"],
  on_hold: ["approve", "hold", "reject"],
  human_escalated: ["close"],
};
const STORAGE_KEYS = {
  reviewerKey: "medicare_reviewer_key",
  verifier: "medicare_pkce_verifier",
  oidcState: "medicare_oidc_state",
  accessToken: "medicare_access_token",
  accessExpiry: "medicare_access_expiry",
};
const EVIDENCE_FIELDS = [
  ["age", "Age", ""],
  ["bp_systolic_mm_hg", "Systolic BP", "mmHg"],
  ["bp_diastolic_mm_hg", "Diastolic BP", "mmHg"],
  ["heart_rate_bpm", "Heart rate", "bpm"],
  ["spo2_pct", "SpO₂", "%"],
  ["temperature_c", "Temperature", "°C"],
  ["respiratory_rate_bpm", "Respiratory rate", "/min"],
  ["weight_kg", "Weight", "kg"],
  ["bmi", "BMI", ""],
  ["creatinine_mg_dl", "Creatinine", "mg/dL"],
  ["egfr_ml_min_1_73m2", "eGFR", "mL/min/1.73m²"],
  ["glucose_mg_dl", "Glucose", "mg/dL"],
  ["hemoglobin_g_dl", "Hemoglobin", "g/dL"],
  ["sodium_mmol_l", "Sodium", "mmol/L"],
  ["potassium_mmol_l", "Potassium", "mmol/L"],
  ["phq9_score", "PHQ-9", ""],
];

const config = document.body.dataset;
const isOidc = config.authMode === "oidc";
const state = { cases: [], selected: null, offset: 0, total: 0 };
const $ = (selector) => document.querySelector(selector);

class SessionExpiredError extends Error {}

const store = {
  get(key) {
    try { return sessionStorage.getItem(key); } catch { return null; }
  },
  set(key, value) {
    try { sessionStorage.setItem(key, value); } catch { /* storage unavailable: keep in-memory only */ }
  },
  remove(key) {
    try { sessionStorage.removeItem(key); } catch { /* storage unavailable */ }
  },
};

function base64Url(bytes) {
  let binary = "";
  bytes.forEach((byte) => { binary += String.fromCharCode(byte); });
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

function randomToken(byteLength = 32) {
  return base64Url(crypto.getRandomValues(new Uint8Array(byteLength)));
}

async function pkceChallenge(verifier) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return base64Url(new Uint8Array(digest));
}

function redirectUri() {
  return `${window.location.origin}/`;
}

async function beginLogin() {
  const verifier = randomToken(48);
  const oauthState = randomToken(24);
  store.set(STORAGE_KEYS.verifier, verifier);
  store.set(STORAGE_KEYS.oidcState, oauthState);
  const url = new URL(config.oidcAuthorizationUrl);
  url.search = new URLSearchParams({
    response_type: "code",
    client_id: config.oidcClientId,
    redirect_uri: redirectUri(),
    scope: config.oidcScope,
    state: oauthState,
    code_challenge: await pkceChallenge(verifier),
    code_challenge_method: "S256",
  }).toString();
  window.location.assign(url.toString());
}

async function completeLoginIfRedirected() {
  const params = new URLSearchParams(window.location.search);
  if (!params.has("code") && !params.has("error")) return false;
  window.history.replaceState(null, "", window.location.pathname);
  const expectedState = store.get(STORAGE_KEYS.oidcState);
  const verifier = store.get(STORAGE_KEYS.verifier);
  store.remove(STORAGE_KEYS.oidcState);
  store.remove(STORAGE_KEYS.verifier);
  if (params.has("error")) throw new Error(`Sign-in failed: ${params.get("error")}`);
  if (!expectedState || params.get("state") !== expectedState || !verifier) {
    throw new Error("Sign-in response did not match this browser session");
  }
  const response = await fetch(config.oidcTokenUrl, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "authorization_code",
      code: params.get("code"),
      redirect_uri: redirectUri(),
      client_id: config.oidcClientId,
      code_verifier: verifier,
    }),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok || !payload.access_token) throw new Error("Token exchange failed");
  const lifetimeSeconds = Number(payload.expires_in) || 300;
  store.set(STORAGE_KEYS.accessToken, payload.access_token);
  store.set(STORAGE_KEYS.accessExpiry, String(Date.now() + lifetimeSeconds * 1000));
  return true;
}

function accessToken() {
  const token = store.get(STORAGE_KEYS.accessToken);
  const expiry = Number(store.get(STORAGE_KEYS.accessExpiry));
  if (!token || !expiry || Date.now() >= expiry) return null;
  return token;
}

function signOut() {
  store.remove(STORAGE_KEYS.accessToken);
  store.remove(STORAGE_KEYS.accessExpiry);
  setConnected(false);
}

function authHeaders() {
  if (isOidc) {
    const token = accessToken();
    if (!token) throw new SessionExpiredError("Sign in to load the review queue");
    return { Authorization: `Bearer ${token}` };
  }
  const key = $("#reviewer-key").value.trim();
  store.set(STORAGE_KEYS.reviewerKey, key);
  return { "X-Reviewer-Key": key };
}

function setMessage(text, isError = false) {
  const node = $("#message");
  node.textContent = text;
  node.style.color = isError ? "var(--red)" : "var(--amber)";
}

function setConnected(connected) {
  $("#access-controls").classList.toggle("connected", connected);
  $("#refresh").textContent = connected ? "Refresh" : "Connect";
  if (isOidc) {
    $("#sign-in").hidden = connected;
    $("#sign-out").hidden = !connected;
  }
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { ...authHeaders(), "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const payload = await response.json().catch(() => null);
  if (response.status === 401 && isOidc) {
    signOut();
    throw new SessionExpiredError("Your session expired. Sign in again.");
  }
  if (!response.ok || !payload || !payload.success) {
    throw new Error(payload?.error?.message || `Request failed (HTTP ${response.status})`);
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

function renderPager() {
  const page = Math.floor(state.offset / PAGE_SIZE) + 1;
  const pages = Math.max(1, Math.ceil(state.total / PAGE_SIZE));
  $("#page-label").textContent = `Page ${page} of ${pages}`;
  $("#previous-page").disabled = state.offset === 0;
  $("#next-page").disabled = state.offset + PAGE_SIZE >= state.total;
}

function closeDrawer() {
  // Move focus out before hiding the drawer from assistive technology.
  if ($("#review-drawer").contains(document.activeElement)) $("#refresh").focus();
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

function handleError(error) {
  if (error instanceof SessionExpiredError) setConnected(false);
  setMessage(error.message, true);
}

async function loadCases() {
  setMessage("Loading…");
  try {
    const status = $("#status-filter").value;
    const query = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(state.offset), order: "oldest" });
    if (status) query.set("status", status);
    const data = await api(`/api/v1/cases?${query.toString()}`);
    state.cases = data.items;
    state.total = data.total;
    if (state.offset > 0 && state.offset >= state.total) {
      state.offset = Math.max(0, (Math.ceil(state.total / PAGE_SIZE) - 1) * PAGE_SIZE);
      return loadCases();
    }
    $("#case-count").textContent = data.total;
    $("#count-label").textContent = data.total === 1 ? "case" : "cases";
    $("#last-updated").textContent = `Updated ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
    setConnected(true);
    closeDrawer();
    renderCases();
    renderPager();
    setMessage("");
  } catch (error) {
    if (!(error instanceof SessionExpiredError)) setConnected(false);
    handleError(error);
  }
}

function listMarkup(values) {
  return values.length
    ? `<ul>${values.map((value) => `<li>${escapeHtml(concise(value))}</li>`).join("")}</ul>`
    : "<p>None</p>";
}

function evidenceMarkup(snapshot) {
  if (!snapshot) return "<p>No stored evidence for this case.</p>";
  const rows = EVIDENCE_FIELDS.map(([field, label, unit]) => `
    <div><span>${escapeHtml(label)}</span><strong>${escapeHtml(snapshot[field])}${unit ? ` <small>${escapeHtml(unit)}</small>` : ""}</strong></div>`).join("");
  return `
    <p class="evidence-meta">Measured ${escapeHtml(formatDate(snapshot.measured_at))} · consent ${escapeHtml(concise(snapshot.consent_status))} · ${escapeHtml(snapshot.conditions.map(concise).join(", "))}</p>
    <div class="evidence-grid">${rows}</div>`;
}

function setReviewError(text) {
  const node = $("#review-error");
  node.textContent = text;
  node.hidden = !text;
}

function configureReviewForm(item) {
  setReviewError("");
  $("#rationale").value = "";
  const decisions = DECISIONS_BY_STATUS[item.status] || [];
  const form = $("#review-form");
  form.hidden = decisions.length === 0;
  form.dataset.decision = "";
  form.querySelectorAll("button[data-decision]").forEach((button) => {
    button.hidden = !decisions.includes(button.dataset.decision);
  });
  const canApprove = decisions.includes("approve");
  $("#patient-message-field").hidden = !canApprove;
  $("#patient-message").value = canApprove ? item.proposal?.patient_message || "" : "";
  $("#policy-note").textContent = item.status === ESCALATED
    ? "Escalated cases are handled outside automation. Close the case once offline follow-up is recorded."
    : "Cases with policy holds are escalated even when approved. They cannot enter dispatch.";
}

async function selectCase(caseId) {
  try {
    const item = await api(`/api/v1/cases/${encodeURIComponent(caseId)}`);
    state.selected = item;
    const proposal = item.proposal;
    $("#drawer-title").textContent = item.customer_id;
    $("#case-detail").innerHTML = `
      <section class="detail-section"><div class="detail-grid"><div><span>Rule tier</span><strong>${escapeHtml(item.rule_tier)}</strong></div><div><span>Status</span><strong>${escapeHtml(concise(item.status))}</strong></div><div><span>Case version</span><strong>${escapeHtml(item.version)}</strong></div><div><span>Provider attempts</span><strong>${escapeHtml(item.provider_attempts)}</strong></div></div></section>
      <section class="detail-section"><h3>Evidence the agent saw</h3>${evidenceMarkup(item.snapshot)}</section>
      <section class="detail-section"><h3>Deterministic signals</h3>${listMarkup(item.rule_reasons)}</section>
      <section class="detail-section"><h3>Policy holds</h3>${listMarkup(item.policy_holds)}</section>
      <section class="detail-section"><h3>Agent summary</h3><p>${escapeHtml(proposal?.summary || item.provider_error || "No proposal available.")}</p></section>
      <section class="detail-section"><h3>Staff guidance</h3><p>${escapeHtml(proposal?.recommended_action || "None")}</p></section>
      <section class="detail-section"><h3>Contact channel</h3><p>${escapeHtml(concise(proposal?.contact_channel || "none"))}</p></section>
      <section class="detail-section"><h3>Audit chain</h3><p id="audit-status">Verifying…</p></section>`;
    configureReviewForm(item);
    openDrawer();
    const audit = await api(`/api/v1/cases/${encodeURIComponent(item.id)}/audit-verification`);
    if (!audit.valid) {
      $("#audit-status").textContent = `Invalid (${concise(audit.reason)}) — stop processing`;
    } else {
      $("#audit-status").textContent = audit.sealed
        ? `Valid — ${audit.event_count} sealed events`
        : `Valid — ${audit.event_count} legacy (unsealed) events`;
    }
  } catch (error) {
    const auditStatus = $("#audit-status");
    if (auditStatus) auditStatus.textContent = "Verification unavailable";
    handleError(error);
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
  const selected = state.selected;
  if (!selected || !decision) return;
  const body = { decision, rationale: $("#rationale").value, expected_version: selected.version };
  const editedMessage = $("#patient-message").value.trim();
  if (decision === "approve" && editedMessage && editedMessage !== selected.proposal?.patient_message) {
    body.patient_message = editedMessage;
  }
  try {
    const data = await api(`/api/v1/cases/${encodeURIComponent(selected.id)}/reviews`, {
      method: "POST",
      body: JSON.stringify(body),
    });
    setMessage(`Decision recorded · ${concise(data.case.status)}`);
    $("#rationale").value = "";
    await loadCases();
  } catch (error) {
    // The drawer covers the page-level status line, so show review failures inside it.
    if (error instanceof SessionExpiredError) closeDrawer();
    setReviewError(error.message);
    handleError(error);
  }
});

$("#refresh").addEventListener("click", () => {
  if (isOidc && !accessToken()) {
    beginLogin().catch(handleError);
    return;
  }
  loadCases();
});
$("#status-filter").addEventListener("change", () => {
  state.offset = 0;
  if ($("#access-controls").classList.contains("connected")) loadCases();
});
$("#previous-page").addEventListener("click", () => {
  state.offset = Math.max(0, state.offset - PAGE_SIZE);
  loadCases();
});
$("#next-page").addEventListener("click", () => {
  state.offset += PAGE_SIZE;
  loadCases();
});
$("#close-drawer").addEventListener("click", closeDrawer);
$("#drawer-backdrop").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeDrawer(); });

if (isOidc) {
  $("#sign-in").addEventListener("click", () => beginLogin().catch(handleError));
  $("#sign-out").addEventListener("click", signOut);
  completeLoginIfRedirected()
    .then(() => { if (accessToken()) loadCases(); })
    .catch(handleError);
} else {
  $("#reviewer-key").value = store.get(STORAGE_KEYS.reviewerKey) || "";
}
