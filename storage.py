#!/usr/bin/env python3
"""Persistence layer for Chess Amateur accounts and game history.

Design goals:
  * DURABLE storage of users + finished games so they survive deploys/restarts.
    In production this is Render Postgres (via DATABASE_URL). Because Render's
    local filesystem is ephemeral, SQLite-on-disk would silently lose data on
    every deploy -- so Postgres is the real backend.
  * GRACEFUL DEGRADATION: if DATABASE_URL is not configured, the whole storage
    layer is DISABLED (Store.enabled == False). The app then still serves guest
    play; only account creation/login/history are unavailable. The app never
    hard-crashes just because a database is missing.
  * DB-AGNOSTIC so the auth/history logic can be unit-tested locally against
    SQLite (no Postgres in CI/sandbox) while running on Postgres in prod. The
    SQL is deliberately kept to the common subset; parameter placeholders are
    adapted per backend.

Schema:
  users(id, username UNIQUE, password_hash, created_at)
  games(id, user_id -> users.id, human_color, result, result_reason,
        moves (space-separated UCI), started_at, ended_at, created_at)

Only LOGGED-IN users' games are ever stored here; guest games are never saved
(the caller simply does not persist them).
"""
import os
import threading
import time


class Store:
    """Thread-safe storage over Postgres (prod) or SQLite (local/testing).

    Backend selection:
      * If a url is provided (or DATABASE_URL is set) and looks like Postgres,
        use psycopg (v3).
      * Else if url looks like sqlite (sqlite:///path or a bare .db path), use
        the stdlib sqlite3 module -- used for local tests only.
      * Else the store is DISABLED (enabled == False): guest-only mode.
    """

    def __init__(self, url=None):
        self.url = url if url is not None else os.environ.get("DATABASE_URL")
        self.backend = None          # "postgres" | "sqlite" | None
        self.enabled = False
        self._lock = threading.Lock()
        self._sqlite_conn = None     # single shared connection for sqlite mode
        self._init_backend()

    # -- backend detection / connection ------------------------------------

    def _init_backend(self):
        url = self.url
        if not url:
            # No database configured -> guest-only mode.
            self.enabled = False
            self.backend = None
            return
        if url.startswith("postgres://") or url.startswith("postgresql://"):
            self.backend = "postgres"
        elif url.startswith("sqlite:///") or url.endswith(".db") or url == ":memory:":
            self.backend = "sqlite"
        else:
            # Unknown scheme: refuse rather than guess. Guest-only.
            self.enabled = False
            self.backend = None
            return

        try:
            self._create_schema()
            self.enabled = True
        except Exception as exc:  # pragma: no cover - defensive
            # If the DB is unreachable/misconfigured we degrade to guest-only
            # rather than take the whole site down.
            print("[storage] DISABLED: could not initialize database: %s" % exc)
            self.enabled = False

    def _placeholder(self):
        return "%s" if self.backend == "postgres" else "?"

    def _connect(self):
        if self.backend == "postgres":
            import psycopg  # imported lazily so sqlite/guest modes need no driver
            return psycopg.connect(self.url)
        elif self.backend == "sqlite":
            import sqlite3
            if self._sqlite_conn is None:
                path = self.url
                if path.startswith("sqlite:///"):
                    path = path[len("sqlite:///"):]
                self._sqlite_conn = sqlite3.connect(
                    path or ":memory:", check_same_thread=False)
            return self._sqlite_conn
        raise RuntimeError("no database backend configured")

    # -- schema ------------------------------------------------------------

    def _create_schema(self):
        if self.backend == "postgres":
            users_sql = (
                "CREATE TABLE IF NOT EXISTS users ("
                " id SERIAL PRIMARY KEY,"
                " username TEXT UNIQUE NOT NULL,"
                " password_hash TEXT NOT NULL,"
                " created_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            games_sql = (
                "CREATE TABLE IF NOT EXISTS games ("
                " id SERIAL PRIMARY KEY,"
                " user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,"
                " human_color TEXT NOT NULL,"
                " status TEXT NOT NULL DEFAULT 'in_progress',"
                " result TEXT,"
                " result_reason TEXT,"
                " moves TEXT NOT NULL,"
                " started_at TIMESTAMPTZ NOT NULL,"
                " ended_at TIMESTAMPTZ,"
                " created_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            # At most ONE in-progress game per user (integrity rule). A partial
            # unique index lets many finished games coexist but only one live.
            index_sql = (
                "CREATE UNIQUE INDEX IF NOT EXISTS one_active_game "
                "ON games(user_id) WHERE status = 'in_progress'"
            )
        else:  # sqlite
            users_sql = (
                "CREATE TABLE IF NOT EXISTS users ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " username TEXT UNIQUE NOT NULL,"
                " password_hash TEXT NOT NULL,"
                " created_at TEXT NOT NULL DEFAULT (datetime('now')))"
            )
            games_sql = (
                "CREATE TABLE IF NOT EXISTS games ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,"
                " human_color TEXT NOT NULL,"
                " status TEXT NOT NULL DEFAULT 'in_progress',"
                " result TEXT,"
                " result_reason TEXT,"
                " moves TEXT NOT NULL,"
                " started_at TEXT NOT NULL,"
                " ended_at TEXT,"
                " created_at TEXT NOT NULL DEFAULT (datetime('now')))"
            )
            index_sql = (
                "CREATE UNIQUE INDEX IF NOT EXISTS one_active_game "
                "ON games(user_id) WHERE status = 'in_progress'"
            )
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(users_sql)
            cur.execute(games_sql)
            cur.execute(index_sql)
            conn.commit()
        finally:
            if self.backend == "postgres":
                conn.close()

    # -- helpers -----------------------------------------------------------

    def _execute(self, sql, params=(), fetch=None, commit=False):
        """Run a statement with the right placeholder style + connection.

        fetch: None | "one" | "all". Returns rows for fetch modes.
        """
        with self._lock:
            conn = self._connect()
            close = self.backend == "postgres"
            try:
                cur = conn.cursor()
                cur.execute(sql, params)
                rows = None
                if fetch == "one":
                    rows = cur.fetchone()
                elif fetch == "all":
                    rows = cur.fetchall()
                if commit:
                    conn.commit()
                return rows
            finally:
                if close:
                    conn.close()

    # -- user operations ---------------------------------------------------

    def create_user(self, username, password_hash):
        """Insert a new user. Returns the new user id, or None if the username
        is already taken (UNIQUE violation) or on any insert error."""
        try:
            if self.backend == "postgres":
                row = self._execute(
                    "INSERT INTO users (username, password_hash)"
                    " VALUES (%s, %s) RETURNING id",
                    (username, password_hash), fetch="one", commit=True)
                return row[0] if row else None
            else:
                # sqlite
                with self._lock:
                    conn = self._connect()
                    cur = conn.cursor()
                    cur.execute(
                        "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                        (username, password_hash))
                    conn.commit()
                    return cur.lastrowid
        except Exception:
            # Unique-violation (username taken) or any insert error.
            return None

    def get_user_by_username(self, username):
        """Return (id, username, password_hash) or None."""
        ph = self._placeholder()
        row = self._execute(
            "SELECT id, username, password_hash FROM users WHERE username = %s" % ph,
            (username,), fetch="one")
        if not row:
            return None
        return {"id": row[0], "username": row[1], "password_hash": row[2]}

    def get_user_by_id(self, user_id):
        ph = self._placeholder()
        row = self._execute(
            "SELECT id, username FROM users WHERE id = %s" % ph,
            (user_id,), fetch="one")
        if not row:
            return None
        return {"id": row[0], "username": row[1]}

    # -- game operations ---------------------------------------------------

    def get_in_progress_game(self, user_id):
        """Return the user's single in-progress game, or None."""
        ph = self._placeholder()
        row = self._execute(
            "SELECT id, human_color, moves, started_at FROM games"
            " WHERE user_id = %s AND status = 'in_progress'"
            " ORDER BY id DESC LIMIT 1" % ph,
            (user_id,), fetch="one")
        if not row:
            return None
        return {
            "id": row[0],
            "human_color": row[1],
            "moves": (row[2].split() if row[2] else []),
            "started_at": str(row[3]),
        }

    def upsert_in_progress_game(self, user_id, game_id, human_color,
                                moves_uci, started_at):
        """Create or update the user's in-progress game (autosave).

        If game_id is given and belongs to the user, update its move list.
        Otherwise create a new in-progress row (the partial unique index
        guarantees only one active game per user). Returns the game id.
        """
        ph = self._placeholder()
        moves_str = " ".join(moves_uci or [])
        # Update path.
        if game_id is not None:
            self._execute(
                "UPDATE games SET moves = %s WHERE id = %s AND user_id = %s"
                " AND status = 'in_progress'" % (ph, ph, ph),
                (moves_str, game_id, user_id), commit=True)
            # Confirm it actually updated a row we own; if not, fall through
            # to insert.
            check = self._execute(
                "SELECT id FROM games WHERE id = %s AND user_id = %s"
                " AND status = 'in_progress'" % (ph, ph),
                (game_id, user_id), fetch="one")
            if check:
                return check[0]
        # Insert path.
        sql = (
            "INSERT INTO games (user_id, human_color, status, moves, started_at)"
            " VALUES (%s, %s, 'in_progress', %s, %s)" % (ph, ph, ph, ph)
        )
        params = (user_id, human_color, moves_str, started_at)
        if self.backend == "postgres":
            row = self._execute(sql + " RETURNING id", params,
                                fetch="one", commit=True)
            return row[0] if row else None
        else:
            with self._lock:
                conn = self._connect()
                cur = conn.cursor()
                cur.execute(sql, params)
                conn.commit()
                return cur.lastrowid

    def finish_game(self, user_id, game_id, human_color, result, result_reason,
                    moves_uci, started_at, ended_at):
        """Finalize a game (real result or resignation): set status='finished',
        the final move list, result, result_reason, and ended_at (to the
        second). Works whether or not an in-progress row already exists.
        Returns the game id."""
        ph = self._placeholder()
        moves_str = " ".join(moves_uci or [])
        # Try to finalize an existing in-progress row we own.
        if game_id is not None:
            self._execute(
                "UPDATE games SET status = 'finished', moves = %s, result = %s,"
                " result_reason = %s, ended_at = %s"
                " WHERE id = %s AND user_id = %s AND status = 'in_progress'"
                % (ph, ph, ph, ph, ph, ph),
                (moves_str, result, result_reason, ended_at, game_id, user_id),
                commit=True)
            check = self._execute(
                "SELECT id FROM games WHERE id = %s AND user_id = %s"
                % (ph, ph), (game_id, user_id), fetch="one")
            if check:
                return check[0]
        # No existing row (e.g. a game that ended on the very first ply before
        # an in-progress row was written): insert a finished row directly.
        sql = (
            "INSERT INTO games (user_id, human_color, status, result,"
            " result_reason, moves, started_at, ended_at)"
            " VALUES (%s, %s, 'finished', %s, %s, %s, %s, %s)"
            % (ph, ph, ph, ph, ph, ph, ph)
        )
        params = (user_id, human_color, result, result_reason, moves_str,
                  started_at, ended_at)
        if self.backend == "postgres":
            row = self._execute(sql + " RETURNING id", params,
                                fetch="one", commit=True)
            return row[0] if row else None
        else:
            with self._lock:
                conn = self._connect()
                cur = conn.cursor()
                cur.execute(sql, params)
                conn.commit()
                return cur.lastrowid

    def list_games(self, user_id):
        """Return a user's games, newest first, as a list of dicts."""
        ph = self._placeholder()
        rows = self._execute(
            "SELECT id, human_color, status, result, result_reason, moves,"
            " started_at, ended_at FROM games WHERE user_id = %s"
            " ORDER BY started_at DESC, id DESC" % ph,
            (user_id,), fetch="all") or []
        out = []
        for r in rows:
            out.append({
                "id": r[0],
                "human_color": r[1],
                "status": r[2],
                "result": r[3],
                "result_reason": r[4],
                "moves": (r[5].split() if r[5] else []),
                "started_at": str(r[6]),
                "ended_at": (str(r[7]) if r[7] is not None else None),
            })
        return out

    def get_game(self, user_id, game_id):
        """Return a single game owned by user_id, or None."""
        ph = self._placeholder()
        row = self._execute(
            "SELECT id, human_color, status, result, result_reason, moves,"
            " started_at, ended_at FROM games WHERE user_id = %s AND id = %s"
            % (ph, ph),
            (user_id, game_id), fetch="one")
        if not row:
            return None
        return {
            "id": row[0],
            "human_color": row[1],
            "status": row[2],
            "result": row[3],
            "result_reason": row[4],
            "moves": (row[5].split() if row[5] else []),
            "started_at": str(row[6]),
            "ended_at": (str(row[7]) if row[7] is not None else None),
        }
