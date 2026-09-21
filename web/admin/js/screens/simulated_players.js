import { api, escapeHtml, fmtDate } from "../api.js";
import { renderError, toast } from "../ui.js";

export const label = "Simulated Players";

const STRATEGIES = ["conservative", "normal", "active", "randomized"];
const SCHEDULE_MODES = ["always_on", "scheduled_window", "room_specific"];
const MAX_SIMULATED_PLAYERS = 200;

// Per-status action buttons -- same ACTIONS-by-status map shape this
// codebase's own SMS control plane (nodes.js) already uses. "reset" is
// offered from every non-disabled state too: an admin correcting a bot's
// balance shouldn't have to stop it first.
const ACTIONS = {
  disabled: [["start", "Start"]],
  idle: [["pause", "Pause"], ["stop", "Stop"], ["reset", "Reset"]],
  joining: [["pause", "Pause"], ["stop", "Stop"], ["reset", "Reset"]],
  playing: [["pause", "Pause"], ["stop", "Stop"], ["reset", "Reset"]],
  paused: [["start", "Resume"], ["stop", "Stop"], ["reset", "Reset"]],
};

const ACTION_VERBS = { start: "start", pause: "pause", stop: "stop", reset: "reset" };
const ACTION_PAST_TENSE = { start: "started", pause: "paused", stop: "stopped", reset: "reset" };

function todayIsoDate() {
  // Africa/Addis_Ababa is UTC+3 year-round (no DST) -- close enough for a
  // "which calendar day is this activity panel showing" label that the
  // backend's own on_date filter (AT TIME ZONE 'Africa/Addis_Ababa') is
  // the real source of truth for anyway.
  return new Date().toISOString().slice(0, 10);
}

export async function render(container) {
  container.innerHTML = `
    <h1>Simulated Players <span class="badge badge-simulated">SIMULATED</span></h1>
    <p class="field-hint">
      Admin-controlled bot accounts that join real rooms and play through the real game
      engine so a room doesn't feel empty during early launch. Bots stake and win house-funded
      virtual balance only -- they can never touch a real customer's money, and every report
      elsewhere in this console excludes them.
    </p>

    <div class="detail-panel" id="settings-panel"><p class="loading">Loading settings…</p></div>
    <div class="detail-panel" id="activity-panel"><p class="loading">Loading today's activity…</p></div>

    <div class="action-row">
      <h2 style="margin:0; flex:1">Roster</h2>
      <button type="button" class="btn btn-secondary btn-sm" id="refresh-btn">Refresh</button>
    </div>
    <div id="roster-list"><p class="loading">Loading…</p></div>
    <div id="strategy-panel"></div>

    <h2>Create bot</h2>
    <form id="create-bot-form" class="detail-panel">
      <div class="detail-grid">
        <label>Display name <input type="text" name="display_name" required placeholder="e.g. Hana Bekele" /></label>
        <label>Strategy
          <select name="strategy">
            ${STRATEGIES.map((s) => `<option value="${s}" ${s === "normal" ? "selected" : ""}>${s}</option>`).join("")}
          </select>
        </label>
      </div>
      <p class="field-hint" id="roster-count-hint"></p>
      <div class="action-row">
        <button type="submit" class="btn">Create bot</button>
      </div>
    </form>

    <div class="detail-panel" id="stop-all-panel"></div>
  `;

  const settingsPanel = container.querySelector("#settings-panel");
  const activityPanel = container.querySelector("#activity-panel");
  const rosterList = container.querySelector("#roster-list");
  const strategyPanel = container.querySelector("#strategy-panel");
  const createForm = container.querySelector("#create-bot-form");
  const rosterCountHint = container.querySelector("#roster-count-hint");
  const stopAllPanel = container.querySelector("#stop-all-panel");

  let botsById = new Map();

  async function reloadAll() {
    await Promise.all([reloadSettings(), reloadActivity(), reloadRoster()]);
  }

  async function reloadSettings() {
    settingsPanel.innerHTML = `<p class="loading">Loading settings…</p>`;
    try {
      const settings = await api("/simulated-players/settings");
      renderSettings(settings);
    } catch (err) {
      renderError(settingsPanel, err);
    }
  }

  function renderSettings(settings) {
    settingsPanel.innerHTML = `
      <h2 style="margin-top:0">Global control</h2>
      <p>
        Bot activity is currently
        <strong>${settings.enabled ? "ENABLED" : "DISABLED"}</strong>
        (last changed ${fmtDate(settings.updated_at)}).
        While disabled, the bot-runner process takes no action at all, regardless of any
        individual bot's own status.
      </p>
      <form id="settings-form">
        <div class="action-row">
          <label style="flex-direction:row; align-items:center; gap:0.35rem;">
            <input type="checkbox" name="enabled" ${settings.enabled ? "checked" : ""} /> Enabled
          </label>
          <label>Max concurrent bots
            <input type="number" name="max_concurrent_bots" min="0" max="${MAX_SIMULATED_PLAYERS}"
                   value="${settings.max_concurrent_bots}" required />
          </label>
        </div>
        <label>Reason (required)
          <input type="text" name="reason" required placeholder="e.g. launch week activity boost" />
        </label>
        <div class="action-row">
          <button type="submit" class="btn">Save settings</button>
        </div>
      </form>
    `;
    settingsPanel.querySelector("#settings-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const data = new FormData(event.target);
      try {
        await api("/simulated-players/settings", {
          method: "PATCH",
          body: {
            enabled: data.get("enabled") === "on",
            max_concurrent_bots: Number(data.get("max_concurrent_bots")),
            reason: data.get("reason"),
          },
        });
        toast("Settings updated.");
        reloadAll();
      } catch (err) {
        toast(err.detail || err.message, true);
      }
    });
    renderStopAllPanel(settings.enabled);
  }

  function renderStopAllPanel(enabled) {
    stopAllPanel.innerHTML = `
      <h2 style="margin-top:0">Emergency stop</h2>
      <p class="warning-text">
        Immediately disables every non-disabled bot and turns off the global switch above.
        Any in-flight round a bot is sitting in still finishes normally through the real
        engine (a bot leaving mid-round is handled exactly like a real player closing their
        session) -- this only stops bots from taking any further action.
      </p>
      <form id="stop-all-form">
        <label>Reason (required)
          <input type="text" name="reason" required placeholder="e.g. incident response" />
        </label>
        <label>Type STOP ALL to confirm
          <input type="text" name="confirmation" required placeholder="STOP ALL" autocomplete="off" />
        </label>
        <div class="action-row">
          <button type="submit" class="btn btn-danger" ${enabled ? "" : "disabled"}>
            STOP ALL SIMULATED PLAYERS
          </button>
        </div>
      </form>
    `;
    if (!enabled) return;
    stopAllPanel.querySelector("#stop-all-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const data = new FormData(event.target);
      const reason = String(data.get("reason") || "").trim();
      const confirmation = String(data.get("confirmation") || "").trim();
      if (!reason) {
        toast("A reason is required.", true);
        return;
      }
      if (confirmation.toUpperCase() !== "STOP ALL") {
        toast('Type "STOP ALL" exactly to confirm.', true);
        return;
      }
      try {
        const result = await api("/simulated-players/stop-all", {
          method: "POST",
          body: { reason, confirmation },
        });
        toast(`Stopped ${result.stopped_count} bot(s).`);
        reloadAll();
      } catch (err) {
        toast(err.detail || err.message, true);
      }
    });
  }

  async function reloadActivity() {
    activityPanel.innerHTML = `<p class="loading">Loading today's activity…</p>`;
    try {
      const activity = await api(`/simulated-players/daily-activity?on_date=${todayIsoDate()}`);
      activityPanel.innerHTML = `
        <h2 style="margin-top:0">Simulated activity today</h2>
        <div class="detail-grid">
          <div><div class="field-label">Bot stakes</div><div class="field-value">${activity.bot_stakes_today} ETB</div></div>
          <div><div class="field-label">Bot payouts</div><div class="field-value">${activity.bot_payouts_today} ETB</div></div>
          <div><div class="field-label">Rounds a bot played in</div><div class="field-value">${activity.bot_rounds_today}</div></div>
        </div>
        <p class="field-hint">
          Excluded from the main Dashboard and GGR reports entirely. Note this does not
          isolate a bot-only house revenue figure: a round a bot shares with real players
          has one house-cut credit covering every winner together, which can't be honestly
          split by account kind alone.
        </p>
      `;
    } catch (err) {
      renderError(activityPanel, err);
    }
  }

  async function reloadRoster() {
    rosterList.innerHTML = `<p class="loading">Loading…</p>`;
    try {
      const bots = await api("/simulated-players");
      renderRoster(bots);
    } catch (err) {
      renderError(rosterList, err);
    }
  }

  function renderRoster(bots) {
    botsById = new Map(bots.map((b) => [b.user_id, b]));
    rosterCountHint.textContent = `${bots.length}/${MAX_SIMULATED_PLAYERS} bots created.`;
    createForm.querySelector('button[type="submit"]').disabled = bots.length >= MAX_SIMULATED_PLAYERS;

    if (bots.length === 0) {
      rosterList.innerHTML = `<p class="empty">No simulated players created yet.</p>`;
      return;
    }
    rosterList.innerHTML = `
      <table class="data-table">
        <thead>
          <tr>
            <th>ID</th><th>Name</th><th>Status</th><th>Strategy</th><th>Join %</th>
            <th>Room</th><th>Balance</th><th>Games (W/L)</th><th>Last active</th><th></th>
          </tr>
        </thead>
        <tbody>
          ${bots.map((b) => `
            <tr data-user-id="${b.user_id}">
              <td>${b.user_id}</td>
              <td>${escapeHtml(b.display_name)} <span class="badge badge-simulated">SIM</span></td>
              <td><span class="badge status-badge badge-${escapeHtml(b.status)}">${escapeHtml(b.status)}</span></td>
              <td>${escapeHtml(b.strategy)}</td>
              <td>${b.join_probability_pct}%</td>
              <td>${b.current_room_id ?? "—"}</td>
              <td>${b.balance} ETB</td>
              <td>${b.games_won} / ${b.games_lost}</td>
              <td>${fmtDate(b.last_activity_at)}</td>
              <td>
                ${(ACTIONS[b.status] || []).map(([action, text]) => `
                  <button class="btn btn-secondary btn-sm bot-action-btn" data-action="${action}">${text}</button>
                `).join("")}
                <button class="btn btn-secondary btn-sm configure-btn">Configure</button>
              </td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    `;
    for (const row of rosterList.querySelectorAll("tr[data-user-id]")) {
      const userId = Number(row.dataset.userId);
      row.querySelector(".configure-btn").addEventListener("click", () => openStrategyPanel(userId));
      for (const btn of row.querySelectorAll(".bot-action-btn")) {
        btn.addEventListener("click", () => runBotAction(userId, btn.dataset.action));
      }
    }
  }

  async function runBotAction(userId, action) {
    const verb = ACTION_VERBS[action] || action;
    const suffix = action === "reset" ? "" : " (optional)";
    const raw = window.prompt(`Reason to ${verb} bot #${userId}${suffix}:`);
    if (raw === null) return; // cancelled -- abort the action entirely, same as rooms.js's own convention
    const reason = raw.trim();
    if (action === "reset" && !reason) {
      toast("Reset requires a reason.", true);
      return;
    }
    try {
      await api(`/simulated-players/${userId}/${action}`, { method: "POST", body: { reason: reason || null } });
      toast(`Bot #${userId} ${ACTION_PAST_TENSE[action] || action}.`);
      reloadAll();
    } catch (err) {
      toast(err.detail || err.message, true);
    }
  }

  function openStrategyPanel(userId) {
    const bot = botsById.get(userId);
    strategyPanel.innerHTML = `
      <form id="strategy-form" class="detail-panel">
        <h2 style="margin-top:0">Configure bot #${userId} (${escapeHtml(bot.display_name)})</h2>
        <div class="detail-grid">
          <label>Strategy
            <select name="strategy">
              ${STRATEGIES.map((s) => `<option value="${s}" ${s === bot.strategy ? "selected" : ""}>${s}</option>`).join("")}
            </select>
          </label>
          <label>Join probability (%)
            <input type="number" name="join_probability_pct" min="0" max="100" value="${bot.join_probability_pct}" required />
          </label>
          <label>Max cards per join
            <input type="number" name="max_cards_per_join" min="1" max="4" value="${bot.max_cards_per_join}" required />
          </label>
          <label>Schedule mode
            <select name="schedule_mode">
              ${SCHEDULE_MODES.map((m) => `<option value="${m}" ${m === bot.schedule_mode ? "selected" : ""}>${m}</option>`).join("")}
            </select>
          </label>
          <label>Window start (minute of day, Africa/Addis_Ababa)
            <input type="number" name="schedule_window_start_minute" min="0" max="1439"
                   value="${bot.schedule_window_start_minute ?? ""}" />
          </label>
          <label>Window end (minute of day)
            <input type="number" name="schedule_window_end_minute" min="0" max="1439"
                   value="${bot.schedule_window_end_minute ?? ""}" />
          </label>
          <label>Pinned room id
            <input type="number" name="pinned_room_id" value="${bot.pinned_room_id ?? ""}" />
          </label>
        </div>
        <label>Reason (required)
          <input type="text" name="reason" required placeholder="e.g. tuning activity for launch week" />
        </label>
        <div class="action-row">
          <button type="submit" class="btn">Save configuration</button>
          <button type="button" class="btn btn-secondary" id="cancel-strategy-btn">Cancel</button>
        </div>
      </form>
    `;
    strategyPanel.querySelector("#cancel-strategy-btn").addEventListener("click", () => {
      strategyPanel.innerHTML = "";
    });
    strategyPanel.querySelector("#strategy-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const data = new FormData(event.target);
      const toIntOrNull = (v) => (v === "" || v === null ? null : Number(v));
      try {
        await api(`/simulated-players/${userId}/strategy`, {
          method: "PATCH",
          body: {
            strategy: data.get("strategy"),
            join_probability_pct: Number(data.get("join_probability_pct")),
            max_cards_per_join: Number(data.get("max_cards_per_join")),
            schedule_mode: data.get("schedule_mode"),
            schedule_window_start_minute: toIntOrNull(data.get("schedule_window_start_minute")),
            schedule_window_end_minute: toIntOrNull(data.get("schedule_window_end_minute")),
            pinned_room_id: toIntOrNull(data.get("pinned_room_id")),
            reason: data.get("reason"),
          },
        });
        toast("Bot configuration updated.");
        strategyPanel.innerHTML = "";
        reloadRoster();
      } catch (err) {
        toast(err.detail || err.message, true);
      }
    });
  }

  createForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(createForm);
    try {
      const result = await api("/simulated-players", {
        method: "POST",
        body: { display_name: data.get("display_name"), strategy: data.get("strategy") },
      });
      toast(`Bot #${result.user_id} created with a 5,000 ETB house-funded balance.`);
      createForm.reset();
      reloadAll();
    } catch (err) {
      toast(err.detail || err.message, true);
    }
  });

  container.querySelector("#refresh-btn").addEventListener("click", reloadAll);

  await reloadAll();
}
