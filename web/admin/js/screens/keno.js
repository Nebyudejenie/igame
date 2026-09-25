import { getRole } from "../api.js";
import * as overview from "./keno/overview.js";
import * as rounds from "./keno/rounds.js";
import * as paytables from "./keno/paytables.js";
import * as tiers from "./keno/tiers.js";
import * as simulator from "./keno/simulator.js";
import * as reports from "./keno/reports.js";

export const label = "Keno";

// Sub-sections of the one Keno nav entry, in display order. `roles` mirrors
// services/admin/rbac.py for visibility only -- keno:view is every role,
// the simulator needs keno:manage (ops/superadmin). The backend is the real
// enforcement; hiding a tab just saves a click-then-403.
const SECTIONS = [
  { key: "overview", label: "Overview", mod: overview },
  { key: "rounds", label: "Rounds", mod: rounds },
  { key: "paytables", label: "Paytables", mod: paytables },
  { key: "tiers", label: "Tiers & access", mod: tiers },
  { key: "simulator", label: "Risk simulator", mod: simulator, roles: ["ops", "superadmin"] },
  { key: "reports", label: "Reports", mod: reports },
];

const SECTION_KEY = "jobingo_admin_keno_section";

function readSection() {
  try {
    return localStorage.getItem(SECTION_KEY) || "overview";
  } catch {
    return "overview";
  }
}

function writeSection(key) {
  try {
    localStorage.setItem(SECTION_KEY, key);
  } catch {
    // per-browser convenience only -- nothing depends on it persisting
  }
}

export async function render(container) {
  const role = getRole();
  const visible = SECTIONS.filter((s) => !s.roles || s.roles.includes(role));
  let active = visible.find((s) => s.key === readSection()) ? readSection() : "overview";

  container.innerHTML = `
    <h1>Keno</h1>
    <div class="subnav" id="keno-subnav"></div>
    <div id="keno-section"></div>
  `;
  const subnav = container.querySelector("#keno-subnav");
  const sectionEl = container.querySelector("#keno-section");

  async function show(key) {
    active = key;
    writeSection(key);
    subnav.innerHTML = visible.map((s) => `
      <button class="subnav-btn ${s.key === key ? "active" : ""}" data-section="${s.key}">${s.label}</button>
    `).join("");
    for (const btn of subnav.querySelectorAll("[data-section]")) {
      btn.addEventListener("click", () => show(btn.dataset.section));
    }
    sectionEl.innerHTML = `<p class="loading">Loading…</p>`;
    await visible.find((s) => s.key === key).mod.render(sectionEl, { role });
  }

  await show(active);
}
