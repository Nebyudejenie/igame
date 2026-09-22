import { api, escapeHtml, fmtDate } from "../api.js";
import { renderError, toast } from "../ui.js";

export const label = "Bonuses & Referrals";

const TRIGGER_TYPES = ["referral_reward", "welcome_bonus", "deposit_match", "manual_grant"];

export async function render(container) {
  container.innerHTML = `
    <h1>Bonuses &amp; referrals</h1>

    <h2>Referral funnel</h2>
    <div id="funnel-result"><p class="loading">Loading…</p></div>

    <h2>Bonus rules</h2>
    <p class="wallet-note">
      Every reward amount, percentage, minimum deposit, and wagering multiplier below is set by you --
      nothing here is hardcoded. A referral reward pays the <strong>referrer</strong> once their invitee makes
      a qualifying deposit; the bonus is non-withdrawable until the recipient wagers real cash equal to the
      wagering multiplier times the bonus amount, at which point it converts automatically.
    </p>
    <div id="rules-list"><p class="loading">Loading…</p></div>
    <form id="create-rule-form" class="detail-panel">
      <div class="detail-grid">
        <label>Name <input type="text" name="name" required /></label>
        <label>Trigger
          <select name="trigger_type">
            ${TRIGGER_TYPES.map((t) => `<option value="${t}">${t.replace("_", " ")}</option>`).join("")}
          </select>
        </label>
        <label>Reward type
          <select name="reward_type">
            <option value="flat">Flat amount</option>
            <option value="percentage">Percentage of deposit</option>
          </select>
        </label>
        <label>Amount (ETB, if flat) <input type="number" step="0.01" name="reward_amount" placeholder="e.g. 10.00" /></label>
        <label>Percentage (if percentage) <input type="number" step="0.01" name="reward_percentage" placeholder="e.g. 10" /></label>
        <label>Cap (ETB, optional) <input type="number" step="0.01" name="reward_cap" /></label>
        <label>Min qualifying deposit (ETB) <input type="number" step="0.01" name="min_qualifying_deposit" value="0" /></label>
        <label>Wagering multiplier <input type="number" step="0.1" name="wagering_multiplier" value="3" /></label>
        <label>Expires after (days, optional) <input type="number" name="expiry_days" /></label>
        <label>Max grants per user <input type="number" name="max_grants_per_user" value="1" /></label>
      </div>
      <div class="action-row">
        <button type="submit" class="btn">Create rule</button>
      </div>
    </form>

    <h2>Grants</h2>
    <form id="grant-filter-form" class="inline-form">
      <input type="number" id="grant-user-id-input" placeholder="Filter by user ID (optional)" />
      <select id="grant-status-select">
        <option value="">Any status</option>
        <option value="active">Active</option>
        <option value="converted">Converted</option>
        <option value="expired">Expired</option>
        <option value="revoked">Revoked</option>
      </select>
      <button type="submit" class="btn">Filter</button>
    </form>
    <div id="grants-list"><p class="loading">Loading…</p></div>

    <h2>Grant a manual bonus</h2>
    <form id="manual-grant-form" class="detail-panel">
      <div class="detail-grid">
        <label>User ID <input type="number" name="user_id" required /></label>
        <label>Amount (ETB) <input type="number" step="0.01" name="amount" required /></label>
        <label>Wagering multiplier <input type="number" step="0.1" name="wagering_multiplier" value="3" /></label>
        <label>Expires after (days, optional) <input type="number" name="expiry_days" /></label>
      </div>
      <label>Reason <input type="text" name="reason" required /></label>
      <div class="action-row">
        <button type="submit" class="btn btn-secondary">Grant bonus</button>
      </div>
    </form>

    <h2>Referral fraud signals</h2>
    <div id="fraud-result"><p class="loading">Loading…</p></div>
  `;

  const funnelEl = container.querySelector("#funnel-result");
  const rulesListEl = container.querySelector("#rules-list");
  const createRuleForm = container.querySelector("#create-rule-form");
  const grantFilterForm = container.querySelector("#grant-filter-form");
  const grantUserIdInput = container.querySelector("#grant-user-id-input");
  const grantStatusSelect = container.querySelector("#grant-status-select");
  const grantsListEl = container.querySelector("#grants-list");
  const manualGrantForm = container.querySelector("#manual-grant-form");
  const fraudEl = container.querySelector("#fraud-result");

  async function loadFunnel() {
    funnelEl.innerHTML = `<p class="loading">Loading…</p>`;
    try {
      const data = await api("/bonuses/referral-funnel");
      funnelEl.innerHTML = `
        <div class="stat-grid">
          <div class="stat-card"><div class="stat-label">Registered via referral</div><div class="stat-value">${data.registered_via_referral}</div></div>
          <div class="stat-card"><div class="stat-label">Referees who deposited</div><div class="stat-value">${data.referees_who_deposited}</div></div>
          <div class="stat-card"><div class="stat-label">Referrals rewarded</div><div class="stat-value">${data.referrals_rewarded}</div></div>
          <div class="stat-card"><div class="stat-label">Outstanding bonus liability</div><div class="stat-value">${data.outstanding_bonus_liability} ETB</div></div>
        </div>
        ${data.top_referrers.length === 0 ? '<p class="empty">No referral rewards yet.</p>' : `
          <table class="data-table">
            <thead><tr><th>Referrer</th><th>Referrals rewarded</th><th>Total rewarded</th></tr></thead>
            <tbody>
              ${data.top_referrers.map((r) => `
                <tr><td>${escapeHtml(r.display_name)} (#${r.user_id})</td><td>${r.referral_count}</td><td>${r.total_rewarded} ETB</td></tr>
              `).join("")}
            </tbody>
          </table>
        `}
      `;
    } catch (err) {
      renderError(funnelEl, err);
    }
  }

  async function loadRules() {
    rulesListEl.innerHTML = `<p class="loading">Loading…</p>`;
    try {
      const rules = await api("/bonus-rules");
      if (rules.length === 0) {
        rulesListEl.innerHTML = `<p class="empty">No bonus rules configured yet.</p>`;
        return;
      }
      rulesListEl.innerHTML = `
        <table class="data-table">
          <thead>
            <tr><th>Name</th><th>Trigger</th><th>Reward</th><th>Min deposit</th><th>Wagering</th><th>Active</th><th></th></tr>
          </thead>
          <tbody>
            ${rules.map((r) => `
              <tr data-rule-id="${r.id}">
                <td>${escapeHtml(r.name)}</td>
                <td>${r.trigger_type.replace("_", " ")}</td>
                <td>${r.reward_type === "flat" ? `${r.reward_amount} ETB flat` : `${r.reward_percentage}%${r.reward_cap ? ` (cap ${r.reward_cap} ETB)` : ""}`}</td>
                <td>${r.min_qualifying_deposit} ETB</td>
                <td>${r.wagering_multiplier}x</td>
                <td>${r.is_active ? "yes" : "no"}</td>
                <td>
                  <button class="btn btn-secondary btn-sm toggle-rule-btn">${r.is_active ? "Deactivate" : "Activate"}</button>
                  <button class="btn btn-secondary btn-sm announce-rule-btn">Announce</button>
                </td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      `;
      for (const row of rulesListEl.querySelectorAll("tr[data-rule-id]")) {
        const ruleId = Number(row.dataset.ruleId);
        const rule = rules.find((r) => r.id === ruleId);
        const isActive = row.querySelector(".toggle-rule-btn").textContent.trim() === "Deactivate";
        row.querySelector(".toggle-rule-btn").addEventListener("click", async () => {
          try {
            await api(`/bonus-rules/${ruleId}`, { method: "PATCH", body: { changes: { is_active: !isActive } } });
            toast("Rule updated.");
            loadRules();
          } catch (err) {
            toast(err.detail || err.message, true);
          }
        });
        row.querySelector(".announce-rule-btn").addEventListener("click", () => {
          const rewardText = rule.reward_type === "flat"
            ? `${rule.reward_amount} ETB`
            : `${rule.reward_percentage}%${rule.reward_cap ? ` (up to ${rule.reward_cap} ETB)` : ""}`;
          sessionStorage.setItem("draftCampaignSeed", JSON.stringify({
            internalName: `Announce: ${rule.name}`,
            title: rule.trigger_type === "referral_reward" ? "Refer a friend, earn a reward!" : "New bonus available!",
            body: rule.trigger_type === "referral_reward"
              ? `Invite a friend to Zemen Game and earn ${rewardText} once they make a qualifying deposit. Use your invite link from the Invite button in the bot menu.`
              : `A new bonus is live: ${rule.name}, worth ${rewardText}. Check your wallet for details.`,
          }));
          document.querySelector('.nav-btn[data-screen="notifications"]').click();
        });
      }
    } catch (err) {
      renderError(rulesListEl, err);
    }
  }

  createRuleForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(createRuleForm);
    const numOrNull = (name) => (data.get(name) ? Number(data.get(name)) : null);
    try {
      await api("/bonus-rules", {
        method: "POST",
        body: {
          name: data.get("name"),
          trigger_type: data.get("trigger_type"),
          reward_type: data.get("reward_type"),
          reward_amount: numOrNull("reward_amount"),
          reward_percentage: numOrNull("reward_percentage"),
          reward_cap: numOrNull("reward_cap"),
          min_qualifying_deposit: Number(data.get("min_qualifying_deposit") || 0),
          wagering_multiplier: Number(data.get("wagering_multiplier") || 3),
          expiry_days: numOrNull("expiry_days"),
          max_grants_per_user: Number(data.get("max_grants_per_user") || 1),
        },
      });
      toast("Rule created.");
      createRuleForm.reset();
      loadRules();
    } catch (err) {
      toast(err.detail || err.message, true);
    }
  });

  async function loadGrants() {
    grantsListEl.innerHTML = `<p class="loading">Loading…</p>`;
    try {
      const params = new URLSearchParams();
      if (grantUserIdInput.value.trim()) params.set("user_id", grantUserIdInput.value.trim());
      if (grantStatusSelect.value) params.set("status", grantStatusSelect.value);
      const grants = await api(`/bonuses?${params.toString()}`);
      if (grants.length === 0) {
        grantsListEl.innerHTML = `<p class="empty">No bonus grants match this filter.</p>`;
        return;
      }
      grantsListEl.innerHTML = `
        <table class="data-table">
          <thead>
            <tr><th>User</th><th>Rule</th><th>Amount</th><th>Wagering progress</th><th>Status</th><th>Created</th><th></th></tr>
          </thead>
          <tbody>
            ${grants.map((g) => {
              const pct = g.wagering_required > 0 ? Math.min(100, Math.round((g.wagering_progress / g.wagering_required) * 100)) : 100;
              return `
                <tr data-bonus-id="${g.id}">
                  <td>${escapeHtml(g.display_name)} (#${g.user_id})</td>
                  <td>${escapeHtml(g.rule_name || "manual")}</td>
                  <td>${g.amount} ETB</td>
                  <td>${g.wagering_progress} / ${g.wagering_required} ETB (${pct}%)</td>
                  <td><span class="badge badge-${g.status}">${g.status}</span></td>
                  <td>${fmtDate(g.created_at)}</td>
                  <td>${g.status === "active" ? '<button class="btn btn-danger btn-sm revoke-btn">Revoke</button>' : ""}</td>
                </tr>
              `;
            }).join("")}
          </tbody>
        </table>
      `;
      for (const row of grantsListEl.querySelectorAll("tr[data-bonus-id]")) {
        const revokeBtn = row.querySelector(".revoke-btn");
        if (!revokeBtn) continue;
        revokeBtn.addEventListener("click", async () => {
          const reason = window.prompt("Reason for revoking this bonus:");
          if (!reason) return;
          try {
            await api(`/bonuses/${row.dataset.bonusId}/revoke`, { method: "POST", body: { reason } });
            toast("Bonus revoked.");
            loadGrants();
          } catch (err) {
            toast(err.detail || err.message, true);
          }
        });
      }
    } catch (err) {
      renderError(grantsListEl, err);
    }
  }

  grantFilterForm.addEventListener("submit", (event) => {
    event.preventDefault();
    loadGrants();
  });

  manualGrantForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(manualGrantForm);
    try {
      await api("/bonuses/grant", {
        method: "POST",
        body: {
          user_id: Number(data.get("user_id")),
          amount: Number(data.get("amount")),
          wagering_multiplier: Number(data.get("wagering_multiplier") || 3),
          expiry_days: data.get("expiry_days") ? Number(data.get("expiry_days")) : null,
          reason: data.get("reason"),
        },
      });
      toast("Bonus granted.");
      manualGrantForm.reset();
      loadGrants();
    } catch (err) {
      toast(err.detail || err.message, true);
    }
  });

  async function loadFraud() {
    fraudEl.innerHTML = `<p class="loading">Loading…</p>`;
    try {
      const data = await api("/bonuses/fraud-candidates");
      const noSignals = data.shared_payout_account_pairs.length === 0 && data.burst_referrers_last_24h.length === 0;
      if (noSignals) {
        fraudEl.innerHTML = `<p class="empty">No referral fraud signals right now.</p>`;
        return;
      }
      fraudEl.innerHTML = `
        ${data.shared_payout_account_pairs.length > 0 ? `
          <h3>Referrer/referee sharing a payout account</h3>
          <table class="data-table">
            <thead><tr><th>Referrer</th><th>Referee</th><th>Shared account</th></tr></thead>
            <tbody>
              ${data.shared_payout_account_pairs.map((p) => `
                <tr>
                  <td>${escapeHtml(p.referrer_name)} (#${p.referrer_id})</td>
                  <td>${escapeHtml(p.referee_name)} (#${p.referee_id})</td>
                  <td>${escapeHtml(p.kind)}: ${escapeHtml(p.account_ref)}</td>
                </tr>
              `).join("")}
            </tbody>
          </table>
        ` : ""}
        ${data.burst_referrers_last_24h.length > 0 ? `
          <h3>Unusually high referral volume (last 24h)</h3>
          <table class="data-table">
            <thead><tr><th>Referrer</th><th>Referrals (24h)</th></tr></thead>
            <tbody>
              ${data.burst_referrers_last_24h.map((r) => `
                <tr><td>${escapeHtml(r.referrer_name)} (#${r.referrer_id})</td><td>${r.referrals_last_24h}</td></tr>
              `).join("")}
            </tbody>
          </table>
        ` : ""}
      `;
    } catch (err) {
      renderError(fraudEl, err);
    }
  }

  await Promise.all([loadFunnel(), loadRules(), loadGrants(), loadFraud()]);
}
