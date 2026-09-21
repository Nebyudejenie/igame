import { api, escapeHtml, fmtDate } from "../api.js";
import { renderError, toast } from "../ui.js";

export const label = "Announcement";

export async function render(container) {
  container.innerHTML = `
    <h1>Announcement</h1>
    <p class="field-hint">
      A single scrolling banner shown to every real player in the Mini App -- currently on
      the spectate screen (a room's own live view, once a player has no card yet). Never
      shown to a simulated player's own view, since bots have no WebApp of their own.
    </p>
    <div id="announcement-panel"><p class="loading">Loading…</p></div>
  `;

  const panel = container.querySelector("#announcement-panel");

  async function reload() {
    panel.innerHTML = `<p class="loading">Loading…</p>`;
    try {
      const data = await api("/announcement");
      renderForm(data);
    } catch (err) {
      renderError(panel, err);
    }
  }

  function renderForm(data) {
    panel.innerHTML = `
      <p>
        Currently <strong>${data.enabled ? "ENABLED" : "DISABLED"}</strong>
        (last changed ${fmtDate(data.updated_at)}).
      </p>
      <form id="announcement-form" class="detail-panel">
        <label>Text (max 280 characters)
          <input type="text" name="text" maxlength="280" value="${escapeHtml(data.text)}"
                 placeholder="e.g. Deposit bonus this week -- ask support for details!" />
        </label>
        <div class="action-row">
          <label style="flex-direction:row; align-items:center; gap:0.35rem;">
            <input type="checkbox" name="enabled" ${data.enabled ? "checked" : ""} /> Enabled
          </label>
        </div>
        <label>Reason (required)
          <input type="text" name="reason" required placeholder="e.g. launch week promo" />
        </label>
        <div class="action-row">
          <button type="submit" class="btn">Save</button>
        </div>
      </form>
    `;
    panel.querySelector("#announcement-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = new FormData(event.target);
      const text = String(form.get("text") || "").trim();
      const enabled = form.get("enabled") === "on";
      const reason = String(form.get("reason") || "").trim();
      if (!reason) {
        toast("A reason is required.", true);
        return;
      }
      if (enabled && !text) {
        toast("Cannot enable an empty announcement.", true);
        return;
      }
      try {
        await api("/announcement", { method: "PATCH", body: { text, enabled, reason } });
        toast("Announcement updated.");
        reload();
      } catch (err) {
        toast(err.detail || err.message, true);
      }
    });
  }

  await reload();
}
