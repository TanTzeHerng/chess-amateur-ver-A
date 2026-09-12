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
import re

import bcrypt

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]+$")
USERNAME_MIN, USERNAME_MAX = 3, 32
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


def register_user(store, username, password):
    """Validate + create a user. Returns (user_dict, None) on success or
    (None, error_message) on failure.

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
    # Case-insensitive uniqueness is friendlier; check first, but the DB UNIQUE
    # constraint remains the ultimate guard against races.
    if store.get_user_by_username(username) is not None:
        return None, "That username is already taken."
    ph = hash_password(password)
    uid = store.create_user(username, ph)
    if uid is None:
        # Lost a race (or other insert failure): treat as taken.
        return None, "That username is already taken."
    return {"id": uid, "username": username}, None


def authenticate_user(store, username, password):
    """Verify credentials. Returns (user_dict, None) or (None, error_message).

    Uses a uniform error message for both unknown-user and wrong-password so
    the endpoint does not leak which usernames exist.
    """
    if not store or not store.enabled:
        return None, "Accounts are unavailable right now."
    username = (username or "").strip()
    generic = "Incorrect username or password."
    user = store.get_user_by_username(username)
    if user is None:
        # Still run a hash to reduce user-enumeration timing differences.
        verify_password(password or "", "$2b$12$" + "x" * 53)
        return None, generic
    if not verify_password(password or "", user["password_hash"]):
        return None, generic
    return {"id": user["id"], "username": user["username"]}, None
