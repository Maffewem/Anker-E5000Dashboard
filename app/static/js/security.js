"use strict";

// ---------- Lock ----------
// With a password (ADMIN_PASSWORD or one set here), changes need signing in;
// with READ_ONLY they're off.
// The server enforces both; this only hides the controls that would fail.

let auth = { password_set: false, read_only: false, signed_in: false, can_edit: true, csrf: null };

function applyAuth(a) {
  auth = a;
  document.body.classList.toggle("locked", !a.can_edit);
  $("control-fields").disabled = !a.can_edit;
  const btn = $("lock-btn");
  btn.hidden = false;
  btn.classList.toggle("open", a.can_edit);
  const label = a.read_only ? "Read-only: changes are turned off in the container settings"
    : !a.password_set ? "Unlocked: anyone can make changes. Click to set a password"
    : a.signed_in ? "Signed in. Click to sign out or change the password" : "Locked. Click to sign in and make changes";
  btn.title = label;
  btn.setAttribute("aria-label", label);
}

async function refreshAuth() {
  try { applyAuth(await (await fetch("/api/auth", { cache: "no-store" })).json()); } catch (_) {}
}

const FORGOT = " If you forget it, delete auth.json from the data volume and restart the container.";

function openPassword() {
  const env = auth.password_source === "env";
  $("pw-intro").textContent = env ? "The password is set by ADMIN_PASSWORD in the container settings. Change it there."
    : auth.password_set ? "Changes are locked behind this password. Change it here, or remove it to unlock the dashboard." + FORGOT
    : "Anyone who can open the dashboard can change its settings and the battery. Set a password to lock changes; viewing stays open." + FORGOT;
  $("pw-current-row").hidden = env || !auth.password_set;
  $("pw-new-rows").hidden = env;
  $("pw-save").hidden = env;
  $("pw-save").textContent = auth.password_set ? "Change password" : "Set password";
  $("pw-remove").hidden = env || !auth.password_set;
  $("pw-signout").hidden = !auth.signed_in;
  for (const id of ["pw-current", "pw-new", "pw-confirm"]) $(id).value = "";
  $("pw-msg").textContent = "";
  $("pw").showModal();
}

async function sendPassword(body) {
  try {
    applyAuth(await postSettings("/api/auth/password", body));
    $("pw").close();
  } catch (err) {
    $("pw-msg").className = "setup-msg error";
    $("pw-msg").textContent = err.message;
  }
}

$("pw-form").addEventListener("submit", (e) => {
  e.preventDefault();
  if ($("pw-new").value !== $("pw-confirm").value) {
    $("pw-msg").className = "setup-msg error";
    $("pw-msg").textContent = "The two new passwords don't match.";
    return;
  }
  sendPassword({ current: $("pw-current").value, new: $("pw-new").value });
});
$("pw-remove").addEventListener("click", () => {
  if (!$("pw-current").value) {
    $("pw-msg").className = "setup-msg error";
    $("pw-msg").textContent = "Enter the current password to remove it.";
    return;
  }
  sendPassword({ current: $("pw-current").value, new: "" });
});
$("pw-signout").addEventListener("click", async () => {
  try { applyAuth(await (await fetch("/api/auth/logout", { method: "POST" })).json()); } catch (_) {}
  $("pw").close();
  refreshOctopus();
});
$("pw-cancel").addEventListener("click", () => $("pw").close());

$("lock-btn").addEventListener("click", async () => {
  if (auth.read_only) return;
  if (!auth.password_set || auth.signed_in) { openPassword(); return; }
  $("login-msg").textContent = "";
  $("login-password").value = "";
  $("login").showModal();
  $("login-password").focus();
});

$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    applyAuth(await postSettings("/api/auth/login", { password: $("login-password").value }));
    $("login").close();
    refreshOctopus();
    refreshControl();
  } catch (err) {
    $("login-msg").className = "setup-msg error";
    $("login-msg").textContent = err.message;
  }
});
$("login-cancel").addEventListener("click", () => $("login").close());

// ---------- Updates ----------
// The server compares its build with the newest published image. "Update now"
// asks Portainer (through the stack's webhook) to re-pull and recreate it.

let updates = null;
const buildDate = (d) => (d ? new Date(`${d}T12:00:00Z`).toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" }) : "");
const versionLabel = (v) => (v && v.name ? `v${v.name}` : "a newer version");

function renderUpdates(u) {
  updates = u;
  $("update-note").hidden = !u.update_available;
  if (u.update_available) {
    const built = buildDate(u.latest.built);
    $("update-text").textContent = `Update available: ${versionLabel(u.latest)}${built ? `, built ${built}` : ""}.`
      + (u.webhook_set ? "" : " Re-pull the stack in Portainer to install it.");
  }
  $("update-now").hidden = !u.webhook_set;
}

async function refreshUpdates() {
  try { renderUpdates(await (await fetch("/api/version", { cache: "no-store" })).json()); } catch (_) {}
}

function openUpdates() {
  const u = updates || {};
  const running = u.running && u.running.name !== "dev" ? `v${u.running.name}` : "a development build";
  $("updates-status").textContent = `Running ${running}. `
    + (u.error ? u.error : !u.checked_at ? "Not checked for updates yet."
      : u.update_available ? `${versionLabel(u.latest)} is available.` : "This is the newest version.");
  $("updates-url").value = "";
  $("updates-url").placeholder = u.webhook_set ? "Saved (paste a new one to replace it)" : "https://192.168.0.10:9443/api/stacks/webhooks/…";
  for (const id of ["updates-url", "updates-save"]) $(id).disabled = Boolean(u.webhook_locked);
  $("updates-locked").hidden = !u.webhook_locked;
  $("updates-remove").hidden = !u.webhook_set || u.webhook_locked;
  $("updates-msg").textContent = "";
  $("updates").showModal();
}

async function saveWebhook(url) {
  try {
    renderUpdates(await postSettings("/api/update/webhook", { url }));
    $("updates").close();
  } catch (err) {
    $("updates-msg").className = "setup-msg error";
    $("updates-msg").textContent = err.message;
  }
}

async function waitForNewVersion(from) {
  // The container is replaced while this runs, so failed requests are expected.
  for (let i = 0; i < 60; i++) {
    await new Promise((r) => setTimeout(r, 5000));
    try {
      const v = (await (await fetch("/api/live", { cache: "no-store" })).json()).version;
      if (v && v.commit !== from) { location.reload(); return; }
    } catch (_) {}
  }
  $("update-text").textContent = "The update hasn't come through yet. Check the stack in Portainer.";
  $("update-now").disabled = false;
}

$("update-now").addEventListener("click", async () => {
  const from = updates && updates.running && updates.running.commit;
  $("update-now").disabled = true;
  $("update-note").hidden = false;
  $("update-text").textContent = "Updating. The dashboard reloads when the new version is running.";
  try {
    await postSettings("/api/update", {});
  } catch (err) {
    if (!(err instanceof TypeError)) {  // a TypeError is the connection closing as the container restarts
      $("update-text").textContent = err.message;
      $("update-now").disabled = false;
      return;
    }
  }
  waitForNewVersion(from);
});
$("update-open").addEventListener("click", openUpdates);
$("footer-updates").addEventListener("click", openUpdates);
$("updates-form").addEventListener("submit", (e) => {
  e.preventDefault();
  if ($("updates-url").value.trim()) saveWebhook($("updates-url").value.trim());
  else $("updates").close();
});
$("updates-remove").addEventListener("click", () => saveWebhook(""));
$("updates-cancel").addEventListener("click", () => $("updates").close());
