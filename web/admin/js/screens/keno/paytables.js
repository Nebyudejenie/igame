import { api, escapeHtml, fmtDate } from "../../api.js";
import { renderError, toast } from "../../ui.js";

const PROFILES = ["low_variance", "standard"];

function pct(value) {
  return `${(Number(value) * 100).toFixed(2)}%`;
}

// Latest effective row per (pick_count, profile) -- the same "newest
// effective_from wins" rule the engine applies (packages/core/keno_config.py).
function activeTables(rows) {
  const now = Date.now();
  const best = new Map();
  for (const row of rows) {
    if (new Date(row.effective_from).getTime() > now) continue;
    const key = `${row.pick_count}:${row.profile}`;
    const current = best.get(key);
    if (!current || new Date(row.effective_from) > new Date(current.effective_from)) best.set(key, row);
  }
  return [...best.values()].sort((a, b) => a.profile.localeCompare(b.profile) || a.pick_count - b.pick_count);
}

export async function render(container, { role }) {
  let rows;
  try {
    rows = await api("/keno/paytables");
  } catch (err) {
    renderError(container, err);
    return;
  }
  const active = activeTables(rows);
  const canPreview = role === "ops" || role === "superadmin";
  const canSave = role === "superadmin";

  container.innerHTML = `
    <h2>Active paytables</h2>
    <table class="data-table">
      <thead><tr><th>Profile</th><th>Picks</th><th>Multipliers (matches → ×stake)</th><th>RTP</th><th>Hit freq</th>
        <th>Top</th><th>Effective</th></tr></thead>
      <tbody>${active.map((t) => `<tr>
        <td>${escapeHtml(t.profile)}</td><td>${t.pick_count}</td>
        <td>${Object.entries(t.multipliers).map(([k, v]) => `${escapeHtml(k)}→${escapeHtml(v)}`).join(", ")}</td>
        <td>${(t.computed_rtp_bps / 100).toFixed(2)}%</td><td>${(t.hit_frequency_bps / 100).toFixed(2)}%</td>
        <td>${escapeHtml(t.max_multiplier)}×</td><td>${fmtDate(t.effective_from)}</td></tr>`).join("")}
      </tbody>
    </table>

    ${canPreview ? `
      <h2>Paytable editor</h2>
      <form id="paytable-form" class="detail-panel keno-form">
        <div class="inline-form">
          <label>Profile <select name="profile">${PROFILES.map((p) => `<option>${p}</option>`).join("")}</select></label>
          <label>Picks <select name="pick_count">${[...Array(10)].map((_, i) => `<option>${i + 1}</option>`).join("")}</select></label>
        </div>
        <div id="multiplier-inputs" class="detail-grid" style="margin-top:0.75rem"></div>
        <p id="preview" class="winning-condition-preview"></p>
        ${canSave ? `
          <div class="inline-form">
            <label>Reason <input type="text" name="reason" minlength="10" /></label>
            <button type="submit" class="btn" id="save-paytable" disabled>Save new version</button>
          </div>
          <p class="field-hint">Saving creates a new version effective from the next round. In-flight rounds keep the
            paytable they were priced against.</p>` : `<p class="field-hint">Saving requires superadmin.</p>`}
      </form>` : ""}
  `;
  if (!canPreview) return;

  const form = container.querySelector("#paytable-form");
  const inputsEl = form.querySelector("#multiplier-inputs");
  const previewEl = form.querySelector("#preview");
  const saveBtn = form.querySelector("#save-paytable");
  let lastPreview = null;
  let timer = null;
  let seq = 0;

  function multipliers() {
    const out = {};
    for (const input of inputsEl.querySelectorAll("input")) {
      const value = input.value.trim();
      if (value !== "" && Number(value) !== 0) out[input.name] = value;
    }
    return out;
  }

  function buildInputs() {
    const pickCount = Number(form.pick_count.value);
    const existing = active.find((t) => t.pick_count === pickCount && t.profile === form.profile.value);
    const values = existing ? existing.multipliers : {};
    inputsEl.innerHTML = [...Array(pickCount + 1)].map((_, matches) => `
      <label>${matches} match${matches === 1 ? "" : "es"}
        <input type="text" name="${matches}" inputmode="decimal" value="${escapeHtml(values[String(matches)] ?? "")}" />
      </label>`).join("");
    for (const input of inputsEl.querySelectorAll("input")) input.addEventListener("input", schedulePreview);
    schedulePreview();
  }

  function schedulePreview() {
    if (saveBtn) saveBtn.disabled = true;
    clearTimeout(timer);
    timer = setTimeout(runPreview, 250);
  }

  async function runPreview() {
    const mine = ++seq;
    const body = { pick_count: Number(form.pick_count.value), multipliers: multipliers() };
    try {
      const result = await api("/keno/paytables/preview", { method: "POST", body });
      if (mine !== seq) return; // a newer keystroke's preview is already in flight
      lastPreview = result;
      previewEl.innerHTML = `
        RTP <span class="${result.within_guardrail ? "guardrail-ok" : "guardrail-bad"}">${pct(result.rtp)}</span>
        (allowed ${pct(result.rtp_floor)}–${pct(result.rtp_ceiling)}) · hit frequency ${pct(result.hit_frequency)}
        · top ${escapeHtml(result.max_multiplier)}× · volatility ${Number(result.volatility).toFixed(3)}
        ${result.within_guardrail ? "" : `<br><strong class="guardrail-bad">Outside the RTP guardrail -- the server will refuse to save this.</strong>`}`;
      if (saveBtn) saveBtn.disabled = !result.within_guardrail;
    } catch (err) {
      if (mine !== seq) return;
      lastPreview = null;
      previewEl.innerHTML = `<span class="guardrail-bad">${escapeHtml(err.detail || err.message)}</span>`;
    }
  }

  form.profile.addEventListener("change", buildInputs);
  form.pick_count.addEventListener("change", buildInputs);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!lastPreview?.within_guardrail) return;
    const body = {
      pick_count: Number(form.pick_count.value),
      profile: form.profile.value,
      multipliers: multipliers(),
      reason: form.reason.value.trim(),
    };
    if (!confirm(`Save a new ${body.profile} paytable for ${body.pick_count} picks at RTP ${pct(lastPreview.rtp)}?`)) return;
    try {
      await api("/keno/paytables", { method: "POST", body });
      toast("Paytable saved");
      await render(container, { role });
    } catch (err) {
      toast(err.detail || err.message, true);
    }
  });
  buildInputs();
}
