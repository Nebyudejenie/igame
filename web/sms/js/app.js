import { getToken, clearToken, getRole, clearRole, api, ApiError } from "./api.js";
import { renderError } from "./ui.js";
import * as loginScreen from "./screens/login.js";
import * as overviewScreen from "./screens/overview.js";
import * as campaignsScreen from "./screens/campaigns.js";
import * as importScreen from "./screens/import.js";
import * as templatesScreen from "./screens/templates.js";
import * as contactsScreen from "./screens/contacts.js";
import * as nodesScreen from "./screens/nodes.js";
import * as suppressionsScreen from "./screens/suppressions.js";

// Order here is the nav order.
const SCREENS = {
  overview: overviewScreen,
  campaigns: campaignsScreen,
  import: importScreen,
  templates: templatesScreen,
  contacts: contactsScreen,
  nodes: nodesScreen,
  suppressions: suppressionsScreen,
};

// A client-side mirror of services/admin/rbac.py's sms:* view permissions,
// for nav visibility only -- the backend remains the sole real
// enforcement (mirrors web/admin/js/app.js's own SCREEN_VIEW_ROLES).
// Every screen here only needs the broad sms:view permission to be
// visible at all; per-action buttons can still 403 for a narrower role
// (e.g. support can see Campaigns but its create/start buttons will 403).
const SCREEN_VIEW_ROLES = {};

function visibleScreens(role) {
  return Object.entries(SCREENS).filter(
    ([name]) => !SCREEN_VIEW_ROLES[name] || SCREEN_VIEW_ROLES[name].includes(role)
  );
}

const loginEl = document.getElementById("login-screen");
const shellEl = document.getElementById("app-shell");
const navEl = document.getElementById("nav");
const contentEl = document.getElementById("content");

function buildNav(active) {
  navEl.innerHTML = `
    <div class="nav-brand">Zemen Game SMS</div>
    ${visibleScreens(getRole()).map(([name, mod]) => `
      <button class="nav-btn ${name === active ? "active" : ""}" data-screen="${name}">${mod.label}</button>
    `).join("")}
    <button class="nav-btn nav-logout" id="logout-btn">Log out</button>
  `;
  for (const btn of navEl.querySelectorAll(".nav-btn[data-screen]")) {
    btn.addEventListener("click", () => showScreen(btn.dataset.screen));
  }
  navEl.querySelector("#logout-btn").addEventListener("click", doLogout);
}

async function showScreen(name) {
  buildNav(name);
  contentEl.innerHTML = `<p class="loading">Loading…</p>`;
  try {
    await SCREENS[name].render(contentEl);
  } catch (err) {
    if (err instanceof ApiError && err.status === 403) {
      contentEl.innerHTML = `<div class="error-banner">Your role does not have access to this screen.</div>`;
    } else if (err instanceof ApiError && err.status === 401) {
      // handled globally by the sms:unauthorized listener below
    } else {
      renderError(contentEl, err);
    }
  }
}

async function doLogout() {
  try {
    await api("/auth/logout", { method: "POST" });
  } catch {
    // best-effort -- the token gets cleared client-side regardless
  }
  clearToken();
  clearRole();
  showLogin();
}

function showApp() {
  loginEl.hidden = true;
  shellEl.hidden = false;
  showScreen("overview");
}

function showLogin() {
  shellEl.hidden = true;
  loginEl.hidden = false;
  loginScreen.render(loginEl, showApp);
}

window.addEventListener("sms:unauthorized", showLogin);

if (getToken()) {
  showApp();
} else {
  showLogin();
}
