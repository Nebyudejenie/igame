import { api, setToken, setRole } from "../api.js";

export function render(container, onSuccess) {
  container.innerHTML = `
    <div class="login-card">
      <h1>Zemen Game SMS Control Plane</h1>
      <form id="login-form">
        <label>Username
          <input type="text" name="username" required autocomplete="username" autofocus />
        </label>
        <label>Password
          <input type="password" name="password" required autocomplete="current-password" />
        </label>
        <label>TOTP code
          <input type="text" name="totp_code" required inputmode="numeric" pattern="[0-9]*" autocomplete="one-time-code" />
        </label>
        <button type="submit">Log in</button>
        <p class="form-error" id="login-error" hidden></p>
      </form>
      <p style="margin-top:1rem;font-size:0.78rem;color:var(--text-dim)">
        Uses the same admin account as the Bingo console.
      </p>
    </div>
  `;

  const form = container.querySelector("#login-form");
  const errorEl = container.querySelector("#login-error");
  const submitBtn = form.querySelector("button[type=submit]");

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    errorEl.hidden = true;
    submitBtn.disabled = true;
    const data = new FormData(form);
    try {
      const result = await api("/auth/login", {
        method: "POST",
        body: {
          username: data.get("username"),
          password: data.get("password"),
          totp_code: data.get("totp_code"),
        },
      });
      setToken(result.token);
      setRole(result.role);
      onSuccess();
    } catch (err) {
      errorEl.textContent = err.detail || err.message || "Login failed";
      errorEl.hidden = false;
    } finally {
      submitBtn.disabled = false;
    }
  });
}
