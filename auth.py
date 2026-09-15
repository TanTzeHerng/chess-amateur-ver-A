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
    """Signup path when Supabase is configured.

    Email is DECOUPLED from the blocking account-creation path: the local
    bcrypt-backed users row is ALWAYS created whenever the username is free,
    and the Supabase sign_up (which sends the confirmation / password-reset
    email) is attempted best-effort. Returns (user_dict, error, supabase_ok)
    where supabase_ok is:
      * True  -> sign_up succeeded and the row was linked (email-bound).
      * False -> sign_up (or client init) FAILED; the row was created UNLINKED
                 via the bcrypt fallback and the real failure was LOGGED.
    On a username race/collision returns (None, error, None). Never raises.
    """
    # Prefer the REAL email the user supplied so Supabase confirmation /
    # reset emails reach them; fall back to the synthesized placeholder only
    # if it is somehow absent (register_user requires it for real signups).
    reg_email = _derive_email(username, email)
    # Create the LOCAL row FIRST so account creation never depends on the
    # Supabase outcome. We store a bcrypt hash so the row is self-consistent
    # and usable even if the email step fails (bcrypt fallback login).
    ph = hash_password(password)
    uid = store.create_user(username, ph, email=reg_email)
    if uid is None:
        # Local username race/collision: treat as taken. No Supabase attempt.
        return None, "That username is already taken.", None
    # Best-effort Supabase sign_up: NEVER aborts account creation. A rate
    # limit, SMTP failure, unavailable client, or any other error simply
    # leaves the row unlinked (bcrypt fallback) and reports supabase_ok False.
    client = _supabase_client()
    if client is None:
        _log.error("Supabase sign_up skipped for %r: client unavailable; "
                   "account created UNLINKED (bcrypt fallback)", username)
        return {"id": uid, "username": username}, None, False
    try:
        res = client.auth.sign_up({"email": reg_email,
                                   "password": password})
    except Exception as exc:
        _log.error("Supabase sign_up failed for %r: %s; account created "
                   "UNLINKED (bcrypt fallback)", username, _short(exc))
        return {"id": uid, "username": username}, None, False
    sb_user = getattr(res, "user", None)
    sb_uid = getattr(sb_user, "id", None) if sb_user is not None else None
    if sb_uid and hasattr(store, "set_supabase_id"):
        store.set_supabase_id(uid, sb_uid)
        return {"id": uid, "username": username}, None, True
    # sign_up returned no usable user id: treat as a Supabase failure but keep
    # the (already created) local bcrypt account.
    _log.error("Supabase sign_up for %r returned no user id; account created "
               "UNLINKED (bcrypt fallback)", username)
    return {"id": uid, "username": username}, None, False


# Supabase auth attempt outcomes, distinguishing a definitive credential
# rejection (do NOT fall back to bcrypt) from a transient outage (fall back).
_SB_AUTHENTICATED = "authenticated"
_SB_REJECTED = "rejected"
_SB_UNAVAILABLE = "unavailable"

try:  # httpx is a hard dependency of supabase; import defensively regardless.
    import httpx as _httpx
except Exception:  # pragma: no cover - httpx should always be present
    _httpx = None

# Raw httpx transport exception class NAMES that mean "Supabase unreachable".
# gotrue/supabase_auth normally wrap these into AuthRetryableError, but we also
# match them directly in case a transport error ever escapes unwrapped.
_HTTPX_TRANSPORT_NAMES = frozenset({
    "ConnectError", "ConnectTimeout", "ReadTimeout", "WriteTimeout",
    "PoolTimeout", "TimeoutException", "NetworkError", "RemoteProtocolError",
    "TransportError",
})


def _classify_supabase_auth_error(exc):
    """Classify a sign_in_with_password exception as 'unavailable' or
    'rejected'.

    Rules:
      (a) UNAVAILABLE iff the exception (or any class in its type().__mro__) is
          named 'AuthRetryableError' -- gotrue/supabase_auth normalizes httpx
          transport errors into AuthRetryableError(status 0) and HTTP
          502/503/504 into AuthRetryableError -- OR the exception is a raw httpx
          transport error (ConnectError/ConnectTimeout/ReadTimeout/... ) in
          case a transport error ever escapes unwrapped.
      (b) REJECTED for everything else (AuthApiError, AuthInvalidCredentialsError,
          AuthUnknownError, and any other Exception).

    We match by CLASS NAME across the MRO rather than a single hard
    import-identity because `gotrue` and `supabase_auth` are DISTINCT packages
    in this venv whose exception classes are NOT identical objects; matching by
    name is robust whether the runtime raises gotrue.* or supabase_auth.* types.

    Conservative default: anything not CLEARLY retryable/transport maps to
    REJECTED so a genuine wrong-password is never salvaged by an old local
    bcrypt hash (no bcrypt bypass).
    """
    mro_names = {c.__name__ for c in type(exc).__mro__}
    if "AuthRetryableError" in mro_names:
        return _SB_UNAVAILABLE
    if _httpx is not None and isinstance(exc, _httpx.HTTPError):
        # Raw httpx transport errors (connect/read/timeout/network) mean the
        # backend was unreachable. An HTTPStatusError (a real HTTP response)
        # is NOT a transport failure, so it stays REJECTED by default.
        if mro_names & _HTTPX_TRANSPORT_NAMES:
            return _SB_UNAVAILABLE
    return _SB_REJECTED


def _authenticate_supabase(local, password):
    """Authenticate an already-resolved local row via Supabase.

    Used for rows that carry a non-empty supabase_user_id (email-bound). The
    local row is resolved by the caller so this routes PER-USER rather than on
    the global supabase_configured() flag.

    Returns a 3-tuple (user_dict, error, outcome) where outcome is one of
    'authenticated' | 'rejected' | 'unavailable' so the caller can decide
    whether a bcrypt fallback is appropriate:
      * client is None                 -> ('...', None, 'unavailable')
      * sign_in succeeds               -> (user_dict, None, 'authenticated')
      * sign_in raises, classified     -> (None, generic, 'rejected'|'unavailable')
    The user-facing error stays the uniform generic string in all failure
    cases. Never raises."""
    generic = "Incorrect username or password."
    client = _supabase_client()
    if client is None:
        return None, generic, _SB_UNAVAILABLE
    # Authenticate against the REAL email stored on the local row at signup;
    # fall back to the synthesized placeholder only for legacy rows without a
    # stored email.
    login_email = _derive_email(local["username"], local.get("email"))
    try:
        client.auth.sign_in_with_password(
            {"email": login_email, "password": password})
    except Exception as exc:  # pragma: no cover - wrong creds / network
        outcome = _classify_supabase_auth_error(exc)
        return None, generic, outcome
    return {"id": local["id"], "username": local["username"]}, None, _SB_AUTHENTICATED


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
    """Validate + create a user. Returns a 3-tuple (user_dict, error,
    supabase_ok).

    On any validation / duplicate-username failure returns (None, error, None).
    On success user_dict is set, error is None, and supabase_ok signals the
    Supabase email outcome:
      * True  -> Supabase was configured AND sign_up succeeded AND the row was
                 linked (the account is email-bound).
      * False -> Supabase was configured but sign_up (or the client) FAILED, so
                 we fell back to a bcrypt-local row (created UNLINKED; the real
                 failure was LOGGED).
      * None  -> Supabase was NOT configured (pure bcrypt-local mode).

    The Supabase email step is DECOUPLED from account creation: whenever the
    username/password/email validate and the username is free, the local row is
    ALWAYS created regardless of the Supabase outcome, and the email is stored
    on that row either way. In BOTH paths the local users.username UNIQUE
    constraint is the duplicate-username guard. Never raises.

    Requires an enabled Store (a configured database). Callers must handle the
    guest-only case (store.enabled == False) before calling.
    """
    if not store or not store.enabled:
        return None, "Accounts are unavailable right now.", None
    username = (username or "").strip()
    err = validate_username(username)
    if err:
        return None, err, None
    err = validate_password(password)
    if err:
        return None, err, None
    # A REAL email is required at signup so Supabase confirmation / password-
    # reset emails reach the user. Validated in BOTH the Supabase and bcrypt-
    # local paths; the normalized (stripped) address is stored on the local row.
    email = (email or "").strip()
    err = validate_email(email)
    if err:
        return None, err, None
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
        return None, "That username is already taken.", None

    if supabase_configured():
        return _register_supabase(store, username, password, email)

    # --- bcrypt-local fallback (default when Supabase is unconfigured) ---
    ph = hash_password(password)
    uid = store.create_user(username, ph, email=email)
    if uid is None:
        # Lost a race (or other insert failure): treat as taken.
        return None, "That username is already taken.", None
    return {"id": uid, "username": username}, None, None


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

    # PER-USER routing (not the global supabase_configured() flag): resolve the
    # local row, then route on whether IT is Supabase-linked. This keeps BOTH
    # kinds of existing account working -- a Supabase/email-bound row (non-empty
    # supabase_user_id) authenticates via Supabase, and a bcrypt-only row (NULL
    # supabase_user_id, e.g. one created via the decoupled email fallback)
    # authenticates via bcrypt -- regardless of the server-wide flag.
    user = store.get_user_by_username(username)
    if user is None:
        # Still run a hash to reduce user-enumeration timing differences.
        verify_password(password or "", "$2b$12$" + "x" * 53)
        return None, generic
    if user.get("supabase_user_id"):
        # Try Supabase FIRST for a linked row, then classify the outcome:
        #   * authenticated -> logged in.
        #   * rejected      -> definitive wrong-credentials; return the generic
        #     error and do NOT fall back (a wrong Supabase password must never
        #     be salvaged by an old local bcrypt hash -> no bypass).
        #   * unavailable   -> Supabase outage/unreachable; fall back to the
        #     row's local bcrypt hash so an outage does not lock the user out.
        sb_user, sb_err, outcome = _authenticate_supabase(user, password)
        if outcome == _SB_AUTHENTICATED:
            return sb_user, None
        if outcome == _SB_UNAVAILABLE:
            if verify_password(password or "", user["password_hash"]):
                return {"id": user["id"], "username": user["username"]}, None
            return None, generic
        # _SB_REJECTED (or any unknown outcome): uniform generic, no fallback.
        return None, generic

    # --- bcrypt-local (row not linked to Supabase) ---
    if not verify_password(password or "", user["password_hash"]):
        return None, generic
    return {"id": user["id"], "username": user["username"]}, None


def _find_supabase_user_id_by_email(email):
    """Resolve whether a Supabase user already EXISTS for ``email``.

    Returns a (user_id|None, resolved_ok) tuple:
      * (found_id, True)  -> a matching Supabase user exists.
      * (None, True)      -> definitively no matching user (admin lookup ran
                             successfully and found nothing).
      * (None, False)     -> admin is unavailable / misconfigured, or the
                             lookup errored; the caller should fall back to
                             sign_up.

    supabase 2.31.0 has NO get-by-email, so we page through
    ``client.auth.admin.list_users(page, per_page)`` (returns List[User] with
    ``.id`` / ``.email``) and match the email case-insensitively. Never raises.
    """
    if not supabase_admin_configured():
        return None, False
    client = _supabase_client(service=True)
    if client is None:
        return None, False
    target = (email or "").strip().casefold()
    per_page = 200
    max_pages = 50
    try:
        for page in range(1, max_pages + 1):
            users = client.auth.admin.list_users(page=page, per_page=per_page)
            users = users or []
            for u in users:
                u_email = (getattr(u, "email", None) or "").strip().casefold()
                if u_email and u_email == target:
                    return getattr(u, "id", None), True
            # Stop when the page is not full (last page reached).
            if len(users) < per_page:
                break
    except Exception as exc:  # pragma: no cover - admin/network issue is non-fatal
        _log.error("Supabase admin list_users lookup failed: %s", _short(exc))
        return None, False
    return None, True


def link_supabase_account(store, user_id, password):
    """Best-effort retry that binds an existing (bcrypt-local) account to
    Supabase / email. Returns (ok, error, email_bound). Never raises.

    The client RESENDS the signup password; the server FIRST verifies it against
    the stored bcrypt hash so an arbitrary password can NOT be set on the
    Supabase side. It then RESOLVES whether a Supabase user already exists for
    the stored email (via the service-role admin client) and links the row to
    that existing id when found; only if no existing user is found (or the admin
    lookup is unavailable / errors) does it fall back to calling Supabase
    sign_up and link the row on success.

    SECURITY: linking to an already-existing Supabase user by email is
    acceptable ONLY because the caller has already proven ownership of the local
    account -- the session guard in app.py plus the bcrypt re-verify of the
    resent password below. Do NOT add any unauthenticated resolve path or new
    endpoint on top of this helper.
    """
    if not store or not store.enabled:
        return False, "Accounts are unavailable right now.", False
    row = store.get_auth_row_by_id(user_id)
    if row is None:
        return False, "Account not found.", False
    # Already linked -> idempotent no-op success.
    if row.get("supabase_user_id"):
        return True, None, True
    # Verify the RESENT password against the stored bcrypt hash so a caller
    # cannot set an arbitrary Supabase password for the account.
    if not verify_password(password or "", row.get("password_hash")):
        return False, "Incorrect password.", False
    if not supabase_configured():
        return False, "Supabase is not configured on this server.", False
    reg_email = _derive_email(row["username"], row.get("email"))
    # First, try to RESOLVE an already-existing Supabase user for this email and
    # link to it instead of blindly re-registering (a prior half-completed
    # sign_up would otherwise fail with "email already registered" / rate-limit).
    uid_found, _resolved_ok = _find_supabase_user_id_by_email(reg_email)
    if uid_found and hasattr(store, "set_supabase_id"):
        store.set_supabase_id(user_id, uid_found)
        return True, None, True
    # No existing user found (or admin lookup unavailable/errored): fall back to
    # the original best-effort sign_up path.
    client = _supabase_client()
    if client is None:
        return False, "Accounts are unavailable right now.", False
    try:
        res = client.auth.sign_up({"email": reg_email, "password": password})
    except Exception as exc:
        _log.error("Supabase link sign_up failed for user %s: %s",
                   user_id, _short(exc))
        return False, "Could not link account: %s" % _short(exc), False
    sb_user = getattr(res, "user", None)
    sb_uid = getattr(sb_user, "id", None) if sb_user is not None else None
    if sb_uid and hasattr(store, "set_supabase_id"):
        store.set_supabase_id(user_id, sb_uid)
        return True, None, True
    return False, "Could not link account.", False
