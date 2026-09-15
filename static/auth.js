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

// Real-window popup for the Supabase configured-but-failed signup case.
// The primary path is a REAL browser window (window.open). All styling uses the
// strict 8-color palette (each RGB channel exactly 0 or 255; no rgba/opacity/
// gradient/hsl/greys). auth.js loads standalone on the login page, so this
// POPUP_STYLE const is defined locally (NOT imported from app.js).
// Palette: black #000000, white #ffffff, red #ff0000, green #00ff00,
// blue #0000ff, yellow #ffff00, cyan #00ffff, magenta #ff00ff.
const POPUP_STYLE = [
  "html,body{margin:0;padding:0;background:#000000;color:#ffffff;",
  "font-family:monospace;font-size:16px;}",
  "body{padding:16px;}",
  "h1{font-size:18px;margin:0 0 12px;color:#ffff00;}",
  "p{margin:0 0 12px;line-height:1.4;}",
  ".ca-ok{color:#00ff00;}",
  ".ca-err{color:#ff0000;}",
  "button{font-family:monospace;font-size:16px;color:#ffffff;background:#000000;",
  "border:2px solid #ffffff;padding:6px 14px;cursor:pointer;margin:0 8px 0 0;}",
  "button:hover{color:#000000;background:#ffffff;}"
].join("");

// Exact wording accepted by the user for the fallback popup body.
const SUPABASE_FALLBACK_MESSAGE =
  "Supabase sign-up failed; your account was created with local (bcrypt) " +
  "fallback and is not linked to email. Password reset by email won't be " +
  "available for this account.";

// Open the REAL fallback window offering Retry / Continue. If window.open is
// blocked (returns null) or throws, proceed with the local account by
// navigating to '/' immediately (the account is created and the user is already
// logged in). When the window opens, the button handlers drive navigation so
// the two choices stay meaningful.
function showSupabaseFallbackWindow(password) {
  let win = null;
  try {
    win = window.open("", "caSupabaseFallback", "width=460,height=320");
  } catch (e) { win = null; }
  if (!win) {
    // Popup blocked / open threw: do not hang, just continue with local account.
    window.location.href = "/";
    return;
  }
  try {
    const doc = win.document;
    doc.open();
    doc.write(
      "<!doctype html><html><head><meta charset='utf-8'>" +
      "<title>Supabase sign-up failed</title><style>" + POPUP_STYLE + "</style></head>" +
      "<body><h1 class='ca-err'>Supabase sign-up failed</h1>" +
      "<p id='msg'></p>" +
      "<button type='button' id='retry'>Retry with Supabase</button>" +
      "<button type='button' id='continue'>Continue with local account</button>" +
      "<p id='status'></p></body></html>"
    );
    doc.close();
    // textContent (not innerHTML) so the message is never interpreted as markup.
    const msgEl = doc.getElementById("msg");
    if (msgEl) msgEl.textContent = SUPABASE_FALLBACK_MESSAGE;
    const statusEl = doc.getElementById("status");
    const retryBtn = doc.getElementById("retry");
    const continueBtn = doc.getElementById("continue");
    if (continueBtn) {
      continueBtn.addEventListener("click", () => {
        try { win.close(); } catch (e) { /* ignore */ }
        window.location.href = "/";
      });
    }
    if (retryBtn) {
      retryBtn.addEventListener("click", async () => {
        retryBtn.disabled = true;
        try {
          if (statusEl) {
            statusEl.className = "";
            statusEl.textContent = "Retrying with Supabase\u2026";
          }
          const { ok, data } = await postJSON("/api/link-supabase", { password: password });
          if (ok && data && data.email_bound) {
            try {
              if (statusEl) {
                statusEl.className = "ca-ok";
                statusEl.textContent = "Linked to email. You can close this window.";
              }
            } catch (e) { /* window may be closing; ignore */ }
            window.location.href = "/";
          } else {
            try {
              if (statusEl) {
                statusEl.className = "ca-err";
                statusEl.textContent = (data && data.error) || "Supabase link failed.";
              }
              retryBtn.disabled = false;
            } catch (e) { /* ignore */ }
          }
        } catch (e) {
          try {
            if (statusEl) {
              statusEl.className = "ca-err";
              statusEl.textContent = "Network error. Please try again.";
            }
            retryBtn.disabled = false;
          } catch (e2) { /* ignore */ }
        }
      });
    }
  } catch (e) {
    // The window may be closed mid-flight; if we cannot build it, just continue.
    window.location.href = "/";
  }
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
        // Required real email: Supabase sends confirmation / password-reset
        // mail here. The server re-validates and returns a clear error.
        const email = (document.getElementById("email") || {}).value || "";
        body.email = email.trim();
        // Optional FIDE ID: seeds the player's starting FIDE ratings server-side.
        const fideId = (document.getElementById("fideId") || {}).value || "";
        if (fideId.trim()) body.fide_id = fideId.trim();
      }
      const { ok, data } = await postJSON(url, body);
      if (ok) {
        // Configured-but-failed Supabase signup: supabase was attempted but the
        // sign_up did not succeed (bcrypt local fallback used). Offer the real
        // window with Retry / Continue. Any other case (normal success, or
        // Supabase not configured, or login mode) behaves exactly as today.
        const showFailurePopup =
          mode === "register" &&
          data && data.supabase_attempted === true && data.supabase !== true;
        if (showFailurePopup) {
          // Capture the signup password (already in JS memory) before navigating.
          showSupabaseFallbackWindow(password);
        } else {
          // Signed in: go to the board.
          window.location.href = "/";
        }
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
