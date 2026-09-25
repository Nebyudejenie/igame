import { api, escapeHtml, fmtDate } from "../../api.js";
import { renderError } from "../../ui.js";

const STATUSES = ["", "betting_open", "betting_closed", "drawing", "draw_complete", "settling", "completed", "failed", "voided"];
const STATUS_BADGE = { completed: "completed", failed: "failed", voided: "cancelled", settling: "review" };
const PAGE = 50;

function badge(status) {
  return `<span class="badge badge-${STATUS_BADGE[status] || "scheduled"}">${escapeHtml(status)}</span>`;
}

function chips(numbers) {
  if (!numbers || numbers.length === 0) return `<span class="empty">—</span>`;
  return `<div class="number-chips">${numbers.map((n) => `<span class="number-chip">${n}</span>`).join("")}</div>`;
}

export async function render(container) {
  container.innerHTML = `
    <div class="inline-form" style="margin-bottom:0.75rem">
      <label>Status
        <select id="round-status">${STATUSES.map((s) => `<option value="${s}">${s || "any"}</option>`).join("")}</select>
      </label>
    </div>
    <div id="round-list"><p class="loading">Loading…</p></div>
    <div class="action-row"><button class="btn btn-secondary" id="round-more" hidden>Older rounds</button></div>
    <div id="round-detail"></div>
  `;
  const listEl = container.querySelector("#round-list");
  const moreBtn = container.querySelector("#round-more");
  const detailEl = container.querySelector("#round-detail");
  const statusEl = container.querySelector("#round-status");
  let rows = [];

  async function load(reset) {
    if (reset) rows = [];
    const params = new URLSearchParams({ limit: String(PAGE) });
    if (statusEl.value) params.set("status", statusEl.value);
    if (!reset && rows.length) params.set("before_id", String(rows[rows.length - 1].id));
    try {
      const page = await api(`/keno/rounds?${params}`);
      rows = rows.concat(page);
      moreBtn.hidden = page.length < PAGE;
      draw();
    } catch (err) {
      renderError(listEl, err);
    }
  }

  function draw() {
    if (rows.length === 0) {
      listEl.innerHTML = `<p class="empty">No rounds match.</p>`;
      return;
    }
    listEl.innerHTML = `
      <table class="data-table">
        <thead><tr><th>ID</th><th>Status</th><th>Tickets</th><th>Stake</th><th>Payout</th>
          <th>Jackpot</th><th>Scheduled</th><th>Completed</th></tr></thead>
        <tbody>${rows.map((r) => `
          <tr class="clickable-row" data-id="${r.id}">
            <td>#${r.id}</td><td>${badge(r.status)}</td><td>${r.ticket_count}</td>
            <td>${escapeHtml(r.total_stake)}</td><td>${escapeHtml(r.total_payout)}</td>
            <td>${r.jackpot_hit ? "Yes" : ""}</td><td>${fmtDate(r.scheduled_at)}</td><td>${fmtDate(r.completed_at)}</td>
          </tr>`).join("")}
        </tbody>
      </table>`;
    for (const tr of listEl.querySelectorAll("[data-id]")) {
      tr.addEventListener("click", () => showDetail(Number(tr.dataset.id)));
    }
  }

  async function showDetail(id) {
    detailEl.innerHTML = `<p class="loading">Loading round #${id}…</p>`;
    try {
      const { round, tickets, events } = await api(`/keno/rounds/${id}`);
      detailEl.innerHTML = `
        <h2>Round #${round.id} ${badge(round.status)}</h2>
        <div class="detail-panel">
          <div class="detail-grid">
            <div><div class="field-label">Stake</div><div class="field-value">${escapeHtml(round.total_stake)} ETB</div></div>
            <div><div class="field-label">Payout</div><div class="field-value">${escapeHtml(round.total_payout)} ETB</div></div>
            <div><div class="field-label">Seed hash</div><div class="field-value"><code>${escapeHtml(round.server_seed_hash)}</code></div></div>
            ${round.failure_reason ? `<div><div class="field-label">Failure</div><div class="field-value">${escapeHtml(round.failure_reason)}</div></div>` : ""}
          </div>
          <div class="field-label" style="margin-top:0.75rem">Drawn numbers ${round.drawn_numbers ? "" : "(hidden until the round is terminal)"}</div>
          ${chips(round.drawn_numbers)}
        </div>
        <h2>Tickets (${tickets.length})</h2>
        ${tickets.length ? `<table class="data-table">
          <thead><tr><th>ID</th><th>User</th><th>Picks</th><th>Stake</th><th>Status</th><th>Matches</th><th>Payout</th><th>Jackpot</th></tr></thead>
          <tbody>${tickets.map((t) => `<tr>
            <td>${t.id}</td><td>${t.user_id}</td><td>${chips((t.picks || []).filter((n) => n !== null))}</td>
            <td>${escapeHtml(t.stake)}</td><td>${escapeHtml(t.status)}</td><td>${t.matches ?? "—"}</td>
            <td>${t.payout ?? "—"}</td><td>${t.jackpot_payout ?? ""}</td></tr>`).join("")}
          </tbody></table>` : `<p class="empty">No tickets.</p>`}
        <h2>State transitions</h2>
        <table class="data-table">
          <thead><tr><th>When</th><th>From</th><th>To</th><th>Worker</th><th>Reason</th></tr></thead>
          <tbody>${events.map((ev) => `<tr><td>${fmtDate(ev.created_at)}</td><td>${escapeHtml(ev.from_status ?? "")}</td>
            <td>${escapeHtml(ev.to_status)}</td><td>${escapeHtml(ev.worker_id ?? "")}</td><td>${escapeHtml(ev.reason ?? "")}</td></tr>`).join("")}
          </tbody></table>`;
      detailEl.scrollIntoView({ behavior: "smooth" });
    } catch (err) {
      renderError(detailEl, err);
    }
  }

  statusEl.addEventListener("change", () => load(true));
  moreBtn.addEventListener("click", () => load(false));
  await load(true);
}
