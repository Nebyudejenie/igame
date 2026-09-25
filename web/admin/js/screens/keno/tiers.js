import { api, escapeHtml, fmtDate } from "../../api.js";
import { renderError, toast } from "../../ui.js";

// Latest version per tier_number -- keno_risk_tiers.version is scoped per
// tier_number, not shared across the ladder (see docs/keno/02-data-model.md).
function latestPerTier(tiers) {
  const best = new Map();
  for (const tier of tiers) {
    const current = best.get(tier.tier_number);
    if (!current || tier.version > current.version) best.set(tier.tier_number, tier);
  }
  return [...best.values()].sort((a, b) => a.tier_number - b.tier_number);
}

export async function render(container, { role }) {
  let tiers;
  let allowlist;
  try {
    [tiers, allowlist] = await Promise.all([api("/keno/tiers"), api("/keno/beta-allowlist")]);
  } catch (err) {
    renderError(container, err);
    return;
  }
  const superadmin = role === "superadmin";
  const ladder = latestPerTier(tiers);
  const current = tiers.find((t) => t.is_current);

  // The engine pins a specific tier *version* (keno_tier_state.current_tier_id),
  // so editing the current tier creates a newer version it won't use until
  // someone adopts it -- that case gets its own label, not "Make current".
  function tierAction(t) {
    if (current && current.id === t.id) return "";
    const adopting = current && current.tier_number === t.tier_number;
    return `<button class="btn btn-secondary" data-set-tier="${t.id}" data-tier-number="${t.tier_number}">
      ${adopting ? `Adopt latest version (v${t.version})` : "Make current"}</button>`;
  }

  container.innerHTML = `
    <h2>Risk tier ladder</h2>
    <p class="field-hint">Promotion needs the reserve above a tier's minimum for 7 consecutive days; demotion is
      immediate. A tier change only ever affects the next round.</p>
    ${current && ladder.some((t) => t.tier_number === current.tier_number && t.id !== current.id)
      ? `<p class="warning-text">Tier ${current.tier_number} has a newer version
          (v${escapeHtml(String(ladder.find((t) => t.tier_number === current.tier_number).version))}) that is not in use --
          rounds still run on v${escapeHtml(String(current.version))} until it is adopted.</p>` : ""}
    <table class="data-table">
      <thead><tr><th>Tier</th><th>Min reserve</th><th>Max picks</th><th>Top multiplier</th><th>Stakes</th>
        <th>Max win/ticket</th><th>Round exposure cap</th><th>Profile</th><th></th></tr></thead>
      <tbody>${ladder.map((t) => `<tr>
        <td>${t.tier_number}${current && current.tier_number === t.tier_number ? ` <span class="badge badge-active">current</span>` : ""}</td>
        <td>${escapeHtml(t.min_reserve)}</td><td>${t.max_pick_count}</td><td>${escapeHtml(t.max_top_multiplier)}×</td>
        <td>${t.stake_options.map(escapeHtml).join(", ")}</td><td>${escapeHtml(t.max_win_per_ticket)}</td>
        <td>${(Number(t.max_round_exposure_pct) * 100).toFixed(1)}% of reserve</td><td>${escapeHtml(t.paytable_profile)}</td>
        <td>${superadmin ? tierAction(t) : ""}</td>
      </tr>`).join("")}
      </tbody>
    </table>

    <h2>Beta allowlist</h2>
    <p class="field-hint">While the active config has <em>beta allowlist enforced</em>, only these players can see or
      play Keno, even with Keno enabled.</p>
    ${allowlist.length ? `<table class="data-table">
      <thead><tr><th>User ID</th><th>Reason</th><th>Added by admin</th><th>Added</th><th></th></tr></thead>
      <tbody>${allowlist.map((row) => `<tr>
        <td>${row.user_id}</td><td>${escapeHtml(row.reason)}</td><td>${row.added_by_admin_id}</td><td>${fmtDate(row.created_at)}</td>
        <td>${superadmin ? `<button class="btn btn-danger" data-remove-user="${row.user_id}">Remove</button>` : ""}</td>
      </tr>`).join("")}</tbody></table>` : `<p class="empty">Nobody is on the allowlist.</p>`}
    ${superadmin ? `
      <form id="allowlist-form" class="detail-panel inline-form">
        <label>User ID <input type="number" name="user_id" required min="1" /></label>
        <label>Reason <input type="text" name="reason" required minlength="10" /></label>
        <button type="submit" class="btn">Add to allowlist</button>
      </form>` : ""}
  `;
  if (!superadmin) return;

  async function act(fn, success) {
    try {
      await fn();
      toast(success);
      await render(container, { role });
    } catch (err) {
      toast(err.detail || err.message, true);
    }
  }

  for (const btn of container.querySelectorAll("[data-set-tier]")) {
    btn.addEventListener("click", () => {
      const reason = prompt(`Reason for overriding the current tier to tier ${btn.dataset.tierNumber}:`);
      if (!reason) return;
      act(() => api("/keno/tiers/set-current", { method: "POST", body: { tier_id: Number(btn.dataset.setTier), reason } }),
        `Tier ${btn.dataset.tierNumber} is now current`);
    });
  }
  for (const btn of container.querySelectorAll("[data-remove-user]")) {
    btn.addEventListener("click", () => {
      const reason = prompt(`Reason for removing user ${btn.dataset.removeUser} from the allowlist:`);
      if (!reason) return;
      const params = new URLSearchParams({ reason });
      act(() => api(`/keno/beta-allowlist/${btn.dataset.removeUser}?${params}`, { method: "DELETE" }), "Removed");
    });
  }
  container.querySelector("#allowlist-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const body = { user_id: Number(e.target.user_id.value), reason: e.target.reason.value.trim() };
    act(() => api("/keno/beta-allowlist", { method: "POST", body }), `User ${body.user_id} added`);
  });
}
