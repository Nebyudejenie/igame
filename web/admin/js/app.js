import { getToken, clearToken, getRole, clearRole, api, ApiError } from "./api.js";
import { renderError } from "./ui.js";
import { initGlobalSearch } from "./global_search.js";
import * as loginScreen from "./screens/login.js";
import * as dashboardScreen from "./screens/dashboard.js";
import * as usersScreen from "./screens/users.js";
import * as paymentsScreen from "./screens/payments.js";
import * as manualDepositsScreen from "./screens/manual_deposits.js";
import * as manualWithdrawalsScreen from "./screens/manual_withdrawals.js";
import * as paymentDestinationsScreen from "./screens/payment_destinations.js";
import * as telebirrEvidenceScreen from "./screens/telebirr_evidence.js";
import * as paymentAgentsScreen from "./screens/payment_agents.js";
import * as ingestionDevicesScreen from "./screens/ingestion_devices.js";
import * as providerAvailabilityScreen from "./screens/provider_availability.js";
import * as roundsScreen from "./screens/rounds.js";
import * as roomsScreen from "./screens/rooms.js";
import * as notificationsScreen from "./screens/notifications.js";
import * as botContentScreen from "./screens/bot_content.js";
import * as reportsScreen from "./screens/reports.js";
import * as riskScreen from "./screens/risk.js";
import * as auditScreen from "./screens/audit.js";
import * as adminUsersScreen from "./screens/admin_users.js";
import * as bonusesScreen from "./screens/bonuses.js";
import * as telegramHealthScreen from "./screens/telegram_health.js";
import * as simulatedPlayersScreen from "./screens/simulated_players.js";
import * as announcementScreen from "./screens/announcement.js";

// Order here is the nav order. Each screen owns its own error handling
// (an inline banner using the real API error detail, e.g. "role 'support'
// lacks 'payments:view'") -- the try/catch below is only a safety net for
// a screen module throwing somewhere it didn't already handle.
const SCREENS = {
  dashboard: dashboardScreen,
  users: usersScreen,
  payments: paymentsScreen,
  manual_deposits: manualDepositsScreen,
  manual_withdrawals: manualWithdrawalsScreen,
  payment_destinations: paymentDestinationsScreen,
  telebirr_evidence: telebirrEvidenceScreen,
  payment_agents: paymentAgentsScreen,
  ingestion_devices: ingestionDevicesScreen,
  provider_availability: providerAvailabilityScreen,
  rounds: roundsScreen,
  rooms: roomsScreen,
  bonuses: bonusesScreen,
  notifications: notificationsScreen,
  bot_content: botContentScreen,
  telegram_health: telegramHealthScreen,
  simulated_players: simulatedPlayersScreen,
  announcement: announcementScreen,
  reports: reportsScreen,
  risk: riskScreen,
  audit: auditScreen,
  admin_users: adminUsersScreen,
};

// A client-side mirror of services/admin/rbac.py's *:view permissions,
// for nav visibility only -- an architecture audit caught that every
// screen was shown to every role regardless, with a 403 only ever
// discovered after the click, the opposite of least-privilege applied
// to the UI. `null` means every role can see it (dashboard/users/
// payments/rounds/rooms:view are all granted to support/finance/ops/
// superadmin already). The backend remains the sole real enforcement --
// this only ever hides a button faster than a click-then-403 would.
const SCREEN_VIEW_ROLES = {
  reports: ["finance", "superadmin"],
  risk: ["ops", "finance", "superadmin"],
  notifications: ["ops", "superadmin"],
  bot_content: ["ops", "superadmin"],
  audit: ["superadmin"],
  admin_users: ["superadmin"],
};

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
    <div class="nav-brand">Arada Bingo Admin</div>
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
  // Reflected in the URL (not pushed as a new history entry -- nav clicks
  // shouldn't pile up back-button stops) purely so a refresh lands back on
  // the same section instead of always resetting to the dashboard.
  history.replaceState(null, "", `#${name}`);
  buildNav(name);
  contentEl.innerHTML = `<p class="loading">Loading…</p>`;
  try {
    await SCREENS[name].render(contentEl);
  } catch (err) {
    if (err instanceof ApiError && err.status === 403) {
      contentEl.innerHTML = `<div class="error-banner">Your role does not have access to this screen.</div>`;
    } else if (err instanceof ApiError && err.status === 401) {
      // handled globally by the admin:unauthorized listener below
    } else {
      // A code-review pass caught this as the one unescaped innerHTML
      // assignment in the whole admin frontend -- every per-screen
      // catch already goes through renderError() (see its own comment
      // in ui.js), but this outer safety net (only reached when a
      // screen module throws something it didn't already handle
      // itself) didn't. err.message can carry real backend-supplied
      // text (ApiError's own message is built from the response body's
      // `detail` field), so an unescaped admin-submitted value echoed
      // back in an error message would have been a real, if narrow,
      // admin-to-admin XSS path.
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

function screenFromUrl() {
  const name = location.hash.slice(1);
  return name in SCREENS ? name : "dashboard";
}

function showApp() {
  loginEl.hidden = true;
  shellEl.hidden = false;
  showScreen(screenFromUrl());
}

function showLogin() {
  shellEl.hidden = true;
  loginEl.hidden = false;
  loginScreen.render(loginEl, showApp);
}

window.addEventListener("admin:unauthorized", showLogin);

initGlobalSearch(showScreen);

if (getToken()) {
  showApp();
} else {
  showLogin();
}
