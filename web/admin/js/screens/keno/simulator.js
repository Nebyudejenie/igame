import { api, escapeHtml } from "../../api.js";
import { renderError } from "../../ui.js";

function etb(value) {
  return Number(value).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function pct(value) {
  return `${(Number(value) * 100).toFixed(2)}%`;
}

export async function render(container) {
  let tables;
  let config;
  try {
    [tables, config] = await Promise.all([api("/keno/paytables"), api("/keno/configs/active")]);
  } catch (err) {
    renderError(container, err);
    return;
  }
  const diversion = config?.jackpot_diversion_bps ?? 150;
  // Newest row per (profile, pick_count) -- simulate the real paytable, not a
  // hand-typed one that could silently differ from what players get.
  const latest = new Map();
  for (const t of tables) {
    const key = `${t.profile}:${t.pick_count}`;
    if (!latest.has(key) || new Date(t.effective_from) > new Date(latest.get(key).effective_from)) latest.set(key, t);
  }
  const options = [...latest.values()].sort((a, b) => a.profile.localeCompare(b.profile) || a.pick_count - b.pick_count);

  container.innerHTML = `
    <p class="field-hint">Monte Carlo projection of the prize reserve. Nothing here is saved and nothing moves money.
      Traffic inputs are your assumptions -- the output is only as real as they are.</p>
    <form id="sim-form" class="detail-panel keno-form">
      <div class="detail-grid">
        <label>Paytable
          <select name="table">${options.map((t, i) => `<option value="${i}">${escapeHtml(t.profile)} · ${t.pick_count} picks
            (RTP ${(t.computed_rtp_bps / 100).toFixed(2)}%)</option>`).join("")}</select></label>
        <label>Starting reserve (ETB) <input type="text" name="starting_reserve" value="30000" required /></label>
        <label>Daily handle (ETB) <input type="text" name="daily_handle" value="5000" required /></label>
        <label>Average stake (ETB) <input type="text" name="avg_stake" value="50" required /></label>
        <label>Reserve floor (ETB) <input type="text" name="floor" value="0" required /></label>
        <label>Days <input type="number" name="days" value="90" min="1" max="3650" required /></label>
        <label>Simulations <input type="number" name="num_simulations" value="2000" min="1" max="20000" required /></label>
        <label>Jackpot diversion (bps) <input type="number" name="jackpot_diversion_bps" value="${diversion}" required /></label>
      </div>
      <div class="action-row"><button type="submit" class="btn">Run simulation</button></div>
    </form>
    <div id="sim-result"></div>
  `;
  const form = container.querySelector("#sim-form");
  const resultEl = container.querySelector("#sim-result");
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const table = options[Number(form.table.value)];
    if (!table) return;
    resultEl.innerHTML = `<p class="loading">Simulating…</p>`;
    try {
      const r = await api("/keno/risk-of-ruin", {
        method: "POST",
        body: {
          starting_reserve: form.starting_reserve.value.trim(),
          daily_handle: form.daily_handle.value.trim(),
          avg_stake: form.avg_stake.value.trim(),
          pick_count: table.pick_count,
          multipliers: table.multipliers,
          jackpot_diversion_bps: Number(form.jackpot_diversion_bps.value),
          floor: form.floor.value.trim(),
          days: Number(form.days.value),
          num_simulations: Number(form.num_simulations.value),
        },
      });
      resultEl.innerHTML = `
        <div class="stat-grid">
          <div class="stat-card${Number(r.floor_breach_probability) > 0 ? " stat-card-alert" : ""}">
            <div class="stat-label">Probability of breaching the floor</div>
            <div class="stat-value">${pct(r.floor_breach_probability)}</div></div>
          <div class="stat-card"><div class="stat-label">Ending reserve p10 / p50 / p90</div>
            <div class="stat-value">${etb(r.ending_reserve_p10)} / ${etb(r.ending_reserve_p50)} / ${etb(r.ending_reserve_p90)}</div></div>
          <div class="stat-card"><div class="stat-label">Worst drawdown (mean / max)</div>
            <div class="stat-value">${etb(r.worst_drawdown_mean)} / ${etb(r.worst_drawdown_max)}</div></div>
        </div>
        <p class="field-hint">${r.num_simulations} runs × ${r.days} days.</p>`;
    } catch (err) {
      renderError(resultEl, err);
    }
  });
}
