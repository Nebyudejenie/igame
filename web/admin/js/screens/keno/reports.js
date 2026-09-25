import { api, escapeHtml } from "../../api.js";
import { renderError } from "../../ui.js";

function pct(value) {
  return value === null || value === undefined ? "—" : `${(Number(value) * 100).toFixed(1)}%`;
}

function num(value, digits = 2) {
  return Number(value).toFixed(digits);
}

export async function render(container) {
  container.innerHTML = `
    <h2>Last 24 hours</h2>
    <div id="kpis"><p class="loading">Loading…</p></div>
    <h2>Daily</h2>
    <div class="inline-form" style="margin-bottom:0.75rem">
      <label>Days <select id="report-days">${[7, 14, 30, 90].map((d) => `<option ${d === 14 ? "selected" : ""}>${d}</option>`).join("")}</select></label>
    </div>
    <div id="daily"><p class="loading">Loading…</p></div>
  `;
  const kpiEl = container.querySelector("#kpis");
  const dailyEl = container.querySelector("#daily");
  const daysEl = container.querySelector("#report-days");

  try {
    const k = await api("/keno/reports/kpis");
    const card = (label, value) => `<div class="stat-card"><div class="stat-label">${label}</div><div class="stat-value">${value}</div></div>`;
    kpiEl.innerHTML = `
      <div class="stat-grid">
        ${card("GGR", `${escapeHtml(k.ggr)} ETB`)}
        ${card("Hold", pct(k.hold_pct))}
        ${card("Daily active players", k.dau)}
        ${card("ARPDAU", `${escapeHtml(k.arpdau)} ETB`)}
        ${card("Sessions / player", num(k.sessions_per_user))}
        ${card("Rounds / session", num(k.rounds_per_session))}
        ${card("Tickets / round", num(k.tickets_per_round))}
        ${card("Average stake", `${escapeHtml(k.avg_stake)} ETB`)}
        ${card("D1 / D7 / D30 retention", `${pct(k.d1_retention)} / ${pct(k.d7_retention)} / ${pct(k.d30_retention)}`)}
        ${card("Player LTV (active 30d)", `${escapeHtml(k.player_ltv)} ETB`)}
        ${card("Deposit conversion", pct(k.deposit_conversion_rate))}
      </div>
      <p class="field-hint">GGR = players × sessions/player × rounds/session × tickets/round × avg stake × hold.
        A session is a run of tickets with no gap over 30 minutes. "—" on a retention or conversion figure means there is
        no cohort or no completed deposit yet, which is not the same as 0%. Definitions: docs/keno/07-economics-and-bankroll.md.</p>`;
  } catch (err) {
    renderError(kpiEl, err);
  }

  async function loadDaily() {
    dailyEl.innerHTML = `<p class="loading">Loading…</p>`;
    try {
      const rows = await api(`/keno/reports/daily?days=${daysEl.value}`);
      dailyEl.innerHTML = rows.length ? `
        <table class="data-table">
          <thead><tr><th>Day (UTC)</th><th>Handle</th><th>Paid out</th><th>GGR</th><th>Hold</th>
            <th>Tickets</th><th>Players</th><th>Rounds</th></tr></thead>
          <tbody>${rows.map((r) => `<tr>
            <td>${escapeHtml(r.day)}</td><td>${escapeHtml(r.handle)}</td><td>${escapeHtml(r.paid)}</td><td>${escapeHtml(r.ggr)}</td>
            <td>${r.hold_pct === null ? "—" : pct(r.hold_pct)}</td><td>${r.tickets}</td><td>${r.players}</td><td>${r.rounds}</td>
          </tr>`).join("")}</tbody>
        </table>` : `<p class="empty">No settled tickets in this period.</p>`;
    } catch (err) {
      renderError(dailyEl, err);
    }
  }
  daysEl.addEventListener("change", loadDaily);
  await loadDaily();
}
