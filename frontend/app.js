/* Mass Mailer frontend – plain JavaScript, no build step. */
"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  csrf: null,
  config: null,
  smtp: null,
  attachments: [],
  stats: null,
  jobId: null,
  pollTimer: null,
  templates: [],
  profiles: [],
  registering: false,
  parseTimer: null,
};

/* ------------------------------------------------------------ helpers */

async function api(path, { method = "GET", body, form } = {}) {
  const headers = {};
  if (method !== "GET") headers["X-CSRF-Token"] = state.csrf || "";
  let payload;
  if (form) payload = form;
  else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  const res = await fetch(path, { method, headers, body: payload, credentials: "same-origin" });
  let data = null;
  const type = res.headers.get("content-type") || "";
  if (type.includes("application/json")) data = await res.json();
  if (res.status === 401 && !path.startsWith("/api/auth/")) {
    showLogin();
    throw new Error("Please sign in.");
  }
  if (!res.ok) {
    let detail = data && data.detail;
    if (Array.isArray(detail)) detail = detail.map((d) => d.msg).join("; ");
    throw new Error(detail || `Request failed (${res.status})`);
  }
  return data;
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === false || v == null) continue;  // boolean attributes such as disabled
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of children) node.append(c);
  return node;
}

function showMsg(id, text, kind = "info") {
  const node = $(id);
  if (!text) { node.hidden = true; return; }
  node.textContent = text;
  node.className = `msg ${kind}`;
  node.hidden = false;
}

const fmt = (n) => Number(n || 0).toLocaleString();
function fmtSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
}
function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString();
}

async function withBusy(button, fn) {
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "Please wait…";
  try { return await fn(); } finally { button.disabled = false; button.textContent = label; }
}

function closeOnButtons(dialog) {
  dialog.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", () => dialog.close()));
}

/* ------------------------------------------------------------ auth */

function showLogin() {
  stopPolling();
  $("app-view").hidden = true;
  $("login-view").hidden = false;
  $("login-email").focus();
}

function setAuthMode(register) {
  state.registering = register;
  $("register-extra").hidden = !register;
  $("login-password2").required = register;
  $("login-password").autocomplete = register ? "new-password" : "current-password";
  $("login-intro").textContent = register ? "Create your account." : "Sign in to continue.";
  $("login-submit").textContent = register ? "Create account" : "Sign in";
  $("auth-switch").firstChild.textContent = register ? "Already have an account? " : "No account yet? ";
  $("auth-toggle").textContent = register ? "Sign in" : "Create an account";
  showMsg("login-error", "");
}

async function login(ev) {
  ev.preventDefault();
  showMsg("login-error", "");
  const email = $("login-email").value.trim();
  const password = $("login-password").value;
  try {
    let data;
    if (state.registering) {
      if (password !== $("login-password2").value) throw new Error("The passwords do not match.");
      data = await api("/api/auth/register", { method: "POST", body: { email, password } });
      if (data.pending) {
        setAuthMode(false);
        showMsg("login-error", data.message, "ok");
        return;
      }
    } else {
      data = await api("/api/auth/login", { method: "POST", body: { email, password } });
    }
    state.csrf = data.csrf;
    $("login-password").value = "";
    $("login-password2").value = "";
    await startApp();
  } catch (err) {
    showMsg("login-error", err.message, "error");
  }
}

async function logout() {
  try { await api("/api/auth/logout", { method: "POST" }); } catch (_) { /* ignore */ }
  location.reload();  // clears everything the previous user had on screen
}

async function onChangePassword(ev) {
  ev.preventDefault();
  if ($("pw-new").value !== $("pw-new2").value) { showMsg("pw-msg", "The new passwords do not match.", "error"); return; }
  try {
    await api("/api/auth/password", { method: "POST",
      body: { current_password: $("pw-current").value, new_password: $("pw-new").value } });
    $("password-form").reset();
    showMsg("pw-msg", "Password changed.", "ok");
  } catch (err) {
    showMsg("pw-msg", err.message, "error");
  }
}

/* ------------------------------------------------------------ admin: users */

async function loadUsers() {
  if (!state.config || state.config.user.role !== "admin") return;
  const users = await api("/api/admin/users");
  const me = state.config.user.id;
  $("users-rows").replaceChildren(...users.map((u) => {
    const actions = el("div", { class: "row-actions" });
    if (u.id !== me && !u.main_admin) {
      if (u.status === "pending") actions.append(userButton("Approve", u, "active"));
      if (u.status === "active") actions.append(userButton("Disable", u, "disabled"));
      if (u.status === "disabled") actions.append(userButton("Enable", u, "active"));
      actions.append(el("button", { class: "btn link danger-link", type: "button", text: "Delete", onclick: () => deleteUser(u) }));
    } else {
      actions.append(el("span", { class: "muted", text: u.id === me ? "you" : "main admin" }));
    }
    return el("tr", {},
      el("td", { class: "wrap", text: u.email }),
      el("td", { text: u.role }),
      el("td", {}, el("span", { class: `pill ${u.status}`, text: u.status })),
      el("td", { text: fmtTime(u.created_at) }),
      el("td", { text: u.last_login ? fmtTime(u.last_login) : "never" }),
      el("td", { text: fmt(u.sends) }),
      el("td", { text: fmt(u.emails_sent) }),
      el("td", {}, actions),
    );
  }));
}

function userButton(label, user, status) {
  return el("button", { class: "btn link", type: "button", text: label, onclick: async () => {
    try {
      await api(`/api/admin/users/${user.id}/status`, { method: "POST", body: { status } });
      showMsg("users-msg", `${user.email}: ${status === "active" ? "enabled" : status}.`, "ok");
      await loadUsers();
    } catch (err) {
      showMsg("users-msg", err.message, "error");
    }
  } });
}

async function deleteUser(user) {
  if (!confirm(`Delete ${user.email}?\n\nThis permanently removes the account and ALL of its data: SMTP settings, ` +
               "templates, attachments, send history and do-not-send list.")) return;
  try {
    await api(`/api/admin/users/${user.id}`, { method: "DELETE" });
    showMsg("users-msg", `${user.email} was deleted.`, "ok");
    await loadUsers();
  } catch (err) {
    showMsg("users-msg", err.message, "error");
  }
}

/* ------------------------------------------------------------ SMTP */

function fillSmtp(s) {
  state.smtp = s;
  $("smtp-host").value = s.host;
  $("smtp-port").value = s.port;
  $("smtp-security").value = s.security;
  $("smtp-username").value = s.username;
  $("smtp-password").value = "";
  $("smtp-password").placeholder = s.password_set ? "•••••••• (saved)" : "";
  $("password-hint").textContent = s.password_set ? "Saved. Leave empty to keep it." : "";
  $("from-name").value = s.from_name;
  $("from-email").value = s.from_email;
  $("reply-to").value = s.reply_to;
  $("to-address").value = s.to_address;
  $("unsub-email").value = s.unsubscribe_email;
  $("unsub-url").value = s.unsubscribe_url;
  $("company-address").value = s.company_address;
  $("add-footer").checked = s.add_footer;
  const pill = $("smtp-status");
  pill.textContent = s.verified ? "✓ Connection verified" : (s.host ? "Not tested" : "");
  pill.className = "status-pill " + (s.verified ? "ok" : "");
  updateSenderLine();
}

function updateSenderLine() {
  const from = $("from-email").value.trim();
  const name = $("from-name").value.trim();
  $("sum-from").textContent = from ? (name ? `${name} <${from}>` : from) : "not set";
  $("sum-reply").textContent = $("reply-to").value.trim() || from || "not set";
}

async function saveSmtp() {
  const body = {
    host: $("smtp-host").value.trim(),
    port: Number($("smtp-port").value) || 587,
    security: $("smtp-security").value,
    username: $("smtp-username").value.trim(),
    from_name: $("from-name").value.trim(),
    from_email: $("from-email").value.trim(),
    reply_to: $("reply-to").value.trim(),
    to_address: $("to-address").value.trim(),
    unsubscribe_email: $("unsub-email").value.trim(),
    unsubscribe_url: $("unsub-url").value.trim(),
    company_address: $("company-address").value.trim(),
    add_footer: $("add-footer").checked,
  };
  const pw = $("smtp-password").value;
  if (pw) body.password = pw;
  const saved = await api("/api/smtp", { method: "PUT", body });
  fillSmtp(saved);
  return saved;
}

async function onSaveSmtp(ev) {
  ev.preventDefault();
  $("smtp-steps").hidden = true;
  try {
    await saveSmtp();
    showMsg("smtp-msg", "Settings saved.", "ok");
  } catch (err) {
    showMsg("smtp-msg", err.message, "error");
  }
}

async function onTestSmtp() {
  showMsg("smtp-msg", "");
  const list = $("smtp-steps");
  list.hidden = true;
  await withBusy($("smtp-test-btn"), async () => {
    try {
      await saveSmtp();
      const res = await api("/api/smtp/test", { method: "POST" });
      list.replaceChildren(...res.steps.map((s) => {
        const icon = s.ok === true ? "✓" : s.ok === false ? "✗" : "–";
        const cls = s.ok === true ? "ok" : s.ok === false ? "bad" : "skip";
        return el("li", { class: cls, text: `${icon} ${s.name}: ${s.message}` });
      }));
      list.hidden = false;
      showMsg("smtp-msg", res.ok ? "✓ Connection successful" : `✗ ${res.message}`, res.ok ? "ok" : "error");
      fillSmtp(res.settings);
    } catch (err) {
      showMsg("smtp-msg", err.message, "error");
    }
  });
}

/* Saved SMTP accounts: store several sets of credentials and switch between them. */
async function loadProfiles(selectId) {
  state.profiles = await api("/api/smtp/profiles");
  const select = $("profile-select");
  const current = String(selectId ?? select.value);
  select.replaceChildren(el("option", { value: "", text: state.profiles.length ? "— Saved SMTP accounts —" : "— No saved accounts yet —" }),
    ...state.profiles.map((p) => el("option", { value: String(p.id),
      text: p.readable ? `${p.name}  (${p.username || p.from_email} @ ${p.host})` : `${p.name}  (re-enter settings)` })));
  select.value = state.profiles.some((p) => String(p.id) === current) ? current : "";
}

function selectedProfile() {
  const p = state.profiles.find((x) => String(x.id) === $("profile-select").value);
  if (!p) showMsg("prof-msg", "Choose a saved account first.", "error");
  return p;
}

async function onProfUse() {
  const p = selectedProfile();
  if (!p) return;
  try {
    fillSmtp(await api(`/api/smtp/profiles/${p.id}/use`, { method: "POST" }));
    $("smtp-steps").hidden = true;
    showMsg("smtp-msg", "");
    showMsg("prof-msg", `Now using "${p.name}". Sends already running keep their own account.`, "ok");
  } catch (err) {
    showMsg("prof-msg", err.message, "error");
  }
}

async function onProfSave() {
  if (!$("smtp-host").value.trim()) { showMsg("prof-msg", "Enter the SMTP settings first.", "error"); return; }
  const suggested = $("smtp-username").value.trim() || $("from-email").value.trim();
  const name = (prompt("Name for this SMTP account:", suggested) || "").trim();
  if (!name) return;
  const existing = state.profiles.find((p) => p.name.toLowerCase() === name.toLowerCase());
  if (existing && !confirm(`A saved account called "${existing.name}" exists. Replace it?`)) return;
  try {
    await saveSmtp();  // store what is in the form, including a newly typed password
    const res = await api("/api/smtp/profiles", { method: "POST", body: { name: existing ? existing.name : name } });
    await loadProfiles(res.id);
    showMsg("prof-msg", `Saved "${res.name}" (including the password, encrypted).`, "ok");
  } catch (err) {
    showMsg("prof-msg", err.message, "error");
  }
}

async function onProfDelete() {
  const p = selectedProfile();
  if (!p || !confirm(`Delete the saved account "${p.name}"? The current settings in the form stay as they are.`)) return;
  try {
    await api(`/api/smtp/profiles/${p.id}`, { method: "DELETE" });
    await loadProfiles("");
    showMsg("prof-msg", `Deleted "${p.name}".`, "ok");
  } catch (err) {
    showMsg("prof-msg", err.message, "error");
  }
}

async function onCheckDeliverability() {
  const list = $("deliv-list");
  list.hidden = true;
  await withBusy($("deliv-btn"), async () => {
    try {
      await saveSmtp();
      const res = await api("/api/smtp/deliverability", { method: "POST" });
      const icons = { ok: "✓", warn: "!", error: "✗", info: "i" };
      list.replaceChildren(...res.items.map((i) =>
        el("li", { class: i.status }, el("strong", { text: `${icons[i.status]}  ${i.name}` }), i.message)));
      list.hidden = false;
    } catch (err) {
      showMsg("smtp-msg", err.message, "error");
    }
  });
}

/* ------------------------------------------------------------ recipients */

function scheduleParse() {
  clearTimeout(state.parseTimer);
  state.parseTimer = setTimeout(parseRecipients, 350);
}

async function parseRecipients() {
  try {
    const res = await api("/api/recipients/parse", { method: "POST", body: { text: $("recipients").value } });
    renderStats(res);
  } catch (err) {
    showMsg("import-msg", err.message, "error");
  }
}

function renderStats(res) {
  state.stats = res;
  const s = res.stats;
  $("st-total").textContent = fmt(s.total);
  $("st-valid").textContent = fmt(s.valid);
  $("st-invalid").textContent = fmt(s.invalid);
  $("st-dupes").textContent = fmt(s.duplicates);
  $("st-suppressed").textContent = fmt(s.suppressed);
  $("suppressed-box").hidden = res.suppressed.length === 0;
  $("suppressed-list").replaceChildren(...res.suppressed.map((x) => el("li", { text: x })));
  $("st-ready").textContent = fmt(s.ready);
  $("invalid-box").hidden = res.invalid.length === 0;
  $("invalid-list").replaceChildren(...res.invalid.map((x) => el("li", { text: x })));
  if (res.over_limit) showMsg("limit-msg", `Too many recipients: the limit is ${fmt(res.max_recipients)}.`, "error");
  else showMsg("limit-msg", "");
  updateBatchInfo();
}

async function onImportCsv(ev) {
  const file = ev.target.files[0];
  ev.target.value = "";
  if (!file) return;
  const form = new FormData();
  form.append("file", file);
  try {
    const res = await api("/api/recipients/import", { method: "POST", form });
    const current = $("recipients").value.trim();
    $("recipients").value = current ? `${current}\n${res.text}` : res.text;
    const s = res.stats;
    showMsg("import-msg",
      `Imported ${file.name}: ${fmt(s.total)} rows, ${fmt(s.valid)} valid, ${fmt(s.invalid)} invalid, ` +
      `${fmt(s.duplicates)} duplicates removed${s.named ? `, ${fmt(s.named)} with names` : ""}.`, "ok");
    await parseRecipients();
  } catch (err) {
    showMsg("import-msg", err.message, "error");
  }
}

async function onCleanRecipients() {
  await parseRecipients();
  if (!state.stats) return;
  const st = state.stats.stats;
  $("recipients").value = state.stats.clean_text;
  showMsg("import-msg", `Removed ${fmt(st.invalid)} invalid, ${fmt(st.duplicates)} duplicate and ` +
    `${fmt(st.suppressed)} do-not-send entries.`, "ok");
  await parseRecipients();
}

/* ------------------------------------------------------------ do-not-send list */

async function loadSuppression() {
  const rows = await api("/api/suppression");
  $("suppress-count").textContent = fmt(rows.length);
  $("suppress-rows").replaceChildren(...(rows.length ? rows.map((r) => el("tr", {},
    el("td", { class: "wrap", text: r.email }),
    el("td", { text: r.reason }),
    el("td", { text: fmtTime(r.created_at) }),
    el("td", {}, el("button", { class: "btn link", type: "button", text: "Remove", onclick: () => unsuppress(r.email) })),
  )) : [el("tr", {}, el("td", { colspan: "4", text: "The list is empty." }))]));
  return rows;
}

async function onSuppressAdd() {
  try {
    const res = await api("/api/suppression", { method: "POST", body: { text: $("suppress-input").value, reason: "unsubscribed" } });
    $("suppress-input").value = "";
    $("suppress-msg").textContent = `Added ${fmt(res.added)}.` + (res.invalid.length ? ` Ignored invalid: ${res.invalid.join(", ")}` : "");
    await loadSuppression();
    await parseRecipients();
  } catch (err) {
    $("suppress-msg").textContent = err.message;
  }
}

async function unsuppress(email) {
  await api("/api/suppression/remove", { method: "POST", body: { text: email } });
  await loadSuppression();
  await parseRecipients();
}

/* ------------------------------------------------------------ templates */

async function loadTemplates(selectId) {
  state.templates = await api("/api/templates");
  const select = $("template-select");
  const current = selectId ?? select.value;
  select.replaceChildren(el("option", { value: "", text: state.templates.length ? "— Saved templates —" : "— No templates yet —" }),
    ...state.templates.map((t) => el("option", { value: t.id, text: t.attachments.length ? `${t.name}  (📎 ${t.attachments.length})` : t.name })));
  select.value = state.templates.some((t) => t.id === current) ? current : "";
}

function selectedTemplate() {
  const t = state.templates.find((x) => x.id === $("template-select").value);
  if (!t) showMsg("tpl-msg", "Choose a template first.", "error");
  return t;
}

async function onTplLoad() {
  const t = selectedTemplate();
  if (!t) return;
  if (($("subject").value.trim() || $("body").value.trim()) &&
      !confirm(`Replace the current subject, message and attachments with "${t.name}"?`)) return;
  try {
    for (const a of [...state.attachments]) await removeAttachment(a.id);
    const res = await api(`/api/templates/${t.id}/use`, { method: "POST" });
    const c = res.compose;
    $("subject").value = c.subject || "";
    $("body").value = c.body || "";
    document.querySelector(`input[name="format"][value="${c.format === "text" ? "text" : "html"}"]`).checked = true;
    $("personalize").checked = !!c.personalize;
    $("personalize-opts").hidden = !c.personalize;
    $("name-fallback").value = c.name_fallback || "there";
    onFormatChange();
    state.attachments = res.attachments;
    renderAttachments();
    showMsg("tpl-msg", `Loaded "${res.name}".`, "ok");
  } catch (err) {
    showMsg("tpl-msg", err.message, "error");
  }
}

async function saveTemplate(name, templateId) {
  const res = await api("/api/templates", {
    method: "POST",
    body: { name, compose: composeData(), attachment_ids: attachmentIds(), template_id: templateId || null },
  });
  await loadTemplates(res.id);
  showMsg("tpl-msg", `Saved template "${res.name}" (subject, message${state.attachments.length ? " and attachments" : ""}).`, "ok");
}

async function onTplSave() {
  if (!$("subject").value.trim() && !$("body").value.trim()) { showMsg("tpl-msg", "Write a subject or message first.", "error"); return; }
  const name = (prompt("Template name:", $("subject").value.trim()) || "").trim();
  if (!name) return;
  const existing = state.templates.find((t) => t.name.toLowerCase() === name.toLowerCase());
  if (existing && !confirm(`A template called "${existing.name}" exists. Replace it?`)) return;
  try { await saveTemplate(name, existing && existing.id); } catch (err) { showMsg("tpl-msg", err.message, "error"); }
}

async function onTplUpdate() {
  const t = selectedTemplate();
  if (!t || !confirm(`Overwrite "${t.name}" with the current subject, message and attachments?`)) return;
  try { await saveTemplate(t.name, t.id); } catch (err) { showMsg("tpl-msg", err.message, "error"); }
}

async function onTplDelete() {
  const t = selectedTemplate();
  if (!t || !confirm(`Delete the template "${t.name}"?`)) return;
  try {
    await api(`/api/templates/${t.id}`, { method: "DELETE" });
    await loadTemplates("");
    showMsg("tpl-msg", `Deleted "${t.name}".`, "ok");
  } catch (err) {
    showMsg("tpl-msg", err.message, "error");
  }
}

/* ------------------------------------------------------------ compose */

function composeData() {
  return {
    subject: $("subject").value,
    body: $("body").value,
    format: document.querySelector('input[name="format"]:checked').value,
    personalize: $("personalize").checked,
    name_fallback: $("name-fallback").value,
  };
}

function onFormatChange() {
  $("toolbar").hidden = composeData().format !== "html";
}

function insertAround(before, after = "") {
  const ta = $("body");
  const { selectionStart: s, selectionEnd: e, value } = ta;
  const selected = value.slice(s, e);
  ta.setRangeText(before + selected + after, s, e, "end");
  if (!selected) ta.setSelectionRange(s + before.length, s + before.length);
  ta.focus();
}

function onToolbar(ev) {
  const btn = ev.target.closest("button");
  if (!btn) return;
  const tag = btn.dataset.wrap;
  if (tag) return insertAround(`<${tag}>`, `</${tag}>`);
  if (btn.dataset.action === "br") return insertAround("<br>\n");
  if (btn.dataset.action === "list") return insertAround("<ul>\n  <li>", "</li>\n  <li></li>\n</ul>");
  if (btn.dataset.action === "link") {
    const url = prompt("Link address (https://…)", "https://");
    if (!url || !/^(https?:\/\/|mailto:)/i.test(url)) return;
    const safe = url.replace(/"/g, "%22");
    insertAround(`<a href="${safe}">`, "</a>");
  }
}

async function onPreview() {
  try {
    const res = await api("/api/preview", {
      method: "POST",
      body: { compose: composeData(), recipients_text: $("recipients").value, attachment_ids: attachmentIds() },
    });
    $("preview-meta").textContent = `Subject: ${res.subject}   ·   Size: ${res.size}` +
      (res.sample_name ? `   ·   Personalized for: ${res.sample_name}` : "");
    const frame = $("preview-frame");
    const text = $("preview-text");
    if (res.html) {
      frame.hidden = false; text.hidden = true;
      frame.srcdoc = res.html;
    } else {
      frame.hidden = true; text.hidden = false;
      text.textContent = res.text;
    }
    $("preview-dialog").showModal();
  } catch (err) {
    showMsg("send-msg", err.message, "error");
  }
}

/* ------------------------------------------------------------ attachments */

const attachmentIds = () => state.attachments.map((a) => a.id);

function renderAttachments() {
  $("attach-list").replaceChildren(...state.attachments.map((a) =>
    el("li", {},
      el("span", { class: "name", text: a.filename, title: a.filename }),
      el("span", { class: "size", text: fmtSize(a.size) }),
      el("button", { class: "btn link", type: "button", text: "Remove", onclick: () => removeAttachment(a.id) }),
    )));
  $("attach-total").textContent = fmtSize(state.attachments.reduce((t, a) => t + a.size, 0));
}

async function loadAttachments() {
  state.attachments = await api("/api/attachments");
  renderAttachments();
}

async function onAttach(ev) {
  const files = [...ev.target.files];
  ev.target.value = "";
  showMsg("attach-msg", "");
  const errors = [];
  for (const file of files) {
    const maxBytes = state.config.max_attachment_mb * 1024 * 1024;
    if (file.size > maxBytes) { errors.push(`${file.name}: larger than ${state.config.max_attachment_mb} MB`); continue; }
    const form = new FormData();
    form.append("file", file);
    try {
      state.attachments.push(await api("/api/attachments", { method: "POST", form }));
    } catch (err) {
      errors.push(`${file.name}: ${err.message}`);
    }
  }
  renderAttachments();
  if (errors.length) showMsg("attach-msg", errors.join(" · "), "error");
}

async function removeAttachment(id) {
  try {
    await api(`/api/attachments/${encodeURIComponent(id)}`, { method: "DELETE" });
    state.attachments = state.attachments.filter((a) => a.id !== id);
    renderAttachments();
  } catch (err) {
    showMsg("attach-msg", err.message, "error");
  }
}

/* ------------------------------------------------------------ sending */

function sendPayload(confirm = false) {
  return {
    compose: composeData(),
    recipients_text: $("recipients").value,
    batch_size: Number($("batch-size").value) || state.config.default_batch_size,
    speed: $("speed").value,
    conservative_delay: $("speed").value === "conservative" ? Number($("conservative-delay").value) : null,
    delivery: $("delivery").value,
    attachment_ids: attachmentIds(),
    confirm,
  };
}

/* Remember the chosen sending mode and pause in this browser, so a reload doesn't reset them. */
const PACE_KEY = "mass-mailer:pace";

function savePace() {
  try { localStorage.setItem(PACE_KEY, JSON.stringify({ speed: $("speed").value, delay: $("conservative-delay").value })); } catch (_) { /* storage unavailable */ }
}

function restorePace() {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(PACE_KEY) || "null"); } catch (_) { /* storage unavailable */ }
  if (!saved) return;
  const pick = (select, value) => { if ([...select.options].some((o) => o.value === value)) select.value = value; };
  pick($("speed"), saved.speed);
  pick($("conservative-delay"), saved.delay);
}

function fmtDuration(seconds) {
  if (seconds < 1) return `${seconds.toFixed(1)} s`;
  if (seconds < 60) return `${Math.round(seconds)} s`;
  const m = Math.round(seconds / 60);
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60} min`;
}

function updateBatchInfo() {
  if (!state.config) return;
  const size = Math.max(1, Number($("batch-size").value) || 1);
  const ready = state.stats ? state.stats.stats.ready : 0;
  const batches = Math.ceil(ready / size);
  $("batch-info").textContent = ready
    ? `${fmt(ready)} recipients → ${fmt(batches)} batch${batches === 1 ? "" : "es"} (max ${state.config.max_batch_size})`
    : `Max ${state.config.max_batch_size} per message.`;
  const conservative = $("speed").value === "conservative";
  $("conservative-delay-field").hidden = !conservative;
  const delay = conservative
    ? Number($("conservative-delay").value)
    : state.config.speeds[$("speed").value];
  const batchesLeft = Math.max(0, Math.ceil(ready / size) - 1);
  $("speed-info").textContent = `${fmtDuration(delay)} pause between batches`
    + (batchesLeft ? ` (≈ ${fmtDuration(batchesLeft * delay)} of pauses in total)` : "");
  const individual = $("delivery").value === "individual" || $("personalize").checked;
  const per = state.config.message_delays[$("speed").value];
  $("delivery-info").textContent = individual
    ? `${fmt(ready)} separate messages, ${per} s apart – each addressed only to its recipient.`
    : "One message per batch; recipients are hidden with BCC.";
}

async function onSendTest() {
  showMsg("test-msg", "");
  await withBusy($("test-btn"), async () => {
    try {
      const res = await api("/api/send/test", {
        method: "POST",
        body: { compose: composeData(), attachment_ids: attachmentIds(), test_email: $("test-email").value, delivery: $("delivery").value },
      });
      showMsg("test-msg", res.ok ? `✓ ${res.message}` : `✗ ${res.message}`, res.ok ? "ok" : "error");
      if (res.ok) loadMailbox();
    } catch (err) {
      showMsg("test-msg", err.message, "error");
    }
  });
}

async function onSendClick() {
  showMsg("send-msg", "");
  await withBusy($("send-btn"), async () => {
    try {
      const s = await api("/api/send/prepare", { method: "POST", body: sendPayload() });
      openConfirm(s);
    } catch (err) {
      showMsg("send-msg", err.message, "error");
    }
  });
}

function openConfirm(s) {
  const rows = [
    ["Recipients", fmt(s.recipients)],
    ["BCC batches", `${fmt(s.batches)} (up to ${s.batch_size} per batch)`],
    ["Delivery", s.delivery === "individual" ? "Individual messages" : "BCC batches"],
    ["Messages", s.messages !== s.batches ? `${fmt(s.messages)} (personalized, one per recipient)` : fmt(s.messages)],
    ["From", s.from],
    ["Reply-To", s.reply_to],
    ["Visible To", s.visible_to],
    ["Subject", s.subject],
    ["Attachments", s.attachments.length ? s.attachments.join(", ") : "none"],
    ["Email size", s.size],
    ["Sending mode", `${s.speed} (${fmtDuration(s.delay_seconds)} between batches)`],
    ["Mode", s.email_mode === "development" ? "DEVELOPMENT – goes to test mailbox only" : "PRODUCTION – real emails"],
  ];
  $("confirm-summary").replaceChildren(...rows.flatMap(([k, v]) => [el("dt", { text: k }), el("dd", { text: v })]));
  const notes = [
    ...s.errors.map((e) => el("li", { class: "error", text: e })),
    ...s.warnings.map((w) => el("li", { text: w })),
  ];
  $("confirm-warnings").replaceChildren(...notes);
  $("confirm-send").disabled = !s.ok;
  $("confirm-dialog").querySelector("h2").textContent = s.ok ? "READY TO SEND" : "CANNOT SEND YET";
  $("confirm-dialog").showModal();
}

async function onConfirmSend() {
  const btn = $("confirm-send");
  btn.disabled = true;
  try {
    const res = await api("/api/send", { method: "POST", body: sendPayload(true) });
    $("confirm-dialog").close();
    state.jobId = res.job_id;
    startPolling();
    loadActiveJobs();
  } catch (err) {
    $("confirm-warnings").replaceChildren(el("li", { class: "error", text: err.message }));
  } finally {
    btn.disabled = false;
  }
}

/* ------------------------------------------------------------ progress & results */

function startPolling() {
  stopPolling();
  $("progress-card").hidden = false;
  $("progress-card").scrollIntoView({ block: "start" });
  pollJob();
}

function stopPolling() {
  clearTimeout(state.pollTimer);
  state.pollTimer = null;
}

async function pollJob() {
  if (!state.jobId) return;
  try {
    const job = await api(`/api/jobs/${state.jobId}`);
    renderJob(job);
    if (!job.finished) state.pollTimer = setTimeout(pollJob, 1000);
    else loadMailbox();
  } catch (err) {
    state.pollTimer = setTimeout(pollJob, 3000);
  }
}

function renderJob(job) {
  $("progress-card").hidden = false;
  $("progress-account").textContent = `“${job.subject}” via ${job.smtp_account}`;
  const titles = { completed: "SENDING COMPLETE", cancelled: "SENDING STOPPED", failed: "SENDING STOPPED", interrupted: "SENDING INTERRUPTED" };
  $("progress-title").textContent = titles[job.status] || "Sending…";
  const bar = $("progress-bar");
  bar.style.width = `${job.percent}%`;
  bar.classList.toggle("done", job.finished);
  $("progress-pct").textContent = `${job.percent}%`;
  $("pg-completed").textContent = `${fmt(job.completed)} / ${fmt(job.total)}`;
  $("pg-success").textContent = fmt(job.successful);
  $("pg-failed").textContent = fmt(job.failed + job.not_sent);
  $("pg-batch").textContent = `${fmt(job.current_batch)} / ${fmt(job.total_batches)}`;
  $("pg-status").textContent = job.status_text || job.status;
  $("running-actions").hidden = job.finished;
  $("done-actions").hidden = !job.finished;
  $("export-failed-btn").href = `/api/jobs/${job.id}/export?status=failed`;
  $("export-all-btn").href = `/api/jobs/${job.id}/export`;
}

/* Every send still running for this user, including ones started before logging out. */
async function loadActiveJobs() {
  clearTimeout(state.activeTimer);
  let jobs = [];
  try { jobs = await api("/api/jobs/active"); } catch (_) { /* retry below */ }
  $("active-card").hidden = !jobs.length;
  $("active-list").replaceChildren(...jobs.map((job) => el("li", { class: job.id === state.jobId ? "viewing" : "" },
    el("div", { class: "job-name" }, el("strong", { text: job.subject || "(no subject)" }), el("br"),
      el("small", { class: "muted", text: `${job.smtp_account} · ${fmt(job.completed)} / ${fmt(job.total)} · ${job.status_text || job.status}` })),
    el("div", { class: "progress" }, el("div", { class: "progress-bar", style: `width:${job.percent}%` })),
    el("button", { class: "btn", type: "button", text: job.id === state.jobId ? "Viewing" : "View",
      disabled: job.id === state.jobId, onclick: () => { state.jobId = job.id; startPolling(); loadActiveJobs(); } }),
  )));
  state.activeTimer = setTimeout(loadActiveJobs, jobs.length ? 3000 : 15000);
}

async function onCancelJob() {
  if (!state.jobId || !confirm("Stop sending? Batches already sent cannot be recalled.")) return;
  try { await api(`/api/jobs/${state.jobId}/cancel`, { method: "POST" }); } catch (err) { alert(err.message); }
}

async function onViewFailed() {
  const rows = await api(`/api/jobs/${state.jobId}/results?status=failed`);
  $("failed-rows").replaceChildren(...(rows.length ? rows.map((r) => el("tr", {},
    el("td", { class: "wrap", text: r.email }),
    el("td", { text: r.status.toUpperCase() }),
    el("td", { text: r.smtp_response || "" }),
    el("td", { text: fmtTime(r.updated_at) }),
  )) : [el("tr", {}, el("td", { colspan: "4", text: "No failed recipients." }))]));
  $("failed-dialog").showModal();
}

async function onNewEmail() {
  if (!confirm("Start a new email? This clears the recipients, message and attachments.")) return;
  for (const a of [...state.attachments]) await removeAttachment(a.id);
  $("recipients").value = "";
  $("subject").value = "";
  $("body").value = "";
  $("personalize").checked = false;
  $("personalize-opts").hidden = true;
  showMsg("import-msg", ""); showMsg("test-msg", ""); showMsg("send-msg", "");
  $("progress-card").hidden = true;
  state.jobId = null;
  await parseRecipients();
  window.scrollTo(0, 0);
}

/* ------------------------------------------------------------ dev mailbox */

async function loadMailbox() {
  if (!state.config || state.config.email_mode !== "development") return;
  const rows = await api("/api/dev/outbox");
  $("mailbox-rows").replaceChildren(...(rows.length ? rows.map((m) => {
    const preview = m.envelope_to.slice(0, 3).join(", ") + (m.envelope_to.length > 3 ? ` … (+${m.envelope_to.length - 3})` : "");
    return el("tr", {},
      el("td", { text: String(m.id) }),
      el("td", { text: fmtTime(m.created_at) }),
      el("td", { text: m.subject || "(no subject)" }),
      el("td", { class: "wrap", text: `${m.envelope_to.length} → ${preview}` }),
      el("td", {}, el("button", { class: "btn link", type: "button", text: "Open", onclick: () => openMessage(m.id) })),
    );
  }) : [el("tr", {}, el("td", { colspan: "5", text: "No messages yet." }))]));
}

async function openMessage(id) {
  const m = await api(`/api/dev/outbox/${id}`);
  const subject = (m.headers.find(([k]) => k.toLowerCase() === "subject") || [, ""])[1];
  $("msg-subject").textContent = subject || "(no subject)";
  const envelope = [
    ["Envelope from", m.envelope_from],
    [`Envelope to (${m.envelope_to.length})`, m.envelope_to.join(", ")],
  ];
  $("msg-envelope").replaceChildren(...envelope.flatMap(([k, v]) => [el("dt", { text: k }), el("dd", { text: v })]));
  $("msg-headers").textContent = m.headers.map(([k, v]) => `${k}: ${v}`).join("\n") + "\n\nMIME structure:\n" + m.structure;
  if (m.html) {
    $("msg-frame").hidden = false; $("msg-text").hidden = true;
    $("msg-frame").srcdoc = m.html;
  } else {
    $("msg-frame").hidden = true; $("msg-text").hidden = false;
    $("msg-text").textContent = m.text || "";
  }
  $("msg-attachments").textContent = m.attachments.length
    ? "Attachments: " + m.attachments.map((a) => `${a.filename} (${fmtSize(a.size)})`).join(", ") : "";
  $("msg-raw").href = `/api/dev/outbox/${id}/raw`;
  $("message-dialog").showModal();
}

async function onClearMailbox() {
  if (!confirm("Delete all messages in the test mailbox?")) return;
  await api("/api/dev/outbox", { method: "DELETE" });
  loadMailbox();
}

/* ------------------------------------------------------------ startup */

async function startApp() {
  $("login-view").hidden = true;
  $("app-view").hidden = false;
  state.config = await api("/api/config");
  const me = state.config.user;
  $("user-email").textContent = me.email;
  $("role-badge").hidden = me.role !== "admin";
  $("password-btn").hidden = me.main_admin;  // main admin password comes from .env
  $("users-card").hidden = me.role !== "admin";
  const dev = state.config.email_mode === "development";
  $("mode-badge").textContent = dev ? "DEVELOPMENT" : "PRODUCTION";
  $("mode-badge").className = "badge " + (dev ? "dev" : "prod");
  $("dev-banner").hidden = !dev;
  $("mailbox-card").hidden = !dev;
  $("batch-size").value = state.config.default_batch_size;
  $("batch-size").max = state.config.max_batch_size;
  $("attach-limits").textContent =
    `Max ${state.config.max_attachment_mb} MB per file, ${state.config.max_message_mb} MB per email. ` +
    `Allowed: ${state.config.allowed_extensions.join(", ")}`;

  fillSmtp(await api("/api/smtp"));
  await Promise.all([loadAttachments(), parseRecipients(), loadMailbox(), loadTemplates(""), loadProfiles(""), loadSuppression(), loadUsers()]);
  onFormatChange();

  const latest = await api("/api/jobs/latest");
  if (latest) {
    state.jobId = latest.id;
    if (!latest.finished) startPolling(); else renderJob(latest);
  }
  loadActiveJobs();
}

function bind() {
  $("login-form").addEventListener("submit", login);
  $("auth-toggle").addEventListener("click", () => setAuthMode(!state.registering));
  $("password-btn").addEventListener("click", () => { showMsg("pw-msg", ""); $("password-dialog").showModal(); });
  $("password-form").addEventListener("submit", onChangePassword);
  $("logout-btn").addEventListener("click", logout);
  $("smtp-form").addEventListener("submit", onSaveSmtp);
  $("smtp-test-btn").addEventListener("click", onTestSmtp);
  $("deliv-btn").addEventListener("click", onCheckDeliverability);
  $("suppress-btn").addEventListener("click", async () => { await loadSuppression(); $("suppress-msg").textContent = ""; $("suppress-dialog").showModal(); });
  $("suppress-add").addEventListener("click", onSuppressAdd);
  $("prof-use").addEventListener("click", onProfUse);
  $("prof-save").addEventListener("click", onProfSave);
  $("prof-delete").addEventListener("click", onProfDelete);
  $("tpl-load").addEventListener("click", onTplLoad);
  $("tpl-save").addEventListener("click", onTplSave);
  $("tpl-update").addEventListener("click", onTplUpdate);
  $("tpl-delete").addEventListener("click", onTplDelete);
  $("delivery").addEventListener("change", updateBatchInfo);
  ["from-name", "from-email", "reply-to"].forEach((id) => $(id).addEventListener("input", updateSenderLine));
  $("recipients").addEventListener("input", scheduleParse);
  $("csv-input").addEventListener("change", onImportCsv);
  $("clean-btn").addEventListener("click", onCleanRecipients);
  $("clear-recipients-btn").addEventListener("click", () => { $("recipients").value = ""; showMsg("import-msg", ""); parseRecipients(); });
  document.querySelectorAll('input[name="format"]').forEach((r) => r.addEventListener("change", onFormatChange));
  $("toolbar").addEventListener("click", onToolbar);
  $("preview-btn").addEventListener("click", onPreview);
  $("personalize").addEventListener("change", () => { $("personalize-opts").hidden = !$("personalize").checked; updateBatchInfo(); });
  $("attach-input").addEventListener("change", onAttach);
  $("batch-size").addEventListener("input", updateBatchInfo);
  restorePace();
  $("speed").addEventListener("change", () => { savePace(); updateBatchInfo(); });
  $("conservative-delay").addEventListener("change", () => { savePace(); updateBatchInfo(); });
  $("test-btn").addEventListener("click", onSendTest);
  $("send-btn").addEventListener("click", onSendClick);
  $("confirm-cancel").addEventListener("click", () => $("confirm-dialog").close());
  $("confirm-send").addEventListener("click", onConfirmSend);
  $("cancel-btn").addEventListener("click", onCancelJob);
  $("view-failed-btn").addEventListener("click", onViewFailed);
  $("new-email-btn").addEventListener("click", onNewEmail);
  $("mailbox-refresh").addEventListener("click", loadMailbox);
  $("mailbox-clear").addEventListener("click", onClearMailbox);
  ["preview-dialog", "failed-dialog", "message-dialog", "suppress-dialog", "password-dialog"].forEach((id) => closeOnButtons($(id)));
}

document.addEventListener("DOMContentLoaded", async () => {
  bind();
  try {
    const s = await api("/api/auth/session");
    $("auth-switch").hidden = !s.registration;
    if (s.authenticated) {
      state.csrf = s.csrf;
      await startApp();
    } else {
      showLogin();
    }
  } catch (_) {
    showLogin();
  }
});
