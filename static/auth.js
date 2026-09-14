"use strict";

// Auth interactions shared by the login/register page and the logout buttons
// on the board/history pages.

async function postJSON(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  let data = {};
  try { data = await res.json(); } catch (e) { /* ignore */ }
  return { status: res.status, ok: res.ok, data };
}

// --- login / register form ---
const authForm = document.getElementById("authForm");
if (authForm) {
  const mode = authForm.dataset.mode; // "login" | "register"
  const errEl = document.getElementById("authError");
  const submit = document.getElementById("authSubmit");

  authForm.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    if (errEl) errEl.textContent = "";
    const username = (document.getElementById("username") || {}).value || "";
    const password = (document.getElementById("password") || {}).value || "";
    if (submit) submit.disabled = true;
    try {
      const url = mode === "register" ? "/api/register" : "/api/login";
      const body = { username, password };
      if (mode === "register") {
        // Optional FIDE ID: seeds the player's starting FIDE ratings server-side.
        const fideId = (document.getElementById("fideId") || {}).value || "";
        if (fideId.trim()) body.fide_id = fideId.trim();
      }
      const { ok, data } = await postJSON(url, body);
      if (ok) {
        // Signed in: go to the board.
        window.location.href = "/";
      } else if (errEl) {
        errEl.textContent = (data && data.error) || "Something went wrong.";
      }
    } catch (e) {
      if (errEl) errEl.textContent = "Network error. Please try again.";
    } finally {
      if (submit) submit.disabled = false;
    }
  });
}

// --- logout buttons (present on board + history pages) ---
const logoutBtn = document.getElementById("logoutBtn");
if (logoutBtn) {
  logoutBtn.addEventListener("click", async () => {
    try { await postJSON("/api/logout", {}); } catch (e) { /* ignore */ }
    window.location.href = "/";
  });
}
