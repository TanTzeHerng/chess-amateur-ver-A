#!/usr/bin/env python3
"""Authentication helpers for Chess Amateur: password hashing + validation.

Kept deliberately small and framework-agnostic so it can be unit-tested
without Flask or a database:
  * hash_password / verify_password use bcrypt.
  * validate_username / validate_password enforce simple, sane rules and
    return a human-readable error message (or None if valid).
  * register_user / authenticate_user tie validation + hashing to a Store.

Security notes:
  * Passwords are never stored or logged in plaintext; only the bcrypt hash is
    persisted. bcrypt includes a per-password salt automatically.
  * verify_password is constant-time (bcrypt.checkpw) and tolerant of malformed
    stored hashes (returns False rather than raising).
"""
import logging
import os
import re

import bcrypt

_log = logging.getLogger(__name__)

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]+$")
USERNAME_MIN, USERNAME_MAX = 3, 32
EMAIL_MAX = 254  # RFC 5321 practical maximum for an email address.
PASSWORD_MIN, PASSWORD_MAX = 8, 200  # 200 keeps us well under bcrypt's 72-byte
                                     # limit note below; see _bcrypt_safe.


def validate_username(username):
    """Return None if valid, else an error string."""
    if not isinstance(username, str):
        return "Username is required."
    username = username.strip()
    if len(username) < USERNAME_MIN:
        return "Username must be at least %d characters." % USERNAME_MIN
    if len(username) > USERNAME_MAX:
        return "Username must be at most %d characters." % USERNAME_MAX
    if not USERNAME_RE.match(username):
        return "Username may contain only letters, digits, and underscores."
    return None


def validate_password(password):
    """Return None if valid, else an error string."""
    if not isinstance(password, str):
        return "Password is required."
    if len(password) < PASSWORD_MIN:
        return "Password must be at least %d characters." % PASSWORD_MIN
    if len(password) > PASSWORD_MAX:
        return "Password must be at most %d characters." % PASSWORD_MAX
    return None


def validate_email(email):
    """Return None if valid, else an error string.

    Signup collects a REAL email so Supabase confirmation / password-reset
    emails actually reach the user, so email is required. We keep the check
    deliberately simple and sane (not a full RFC parser): a non-empty address
    with exactly one '@', a non-empty local part, and a domain that contains a
    dot with non-empty labels on either side of it.
    """
    if not isinstance(email, str):
        return "Email is required."
    email = email.strip()
    if not email:
        return "Email is required."
    if len(email) > EMAIL_MAX:
        return "Email must be at most %d characters." % EMAIL_MAX
    if email.count("@") != 1:
        return "Enter a valid email address."
    local, _, domain = email.partition("@")
    if not local or not domain:
        return "Enter a valid email address."
    if "." not in domain:
        return "Enter a valid email address."
    # No empty domain labels (rejects 'a@.com', 'a@b.', 'a@b..c').
    if any(not label for label in domain.split(".")):
        return "Enter a valid email address."
    return None


def _bcrypt_safe(password):
    """bcrypt only uses the first 72 BYTES of input. Encode to utf-8 and, to
    avoid silently ignoring trailing chars for very long passwords, we cap
    length in validate_password. Returns the utf-8 bytes."""
    return password.encode("utf-8")


def hash_password(password):
    """Return a bcrypt hash (str) for the given plaintext password."""
    return bcrypt.hashpw(_bcrypt_safe(password), bcrypt.gensalt()).decode("ascii")


def verify_password(password, password_hash):
    """Return True iff `password` matches the stored bcrypt `password_hash`.

    Tolerant: returns False (never raises) on malformed/empty hashes.
    """
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(_bcrypt_safe(password),
                              password_hash.encode("ascii"))
    except (ValueError, TypeError):
        return False


# ==========================================================================
# Supabase Auth (identity + password-reset emails), config-gated
# ==========================================================================
#
# Supabase owns credential storage, login, signup, and password-reset emails
# ONLY when SUPABASE_URL and SUPABASE_ANON_KEY are configured. When they are
# unset (local tests, guest mode, or a deploy without Supabase) we fall back to
# the original bcrypt-local auth path below -- the two functions register_user
# / authenticate_user pick the path at call time. In BOTH paths the local
# users.username UNIQUE constraint remains the duplicate-username guard, and
# the local users row (linked via supabase_user_id) owns all per-user FKs.


def supabase_configured():
    """True iff the anon (client) Supabase credentials are set."""
    return bool(os.environ.get("SUPABASE_URL")
                and os.environ.get("SUPABASE_ANON_KEY"))


def supabase_admin_configured():
    """True iff the service-role key is set (needed for admin/delete calls)."""
    return bool(os.environ.get("SUPABASE_URL")
                and os.environ.get("SUPABASE_SERVICE_KEY"))


def _supabase_client(service=False):
    """Return a Supabase client (anon or service-role), or None if unconfigured
    / the client library is unavailable. Never raises."""
    url = os.environ.get("SUPABASE_URL")
    key = (os.environ.get("SUPABASE_SERVICE_KEY") if service
           else os.environ.get("SUPABASE_ANON_KEY"))
    if not url or not key:
        return None
    try:
        from supabase import create_client
        return create_client(url, key)
    except Exception as exc:  # pragma: no cover - library/network issue is non-fatal
        # Never take the site down: return None so callers fall back to the
        # guest/bcrypt path. But LOG the real reason (e.g. "Invalid API key")
        # so a future auth misconfiguration surfaces in the deploy logs instead
        # of silently degrading to "Accounts are unavailable right now."
        _log.error("Supabase client init failed (%s key): %s",
                   "service" if service else "anon", _short(exc))
        return None


def _derive_email(username, email):
    """Choose the email address to register with Supabase.

    The signup UI collects a username; Supabase Auth is email-based. If the
    caller passes an explicit email we use it, else we synthesize a stable
    placeholder from the username. Callers that want real password-reset emails
    should pass a real address.
    """
    email = (email or "").strip()
    if email:
        return email
    return "%s@users.chess-amateur.local" % username.lower()


def _register_supabase(store, username, password, email):
    """Signup path when Supabase is configured: create the Supabase auth user,
    then create + link the local users row. Returns (user_dict, error)."""
    client = _supabase_client()
    if client is None:  # pragma: no cover - only when misconfigured at runtime
        return None, "Accounts are unavailable right now."
    # Prefer the REAL email the user supplied so Supabase confirmation /
    # reset emails reach them; fall back to the synthesized placeholder only
    # if it is somehow absent (register_user requires it for real signups).
    reg_email = _derive_email(username, email)
    try:
        res = client.auth.sign_up({"email": reg_email,
                                   "password": password})
    except Exception as exc:  # pragma: no cover - network/dupe-email errors
        return None, "Could not create account: %s" % _short(exc)
    sb_user = getattr(res, "user", None)
    sb_uid = getattr(sb_user, "id", None) if sb_user is not None else None
    # Create the linked LOCAL row. We still store a bcrypt hash so the local
    # row is self-consistent, but Supabase is the credential authority here.
    ph = hash_password(password)
    uid = store.create_user(username, ph, email=reg_email)
    if uid is None:
        # Local username race/collision: treat as taken (Supabase user may have
        # been created; that is acceptable and can be reclaimed on retry).
        return None, "That username is already taken."
    if sb_uid and hasattr(store, "set_supabase_id"):
        store.set_supabase_id(uid, sb_uid)
    return {"id": uid, "username": username}, None


def _authenticate_supabase(store, username, password):
    """Login path when Supabase is configured: authenticate via Supabase, then
    resolve the local users row. Returns (user_dict, error)."""
    generic = "Incorrect username or password."
    # Resolve the local row first so we know which email to authenticate.
    local = store.get_user_by_username(username)
    if local is None:
        return None, generic
    client = _supabase_client()
    if client is None:  # pragma: no cover - misconfigured at runtime
        return None, "Accounts are unavailable right now."
    # Authenticate against the REAL email stored on the local row at signup;
    # fall back to the synthesized placeholder only for legacy rows without a
    # stored email.
    login_email = _derive_email(username, local.get("email"))
    try:
        client.auth.sign_in_with_password(
            {"email": login_email, "password": password})
    except Exception:  # pragma: no cover - wrong creds / network
        return None, generic
    return {"id": local["id"], "username": local["username"]}, None


def _short(exc):
    """Compact, log-safe rendering of an exception message."""
    return str(exc).splitlines()[0][:200] if str(exc) else exc.__class__.__name__


def send_password_reset(username_or_email, redirect_to=None, store=None):
    """Trigger Supabase's password-reset email flow. Returns (ok, error).

    Delegates entirely to Supabase (it sends the email). A no-op with a clear
    message when Supabase is unconfigured. Never reveals whether an address
    exists (always reports success to the caller on the happy path).

    The identifier may be an email (used directly) or a username; for a
    username we resolve the REAL email stored on the local row (via ``store``)
    so the reset email goes to the user's actual address rather than the
    synthesized placeholder.
    """
    if not supabase_configured():
        return False, "Password reset is unavailable."
    client = _supabase_client()
    if client is None:  # pragma: no cover
        return False, "Password reset is unavailable."
    identifier = (username_or_email or "").strip()
    if "@" in identifier:
        # Caller supplied an email directly: use it as-is.
        email = identifier
    else:
        # Username: prefer the REAL email stored on the local row; fall back to
        # the synthesized placeholder only for legacy rows without one.
        stored_email = None
        if store is not None:
            local = store.get_user_by_username(identifier)
            if local is not None:
                stored_email = local.get("email")
        email = _derive_email(identifier, stored_email)
    try:
        opts = {"redirect_to": redirect_to} if redirect_to else None
        if opts:
            client.auth.reset_password_email(email, opts)
        else:
            client.auth.reset_password_email(email)
    except Exception:  # pragma: no cover - network; do not leak existence
        return True, None
    return True, None


def delete_supabase_user(supabase_user_id):
    """Delete the Supabase auth user via the service-role key. Returns (ok,
    error). No-op when the service key / id is unavailable; never raises."""
    if not supabase_user_id or not supabase_admin_configured():
        return False, None
    client = _supabase_client(service=True)
    if client is None:  # pragma: no cover
        return False, None
    try:
        client.auth.admin.delete_user(supabase_user_id)
    except Exception:  # pragma: no cover - network; non-fatal for local delete
        return False, None
    return True, None


def register_user(store, username, password, email=None):
    """Validate + create a user. Returns (user_dict, None) on success or
    (None, error_message) on failure.

    Routes through Supabase Auth when configured (SUPABASE_URL +
    SUPABASE_ANON_KEY), else the original bcrypt-local path. In BOTH paths the
    local users.username UNIQUE constraint is the duplicate-username guard.

    Requires an enabled Store (a configured database). Callers must handle the
    guest-only case (store.enabled == False) before calling.
    """
    if not store or not store.enabled:
        return None, "Accounts are unavailable right now."
    username = (username or "").strip()
    err = validate_username(username)
    if err:
        return None, err
    err = validate_password(password)
    if err:
        return None, err
    # A REAL email is required at signup so Supabase confirmation / password-
    # reset emails reach the user. Validated in BOTH the Supabase and bcrypt-
    # local paths; the normalized (stripped) address is stored on the local row.
    email = (email or "").strip()
    err = validate_email(email)
    if err:
        return None, err
    # Case-insensitive uniqueness is friendlier: reject a new username that
    # collides with an existing one ignoring case (e.g. 'Alice' vs 'alice').
    # This is a pre-check for a clear error message; the DB UNIQUE constraint
    # (plus create_user returning None) remains the ultimate guard against
    # races. Login stays exact-match, so existing accounts are unaffected.
    # Applies to BOTH the Supabase and bcrypt paths (FEAT-003 preserved).
    exists = False
    if hasattr(store, "username_exists_ci"):
        exists = store.username_exists_ci(username)
    else:  # pragma: no cover - defensive for older stores
        exists = store.get_user_by_username(username) is not None
    if exists:
        return None, "That username is already taken."

    if supabase_configured():
        return _register_supabase(store, username, password, email)

    # --- bcrypt-local fallback (default when Supabase is unconfigured) ---
    ph = hash_password(password)
    uid = store.create_user(username, ph, email=email)
    if uid is None:
        # Lost a race (or other insert failure): treat as taken.
        return None, "That username is already taken."
    return {"id": uid, "username": username}, None


def authenticate_user(store, username, password):
    """Verify credentials. Returns (user_dict, None) or (None, error_message).

    Routes through Supabase Auth when configured, else the original bcrypt
    path. Uses a uniform error message for both unknown-user and wrong-password
    so the endpoint does not leak which usernames exist.
    """
    if not store or not store.enabled:
        return None, "Accounts are unavailable right now."
    username = (username or "").strip()
    generic = "Incorrect username or password."

    if supabase_configured():
        return _authenticate_supabase(store, username, password)

    # --- bcrypt-local fallback ---
    user = store.get_user_by_username(username)
    if user is None:
        # Still run a hash to reduce user-enumeration timing differences.
        verify_password(password or "", "$2b$12$" + "x" * 53)
        return None, generic
    if not verify_password(password or "", user["password_hash"]):
        return None, generic
    return {"id": user["id"], "username": user["username"]}, None
