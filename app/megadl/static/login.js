"use strict";

const form = document.getElementById("signin-form");
const errorEl = document.getElementById("signin-error");
const submit = document.getElementById("signin-submit");

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  errorEl.hidden = true;
  submit.disabled = true;
  submit.textContent = "Signing in…";
  try {
    // Sent as JSON once; afterwards the browser only sends a session cookie.
    const res = await fetch("/api/session", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-MegaDL": "1" },
      body: JSON.stringify({
        user: document.getElementById("user").value.trim(),
        secret: document.getElementById("secret").value,
        remember: document.getElementById("remember").checked,
      }),
    });
    if (res.ok) {
      location.replace("/");
      return;
    }
    let message = "Sign in failed. Try again.";
    try { message = (await res.json()).error || message; } catch (_) { /* not JSON */ }
    errorEl.textContent = message;
    errorEl.hidden = false;
    document.getElementById("secret").select();
  } catch (_) {
    errorEl.textContent = "Can't reach MegaDL. Check that the container is running.";
    errorEl.hidden = false;
  } finally {
    submit.disabled = false;
    submit.textContent = "Sign in";
  }
});
