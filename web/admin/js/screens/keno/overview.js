import { api, escapeHtml } from "../../api.js";
import { renderError, toast } from "../../ui.js";

function money(value) {
  return value === null || value === undefined ? "—" : `${escapeHtml(value)} ETB`;
}

export async function render(container, { role }) {
  let data;
  let config;
  try {
    [data, config] = await Promise.all([api("/keno/dashboard"), api("/keno/configs/active")]);
  } catch (err) {
    renderError(container, err);
    return;
  }
  const round = data.current_round;
  const superadmin = role === "superadmin";

  container.innerHTML = `
    <div class="stat-grid">
      <div class="stat-card${data.keno_enabled ? "" : " stat-card-alert"}">
        <div class="stat-label">Keno</div>
        <div class="stat-value">${data.keno_enabled ? "Enabled" : "Disabled"}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Prize reserve</div>
        <div class="stat-value">${money(data.reserve_balance)}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Player liability (money owed, not reserve)</div>
        <div class="stat-value">${money(data.player_liability)}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Jackpot pool</div>
        <div class="stat-value">${money(data.jackpot_pool)}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Current tier</div>
        <div class="stat-value">${data.current_tier_number ?? "—"}</div>
      </div>
    </div>
    <p class="field-hint">Reserve and player liability are separate pots. The reserve pays Keno prizes; liability is
      players' own balances the platform owes back. Never read one as the other.</p>

    <h2>Live round</h2>
    ${round ? `
      <div class="detail-panel detail-grid">
        <div><div class="field-label">Round</div><div class="field-value">#${round.id}</div></div>
        <div><div class="field-label">Status</div><div class="field-value">${escapeHtml(round.status)}</div></div>
        <div><div class="field-label">Tickets</div><div class="field-value">${round.ticket_count}</div></div>
        <div><div class="field-label">Stake</div><div class="field-value">${money(round.total_stake)}</div></div>
        <div><div class="field-label">Projected exposure (net, 99.9th pct)</div>
          <div class="field-value">${money(round.projected_exposure)}</div></div>
      </div>` : `<p class="empty">No round in progress.</p>`}

    <h2>Active config</h2>
    ${config ? `
      <div class="detail-panel detail-grid">
        <div><div class="field-label">Version</div><div class="field-value">${config.version}</div></div>
        <div><div class="field-label">Beta allowlist enforced</div>
          <div class="field-value">${config.beta_restricted ? "Yes" : "No"}</div></div>
        <div><div class="field-label">Round cycle</div><div class="field-value">${config.round_cycle_seconds}s</div></div>
        <div><div class="field-label">Picks</div><div class="field-value">${config.min_picks}–${config.max_picks}</div></div>
        <div><div class="field-label">Jackpot diversion</div>
          <div class="field-value">${(config.jackpot_diversion_bps / 100).toFixed(2)}%</div></div>
        <div><div class="field-label">Reserve withdrawal floor</div>
          <div class="field-value">${money(config.reserve_withdrawal_floor)}</div></div>
      </div>` : `<p class="empty">Keno has never been configured.</p>`}

    ${superadmin ? `
      <h2>Kill switch</h2>
      <form id="kill-switch-form" class="detail-panel inline-form">
        <label>Reason <input type="text" name="reason" required minlength="10" /></label>
        <button type="submit" class="btn ${data.keno_enabled ? "btn-danger" : "btn-success"}">
          ${data.keno_enabled ? "Disable Keno now" : "Enable Keno"}
        </button>
      </form>
      <p class="field-hint">Blocks new tickets immediately. A round already in progress still finishes and settles.</p>

      <h2>Prize reserve</h2>
      <form id="reserve-form" class="detail-panel inline-form">
        <label>Amount (ETB) <input type="text" name="amount" required inputmode="decimal" /></label>
        <label>Reason <input type="text" name="reason" required minlength="10" /></label>
        <button type="submit" class="btn" data-direction="deposit">Deposit</button>
        <button type="submit" class="btn btn-secondary" data-direction="withdraw">Withdraw</button>
      </form>
      <p class="field-hint">Moves real money between house float and the prize reserve. Every transfer is audited.
        Withdrawals below the configured floor are refused.</p>
    ` : ""}
  `;

  if (!superadmin) return;

  container.querySelector("#kill-switch-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const enabled = !data.keno_enabled;
    const reason = e.target.reason.value.trim();
    if (!confirm(`${enabled ? "ENABLE" : "DISABLE"} Keno for players now?\n\nReason: ${reason}`)) return;
    try {
      await api("/keno/kill-switch", { method: "POST", body: { enabled, reason } });
      toast(`Keno ${enabled ? "enabled" : "disabled"}`);
      await render(container, { role });
    } catch (err) {
      toast(err.detail || err.message, true);
    }
  });

  const reserveForm = container.querySelector("#reserve-form");
  reserveForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const direction = e.submitter?.dataset.direction;
    const amount = reserveForm.amount.value.trim();
    const reason = reserveForm.reason.value.trim();
    const verb = direction === "withdraw" ? "WITHDRAW" : "DEPOSIT";
    const flow = direction === "withdraw" ? "prize reserve → house float" : "house float → prize reserve";
    if (!confirm(`${verb} ${amount} ETB\n(${flow})\n\nReason: ${reason}\n\nThis moves real money.`)) return;
    try {
      const result = await api(`/keno/reserve/${direction}`, { method: "POST", body: { amount, reason } });
      toast(`Reserve now ${result.balance} ETB`);
      await render(container, { role });
    } catch (err) {
      toast(err.detail || err.message, true);
    }
  });
}
