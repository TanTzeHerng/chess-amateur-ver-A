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
import datetime
import os
import threading
import time

import eco  # pure module (needs only python-chess); keeps storage Flask-free


# GMT+8 (Asia/Singapore, UTC+8, no DST) as a fixed offset. Game calendar dates
# are derived at 00:00 GMT+8 boundaries so they line up with the dashboard's
# GMT+8 rating buckets (see app.GMT8/_parse_utc). storage CANNOT import app
# (app imports storage -> circular), so this is a self-contained mirror.
_GMT8 = datetime.timezone(datetime.timedelta(hours=8))


def _gmt8_date(at):
    """Return the GMT+8 calendar date ('YYYY-MM-DD') of a stored ISO instant.

    Mirrors app._parse_utc's robustness: handles ISO with a trailing 'Z' or
    '+00:00' offset, a space OR 'T' date/time separator, and second-precision
    (with or without fractional seconds). A naive timestamp is assumed to be
    UTC (server-generated). Returns None if the value cannot be parsed. Pure
    (no Flask/DB) so it is directly unit-testable."""
    if at is None:
        return None
    s = str(at).strip()
    if not s:
        return None
    s = s.replace(" ", "T", 1)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(s)
    except ValueError:
        # Fall back to bare second precision without an offset.
        try:
            dt = datetime.datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(_GMT8).date().isoformat()


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
                " email TEXT,"
                " fide_blitz DOUBLE PRECISION NOT NULL DEFAULT 1400,"
                " fide_rapid DOUBLE PRECISION NOT NULL DEFAULT 1400,"
                " fide_classical DOUBLE PRECISION NOT NULL DEFAULT 1400,"
                " rated_rating DOUBLE PRECISION NOT NULL DEFAULT 0,"
                " rated_rd DOUBLE PRECISION NOT NULL DEFAULT 350,"
                " rated_vol DOUBLE PRECISION NOT NULL DEFAULT 0.06,"
                " demo_pending BOOLEAN NOT NULL DEFAULT FALSE,"
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
                " base_seconds INTEGER,"          # time control base (NULL = unlimited)
                " increment INTEGER NOT NULL DEFAULT 0,"
                " clock_white DOUBLE PRECISION,"  # live remaining seconds (NULL = unlimited)
                " clock_black DOUBLE PRECISION,"
                " rating_delta TEXT,"             # self-describing, e.g. '+1' or '+1 FIDE rapid'
                " player_rating_after DOUBLE PRECISION,"  # player rating AFTER the game (NULL = casual/guest/unknown)
                " eco_code TEXT,"                 # ECO opening code, e.g. 'C60' (NULL if unknown)
                " eco_name TEXT,"                 # ECO opening name, e.g. 'Ruy Lopez'
                " mode TEXT,"                     # game mode: fide/rated/casual
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
            # FEAT-006: per-user collections (folders) for organizing My Games.
            # parent_id self-FK (nullable) enables indefinite nesting; deleting
            # a collection cascades to its child collections and memberships.
            collections_sql = (
                "CREATE TABLE IF NOT EXISTS collections ("
                " id SERIAL PRIMARY KEY,"
                " user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,"
                " parent_id INTEGER REFERENCES collections(id) ON DELETE CASCADE,"
                " name TEXT NOT NULL,"
                " created_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            game_collections_sql = (
                "CREATE TABLE IF NOT EXISTS game_collections ("
                " game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,"
                " collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,"
                " PRIMARY KEY (game_id, collection_id))"
            )
            # FEAT-007: curated puzzles bulk-loaded from the OFFLINE Lichess
            # puzzle-filtering job (only puzzles Chess Amateur fully solves at
            # depth 1 are kept). The web app only QUERIES this table at runtime;
            # it is never written to at request time. lichess_id is UNIQUE so
            # the loader is idempotent (ON CONFLICT DO NOTHING). moves is the
            # full space-separated UCI line AS LICHESS FORMATS IT (the first
            # move is the opponent's setup move, then the solver's moves
            # alternate). The curated set may be LARGE, so lichess_rating is
            # indexed for the FEAT-008 bell-curve rating-proximity selection.
            puzzles_sql = (
                "CREATE TABLE IF NOT EXISTS puzzles ("
                " id SERIAL PRIMARY KEY,"
                " lichess_id TEXT UNIQUE NOT NULL,"
                " fen TEXT NOT NULL,"
                " moves TEXT NOT NULL,"
                " lichess_rating INTEGER NOT NULL,"
                " themes TEXT,"
                " created_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            puzzles_index_sql = (
                "CREATE INDEX IF NOT EXISTS puzzles_lichess_rating"
                " ON puzzles(lichess_rating)"
            )
            # FEAT-002 (truncation study): puzzles that Chess Amateur could NOT
            # solve intact but COULD solve after progressive front-truncation
            # (removing the first 2 ply -- the solver's first move + the
            # opponent's reply -- one solver move at a time). These are NEVER
            # served (they are not in `puzzles`); this is a pure CORRELATION-
            # STUDY dataset so we can later model the rating of a truncated
            # puzzle from (original_solver_move_count, solver_moves_removed,
            # lichess_rating). lichess_id is UNIQUE so the offline job's
            # insert_truncation_stat is idempotent (ON CONFLICT DO NOTHING).
            # final_fen/final_moves describe the SHORTENED line that solved
            # (Lichess convention: final_moves[0] is the opponent setup move).
            truncation_stats_sql = (
                "CREATE TABLE IF NOT EXISTS puzzle_truncation_stats ("
                " id SERIAL PRIMARY KEY,"
                " lichess_id TEXT UNIQUE NOT NULL,"
                " original_solver_move_count INTEGER NOT NULL,"
                " solver_moves_removed INTEGER NOT NULL,"
                " plies_removed INTEGER NOT NULL,"
                " lichess_rating INTEGER NOT NULL,"
                " final_fen TEXT NOT NULL,"
                " final_moves TEXT NOT NULL,"
                " themes TEXT,"
                " created_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            truncation_stats_rating_index_sql = (
                "CREATE INDEX IF NOT EXISTS puzzle_truncation_stats_rating"
                " ON puzzle_truncation_stats(lichess_rating)"
            )
            truncation_stats_removed_index_sql = (
                "CREATE INDEX IF NOT EXISTS puzzle_truncation_stats_removed"
                " ON puzzle_truncation_stats(solver_moves_removed)"
            )
            # FEAT-010: durable rating time series for the Dashboard. ONE row
            # per rating change, uniform across ALL series (game FIDE classes,
            # Rated, and puzzle) so the dashboard has a single source. kind is
            # one of 'fide_blitz'|'fide_rapid'|'fide_classical'|'rated'|
            # 'puzzle'; rating is the value AFTER the change; at is the moment
            # of the change. Indexed on (user_id, kind, at) for the range query.
            rating_history_sql = (
                "CREATE TABLE IF NOT EXISTS rating_history ("
                " id SERIAL PRIMARY KEY,"
                " user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,"
                " kind TEXT NOT NULL,"
                " rating DOUBLE PRECISION NOT NULL,"
                " at TIMESTAMPTZ NOT NULL)"
            )
            rating_history_index_sql = (
                "CREATE INDEX IF NOT EXISTS rating_history_user_kind_at"
                " ON rating_history(user_id, kind, at)"
            )
        else:  # sqlite
            users_sql = (
                "CREATE TABLE IF NOT EXISTS users ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " username TEXT UNIQUE NOT NULL,"
                " password_hash TEXT NOT NULL,"
                " email TEXT,"
                " fide_blitz REAL NOT NULL DEFAULT 1400,"
                " fide_rapid REAL NOT NULL DEFAULT 1400,"
                " fide_classical REAL NOT NULL DEFAULT 1400,"
                " rated_rating REAL NOT NULL DEFAULT 0,"
                " rated_rd REAL NOT NULL DEFAULT 350,"
                " rated_vol REAL NOT NULL DEFAULT 0.06,"
                " demo_pending INTEGER NOT NULL DEFAULT 0,"
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
                " base_seconds INTEGER,"
                " increment INTEGER NOT NULL DEFAULT 0,"
                " clock_white REAL,"
                " clock_black REAL,"
                " rating_delta TEXT,"
                " player_rating_after REAL,"
                " eco_code TEXT,"
                " eco_name TEXT,"
                " mode TEXT,"
                " started_at TEXT NOT NULL,"
                " ended_at TEXT,"
                " created_at TEXT NOT NULL DEFAULT (datetime('now')))"
            )
            index_sql = (
                "CREATE UNIQUE INDEX IF NOT EXISTS one_active_game "
                "ON games(user_id) WHERE status = 'in_progress'"
            )
            collections_sql = (
                "CREATE TABLE IF NOT EXISTS collections ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,"
                " parent_id INTEGER REFERENCES collections(id) ON DELETE CASCADE,"
                " name TEXT NOT NULL,"
                " created_at TEXT NOT NULL DEFAULT (datetime('now')))"
            )
            game_collections_sql = (
                "CREATE TABLE IF NOT EXISTS game_collections ("
                " game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,"
                " collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,"
                " PRIMARY KEY (game_id, collection_id))"
            )
            # FEAT-007: curated puzzles (see the postgres branch above for the
            # rationale). SQLite mirror used for local/offline tests + the
            # sample run of the offline filtering job. lichess_id UNIQUE keeps
            # the INSERT OR IGNORE loader idempotent; lichess_rating is indexed.
            puzzles_sql = (
                "CREATE TABLE IF NOT EXISTS puzzles ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " lichess_id TEXT UNIQUE NOT NULL,"
                " fen TEXT NOT NULL,"
                " moves TEXT NOT NULL,"
                " lichess_rating INTEGER NOT NULL,"
                " themes TEXT,"
                " created_at TEXT NOT NULL DEFAULT (datetime('now')))"
            )
            puzzles_index_sql = (
                "CREATE INDEX IF NOT EXISTS puzzles_lichess_rating"
                " ON puzzles(lichess_rating)"
            )
            # FEAT-002 (truncation study): SQLite mirror of the postgres branch
            # above (used for local/offline tests + the sample run of the
            # offline filtering job). lichess_id UNIQUE keeps
            # insert_truncation_stat idempotent (INSERT OR IGNORE).
            truncation_stats_sql = (
                "CREATE TABLE IF NOT EXISTS puzzle_truncation_stats ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " lichess_id TEXT UNIQUE NOT NULL,"
                " original_solver_move_count INTEGER NOT NULL,"
                " solver_moves_removed INTEGER NOT NULL,"
                " plies_removed INTEGER NOT NULL,"
                " lichess_rating INTEGER NOT NULL,"
                " final_fen TEXT NOT NULL,"
                " final_moves TEXT NOT NULL,"
                " themes TEXT,"
                " created_at TEXT NOT NULL DEFAULT (datetime('now')))"
            )
            truncation_stats_rating_index_sql = (
                "CREATE INDEX IF NOT EXISTS puzzle_truncation_stats_rating"
                " ON puzzle_truncation_stats(lichess_rating)"
            )
            truncation_stats_removed_index_sql = (
                "CREATE INDEX IF NOT EXISTS puzzle_truncation_stats_removed"
                " ON puzzle_truncation_stats(solver_moves_removed)"
            )
            # FEAT-010: durable rating time series (see the postgres branch).
            rating_history_sql = (
                "CREATE TABLE IF NOT EXISTS rating_history ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,"
                " kind TEXT NOT NULL,"
                " rating REAL NOT NULL,"
                " at TEXT NOT NULL)"
            )
            rating_history_index_sql = (
                "CREATE INDEX IF NOT EXISTS rating_history_user_kind_at"
                " ON rating_history(user_id, kind, at)"
            )
        # All schema statements are IF NOT EXISTS / idempotent. They are run
        # one at a time and each is committed on success so a single failing
        # statement cannot abort the whole batch (on Postgres a failed
        # statement aborts the transaction and every later statement then
        # fails with "current transaction is aborted", which would disable the
        # Store and force guest mode). On failure we roll back to a clean state
        # and continue with the remaining statements.
        schema_statements = [
            users_sql, games_sql, index_sql, collections_sql,
            game_collections_sql, puzzles_sql, puzzles_index_sql,
            truncation_stats_sql, truncation_stats_rating_index_sql,
            truncation_stats_removed_index_sql, rating_history_sql,
            rating_history_index_sql,
        ]
        conn = self._connect()
        try:
            cur = conn.cursor()
            for stmt in schema_statements:
                try:
                    cur.execute(stmt)
                    conn.commit()
                except Exception as exc:
                    try:
                        conn.rollback()
                    except Exception:  # pragma: no cover - defensive
                        pass
                    print("[storage] schema note: %s" % exc)
            # Idempotent migration: add columns introduced after the original
            # accounts release, so an EXISTING database (whose users/games
            # tables predate time-controls/ratings) gains them WITHOUT dropping
            # data. CREATE TABLE IF NOT EXISTS alone never alters existing
            # tables, so this is required for a clean upgrade on Render Postgres.
            # _migrate_columns isolates each ALTER (commit/rollback per column).
            self._migrate_columns(conn, cur)
            conn.commit()
        finally:
            if self.backend == "postgres":
                conn.close()

    # -- migration ---------------------------------------------------------

    # Columns added after the original accounts release, with per-backend types.
    # (column_name, postgres_type, sqlite_type, default_clause_or_None)
    #
    # The default is written literally into the ADD COLUMN DDL. It MUST be a
    # valid literal for BOTH backends' declared type -- e.g. a BOOLEAN Postgres
    # column CANNOT take the integer literal "0" (Postgres raises "column is of
    # type boolean but default expression is of type integer" and, running in a
    # transaction, that error would abort the whole migration batch). Such
    # boolean-typed columns keep the SQLite integer default (0/1) but are mapped
    # to the matching SQL boolean literal for Postgres by _pg_default_literal().
    _MIGRATION_COLUMNS = {
        "users": [
            # Real signup email so Supabase confirmation / password-reset emails
            # reach the user. Nullable (default NULL) so any pre-existing row
            # survives the migration; new signups always store a validated
            # address via create_user(..., email=...).
            ("email", "TEXT", "TEXT", None),
            ("fide_blitz", "DOUBLE PRECISION", "REAL", "1400"),
            ("fide_rapid", "DOUBLE PRECISION", "REAL", "1400"),
            ("fide_classical", "DOUBLE PRECISION", "REAL", "1400"),
            ("rated_rating", "DOUBLE PRECISION", "REAL", "0"),
            ("rated_rd", "DOUBLE PRECISION", "REAL", "350"),
            ("rated_vol", "DOUBLE PRECISION", "REAL", "0.06"),
            # FEAT-004 (Supabase Auth + FIDE seeding). Both nullable so every
            # EXISTING account survives: supabase_user_id links the local row
            # to the Supabase auth user (NULL for bcrypt-local accounts);
            # fide_id records the FIDE ID supplied at signup (NULL if none).
            ("supabase_user_id", "TEXT", "TEXT", None),
            ("fide_id", "TEXT", "TEXT", None),
            # FEAT-008 (Train tab). Per-user PUZZLE Glicko-2 rating, separate
            # from the game rated_* rating. Defaults match Glickman's start
            # values (rating 1400 per the feature spec, RD 350, vol 0.06) so
            # every EXISTING account is backfilled to a fresh 1400 puzzle
            # rating. assigned_puzzle_id persists the CURRENTLY-assigned puzzle
            # so reloading the page shows the same puzzle (NULL = none assigned
            # yet; the next GET /api/puzzle picks one via the bell-curve
            # sampler and stores it).
            ("puzzle_rating", "DOUBLE PRECISION", "REAL", "1400"),
            ("puzzle_rd", "DOUBLE PRECISION", "REAL", "350"),
            ("puzzle_vol", "DOUBLE PRECISION", "REAL", "0.06"),
            ("assigned_puzzle_id", "INTEGER", "INTEGER", None),
            # FEAT-009 (onboarding demo). Highest demo version this user has
            # seen. Default 0 so EVERY existing account (and every brand-new
            # signup) starts below the current demo version and is therefore
            # shown the demo; POST /api/demo/seen bumps it to
            # CURRENT_DEMO_VERSION so it is not shown again until the constant
            # is bumped for a major update.
            ("demo_seen_version", "INTEGER", "INTEGER", "0"),
            # FEAT-001 (follow-up): onboarding demo is now NEW-USERS-ONLY.
            # demo_pending is set TRUE only in the register path (see
            # app.api_register) and cleared on POST /api/demo/seen, so the
            # demo is shown EXACTLY ONCE right after account creation and
            # NEVER re-shown on major updates. Default FALSE/0 so EVERY
            # pre-existing/migrated account is treated as demo-already-done.
            # (demo_seen_version above is kept for back-compat but no longer
            # gates showing the demo.)
            ("demo_pending", "BOOLEAN", "INTEGER", "0"),
        ],
        "games": [
            ("base_seconds", "INTEGER", "INTEGER", None),
            ("increment", "INTEGER", "INTEGER", "0"),
            ("clock_white", "DOUBLE PRECISION", "REAL", None),
            ("clock_black", "DOUBLE PRECISION", "REAL", None),
            ("rating_delta", "TEXT", "TEXT", None),
            # Player rating AFTER the game (rated/FIDE only). NULL for
            # casual/guest games and pre-migration rows (shown blank).
            ("player_rating_after", "DOUBLE PRECISION", "REAL", None),
            ("eco_code", "TEXT", "TEXT", None),
            ("eco_name", "TEXT", "TEXT", None),
            # Game MODE (fide/rated/casual). Needed by the My Games Mode filter
            # so both finished AND in-progress games can be filtered. NULL for
            # pre-migration rows (treated as unknown/casual by the UI).
            ("mode", "TEXT", "TEXT", None),
            # FEAT-005: custom STARTING position (Casual only). The stateless
            # replay anchors the move list on this FEN so a resumed
            # custom-position game rebuilds correctly. NULL -> standard start.
            ("start_fen", "TEXT", "TEXT", None),
        ],
    }

    def _existing_columns(self, cur, table):
        """Return the set of column names currently on `table`."""
        if self.backend == "postgres":
            cur.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name = %s", (table,))
            return {r[0] for r in cur.fetchall()}
        # sqlite
        cur.execute("PRAGMA table_info(%s)" % table)
        return {r[1] for r in cur.fetchall()}

    @staticmethod
    def _pg_default_literal(pg_type, default):
        """Map a migration default to a literal valid for its POSTGRES type.

        The migration table stores SQLite-flavoured defaults (booleans as the
        integer 0/1). Postgres is strict about types: `ADD COLUMN x BOOLEAN
        DEFAULT 0` fails ("column is of type boolean but default expression is
        of type integer"). Translate the integer-ish default to a SQL boolean
        literal for BOOLEAN columns; every other type is already a valid
        Postgres literal (numbers, quoted TEXT, etc.), so pass it through.
        """
        if pg_type.upper() == "BOOLEAN":
            return "FALSE" if str(default).strip() in ("0", "false", "FALSE",
                                                       "False") else "TRUE"
        return default

    def _migrate_columns(self, conn, cur):
        """Add any missing post-release columns to users/games (idempotent).

        Postgres supports ADD COLUMN IF NOT EXISTS; SQLite does not, so we
        check PRAGMA table_info first. Newly added columns get their default so
        existing rows are backfilled (e.g. old users become 1400 FIDE / 0
        Rated; old games get increment 0). Safe to run on every boot.

        ROBUSTNESS: each ALTER is isolated so one failing statement cannot
        poison the rest. On Postgres a failed statement aborts the current
        transaction ("current transaction is aborted, commands ignored...") and
        every subsequent statement then fails too -- which previously disabled
        the whole Store and forced guest mode. We therefore COMMIT after each
        successful ALTER and ROLLBACK after a failure, so the connection is
        always returned to a clean state and the remaining columns still get
        added. SQLite runs each ALTER in autocommit-ish fashion already; the
        commit/rollback calls are harmless there.
        """
        for table, cols in self._MIGRATION_COLUMNS.items():
            existing = self._existing_columns(cur, table)
            for name, pg_type, sq_type, default in cols:
                if name in existing:
                    continue
                col_type = pg_type if self.backend == "postgres" else sq_type
                ddl = "ALTER TABLE %s ADD COLUMN %s %s" % (table, name, col_type)
                if default is not None:
                    if self.backend == "postgres":
                        literal = self._pg_default_literal(pg_type, default)
                    else:
                        literal = default
                    ddl += " DEFAULT %s" % literal
                try:
                    cur.execute(ddl)
                    # Persist immediately so a later failure can't roll this
                    # (successful) column back out of the batch.
                    conn.commit()
                except Exception as exc:
                    # A concurrent boot may have added it, or the statement may
                    # be otherwise invalid. Roll back so the transaction is
                    # clean and the REMAINING columns can still be added rather
                    # than every subsequent statement failing with "current
                    # transaction is aborted".
                    try:
                        conn.rollback()
                    except Exception:  # pragma: no cover - defensive
                        pass
                    print("[storage] migration note for %s.%s: %s"
                          % (table, name, exc))

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

    def create_user(self, username, password_hash, email=None):
        """Insert a new user. Returns the new user id, or None if the username
        is already taken (UNIQUE violation) or on any insert error.

        ``email`` is the real signup email (nullable) used for Supabase
        confirmation / password-reset delivery."""
        try:
            if self.backend == "postgres":
                row = self._execute(
                    "INSERT INTO users (username, password_hash, email)"
                    " VALUES (%s, %s, %s) RETURNING id",
                    (username, password_hash, email), fetch="one", commit=True)
                return row[0] if row else None
            else:
                # sqlite
                with self._lock:
                    conn = self._connect()
                    cur = conn.cursor()
                    cur.execute(
                        "INSERT INTO users (username, password_hash, email)"
                        " VALUES (?, ?, ?)",
                        (username, password_hash, email))
                    conn.commit()
                    return cur.lastrowid
        except Exception:
            # Unique-violation (username taken) or any insert error.
            return None

    def get_user_by_username(self, username):
        """Return (id, username, password_hash, email) or None (exact-match)."""
        ph = self._placeholder()
        row = self._execute(
            "SELECT id, username, password_hash, email FROM users"
            " WHERE username = %s" % ph,
            (username,), fetch="one")
        if not row:
            return None
        return {"id": row[0], "username": row[1], "password_hash": row[2],
                "email": row[3]}

    def username_exists_ci(self, username):
        """Return True if a user with this username exists, comparing
        case-insensitively (LOWER(username) = LOWER(?)).

        Used at REGISTRATION only to reject near-duplicate usernames that differ
        only in case (e.g. 'Alice' vs 'alice'). Login stays exact-match via
        get_user_by_username so existing accounts keep working unchanged. Works
        on both Postgres and SQLite (both support LOWER()).
        """
        ph = self._placeholder()
        row = self._execute(
            "SELECT 1 FROM users WHERE LOWER(username) = LOWER(%s) LIMIT 1" % ph,
            (username,), fetch="one")
        return row is not None

    def get_user_by_supabase_id(self, supabase_user_id):
        """Return (id, username, password_hash) for the local users row linked
        to the given Supabase auth user id, or None.

        Used on the Supabase login path to resolve the local row (which owns all
        per-user game/rating/puzzle FKs) from the authenticated Supabase user.
        """
        if not supabase_user_id:
            return None
        ph = self._placeholder()
        row = self._execute(
            "SELECT id, username, password_hash FROM users"
            " WHERE supabase_user_id = %s" % ph,
            (supabase_user_id,), fetch="one")
        if not row:
            return None
        return {"id": row[0], "username": row[1], "password_hash": row[2]}

    def set_supabase_id(self, user_id, supabase_user_id):
        """Link a local users row to its Supabase auth user id (idempotent)."""
        ph = self._placeholder()
        self._execute(
            "UPDATE users SET supabase_user_id = %s WHERE id = %s" % (ph, ph),
            (supabase_user_id, user_id), commit=True)

    def set_fide_id(self, user_id, fide_id):
        """Store the FIDE ID supplied at signup on the user row (nullable)."""
        ph = self._placeholder()
        self._execute(
            "UPDATE users SET fide_id = %s WHERE id = %s" % (ph, ph),
            (fide_id, user_id), commit=True)

    def delete_user(self, user_id):
        """Delete a user (and all of their games).

        The users/games FK is declared ON DELETE CASCADE, which Postgres
        enforces automatically. SQLite, however, only enforces foreign keys
        when `PRAGMA foreign_keys = ON` is set on the connection (off by
        default), so we DELETE the user's games explicitly first and then the
        user row. Doing both in one transaction is idempotent and behaves
        identically on Postgres and SQLite. Returns True if a user row was
        deleted, else False.
        """
        ph = self._placeholder()
        with self._lock:
            conn = self._connect()
            close = self.backend == "postgres"
            try:
                cur = conn.cursor()
                cur.execute(
                    "DELETE FROM games WHERE user_id = %s" % ph, (user_id,))
                cur.execute(
                    "DELETE FROM rating_history WHERE user_id = %s" % ph,
                    (user_id,))
                cur.execute(
                    "DELETE FROM users WHERE id = %s" % ph, (user_id,))
                deleted = cur.rowcount
                conn.commit()
                return bool(deleted and deleted > 0)
            finally:
                if close:
                    conn.close()

    def get_user_by_id(self, user_id):
        """Return the user including all rating fields, or None."""
        ph = self._placeholder()
        row = self._execute(
            "SELECT id, username, fide_blitz, fide_rapid, fide_classical,"
            " rated_rating, rated_rd, rated_vol,"
            " puzzle_rating, puzzle_rd, puzzle_vol, assigned_puzzle_id,"
            " demo_seen_version, demo_pending"
            " FROM users WHERE id = %s" % ph,
            (user_id,), fetch="one")
        if not row:
            return None
        return {
            "id": row[0], "username": row[1],
            "fide_blitz": float(row[2]), "fide_rapid": float(row[3]),
            "fide_classical": float(row[4]),
            "rated_rating": float(row[5]), "rated_rd": float(row[6]),
            "rated_vol": float(row[7]),
            # FEAT-008 puzzle rating + currently-assigned puzzle.
            "puzzle_rating": float(row[8]), "puzzle_rd": float(row[9]),
            "puzzle_vol": float(row[10]),
            "assigned_puzzle_id": (int(row[11]) if row[11] is not None
                                   else None),
            # FEAT-009 onboarding demo. Default 0 for pre-migration rows.
            "demo_seen_version": (int(row[12]) if row[12] is not None else 0),
            # FEAT-001 (follow-up): demo is now new-users-only. demo_pending is
            # TRUE only for freshly-registered accounts and cleared once the
            # demo is dismissed. NULL-safe default False for pre-migration rows.
            "demo_pending": bool(row[13]) if row[13] is not None else False,
        }

    def get_demo_seen_version(self, user_id):
        """Return the highest demo version this user has seen (0 if unset)."""
        ph = self._placeholder()
        row = self._execute(
            "SELECT demo_seen_version FROM users WHERE id = %s" % ph,
            (user_id,), fetch="one")
        if not row or row[0] is None:
            return 0
        return int(row[0])

    def set_demo_seen_version(self, user_id, version):
        """Record that the user has seen the given demo version."""
        ph = self._placeholder()
        self._execute(
            "UPDATE users SET demo_seen_version = %s WHERE id = %s"
            % (ph, ph),
            (int(version), user_id), commit=True)

    def get_demo_pending(self, user_id):
        """Return True if this user still has the one-shot onboarding demo
        pending (i.e. is a freshly-registered account that has not dismissed
        it yet). NULL-safe: pre-migration rows default to False."""
        ph = self._placeholder()
        row = self._execute(
            "SELECT demo_pending FROM users WHERE id = %s" % ph,
            (user_id,), fetch="one")
        if not row or row[0] is None:
            return False
        return bool(row[0])

    def set_demo_pending(self, user_id, pending):
        """Set the one-shot onboarding-demo pending flag. Set TRUE only in the
        register path (brand-new accounts); cleared on POST /api/demo/seen."""
        ph = self._placeholder()
        self._execute(
            "UPDATE users SET demo_pending = %s WHERE id = %s"
            % (ph, ph),
            (1 if pending else 0, user_id), commit=True)

    def update_fide_rating(self, user_id, time_class, new_rating):
        """Set the user's FIDE rating for a given time_class
        (blitz/rapid/classical)."""
        col = {"blitz": "fide_blitz", "rapid": "fide_rapid",
               "classical": "fide_classical"}[time_class]
        ph = self._placeholder()
        self._execute("UPDATE users SET %s = %s WHERE id = %s" % (col, ph, ph),
                      (new_rating, user_id), commit=True)

    def update_rated_rating(self, user_id, rating, rd, vol):
        """Set the user's Glicko-2 (rating, RD, volatility) for Rated mode."""
        ph = self._placeholder()
        self._execute(
            "UPDATE users SET rated_rating = %s, rated_rd = %s, rated_vol = %s"
            " WHERE id = %s" % (ph, ph, ph, ph),
            (rating, rd, vol, user_id), commit=True)

    # -- game operations ---------------------------------------------------

    def get_in_progress_game(self, user_id):
        """Return the user's single in-progress game, or None."""
        ph = self._placeholder()
        row = self._execute(
            "SELECT id, human_color, moves, started_at, base_seconds, increment,"
            " clock_white, clock_black, mode, start_fen"
            " FROM games WHERE user_id = %s AND status = 'in_progress'"
            " ORDER BY id DESC LIMIT 1" % ph,
            (user_id,), fetch="one")
        if not row:
            return None
        clock = None
        if row[6] is not None and row[7] is not None:
            clock = {"white": float(row[6]), "black": float(row[7])}
        return {
            "id": row[0],
            "human_color": row[1],
            "moves": (row[2].split() if row[2] else []),
            "started_at": str(row[3]),
            "base_seconds": (int(row[4]) if row[4] is not None else None),
            "increment": int(row[5]) if row[5] is not None else 0,
            "clock": clock,
            "mode": row[8],
            "start_fen": row[9],
        }

    def upsert_in_progress_game(self, user_id, game_id, human_color,
                                moves_uci, started_at,
                                base_seconds=None, increment=0,
                                clock_white=None, clock_black=None,
                                mode=None, start_fen=None):
        """Create or update the user's in-progress game (autosave).

        If game_id is given and belongs to the user, update its move list AND
        the live remaining clocks (so a resumed game continues with the correct
        times). Otherwise create a new in-progress row. Returns the game id.
        """
        ph = self._placeholder()
        moves_str = " ".join(moves_uci or [])
        # Update path (also persists the live clocks).
        if game_id is not None:
            self._execute(
                "UPDATE games SET moves = %s, clock_white = %s, clock_black = %s"
                " WHERE id = %s AND user_id = %s AND status = 'in_progress'"
                % (ph, ph, ph, ph, ph),
                (moves_str, clock_white, clock_black, game_id, user_id),
                commit=True)
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
            "INSERT INTO games (user_id, human_color, status, moves, started_at,"
            " base_seconds, increment, clock_white, clock_black, mode, start_fen)"
            " VALUES (%s, %s, 'in_progress', %s, %s, %s, %s, %s, %s, %s, %s)"
            % (ph, ph, ph, ph, ph, ph, ph, ph, ph, ph)
        )
        params = (user_id, human_color, moves_str, started_at,
                  base_seconds, increment, clock_white, clock_black, mode,
                  start_fen)
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
                    moves_uci, started_at, ended_at,
                    base_seconds=None, increment=0, rating_delta=None,
                    eco_code=None, eco_name=None, mode=None,
                    player_rating_after=None):
        """Finalize a game (real result or resignation): set status='finished',
        the final move list, result, result_reason, ended_at (to the second),
        and the classified ECO opening (code + name). Works whether or not an
        in-progress row already exists. Returns the game id."""
        ph = self._placeholder()
        moves_str = " ".join(moves_uci or [])
        # Try to finalize an existing in-progress row we own.
        if game_id is not None:
            self._execute(
                "UPDATE games SET status = 'finished', moves = %s, result = %s,"
                " result_reason = %s, ended_at = %s, rating_delta = %s,"
                " player_rating_after = %s,"
                " eco_code = %s, eco_name = %s, mode = %s"
                " WHERE id = %s AND user_id = %s AND status = 'in_progress'"
                % (ph, ph, ph, ph, ph, ph, ph, ph, ph, ph, ph),
                (moves_str, result, result_reason, ended_at, rating_delta,
                 player_rating_after, eco_code, eco_name, mode, game_id,
                 user_id),
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
            " result_reason, moves, started_at, ended_at, base_seconds,"
            " increment, rating_delta, player_rating_after, eco_code,"
            " eco_name, mode)"
            " VALUES (%s, %s, 'finished', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            % (ph, ph, ph, ph, ph, ph, ph, ph, ph, ph, ph, ph, ph, ph)
        )
        params = (user_id, human_color, result, result_reason, moves_str,
                  started_at, ended_at, base_seconds, increment, rating_delta,
                  player_rating_after, eco_code, eco_name, mode)
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
            " started_at, ended_at, base_seconds, increment, rating_delta,"
            " eco_code, eco_name, mode, player_rating_after"
            " FROM games WHERE user_id = %s"
            " ORDER BY started_at DESC, id DESC" % ph,
            (user_id,), fetch="all") or []
        return [self._game_row_to_dict(r) for r in rows]

    def get_game(self, user_id, game_id):
        """Return a single game owned by user_id, or None."""
        ph = self._placeholder()
        row = self._execute(
            "SELECT id, human_color, status, result, result_reason, moves,"
            " started_at, ended_at, base_seconds, increment, rating_delta,"
            " eco_code, eco_name, mode, player_rating_after"
            " FROM games WHERE user_id = %s AND id = %s" % (ph, ph),
            (user_id, game_id), fetch="one")
        if not row:
            return None
        return self._game_row_to_dict(row)

    @staticmethod
    def _game_row_to_dict(r):
        moves = (r[5].split() if r[5] else [])
        eco_code = r[11]
        eco_name = r[12]
        # RECOMPUTE the ECO opening when the stored value is NULL/empty. Games
        # finished BEFORE the eco_code/eco_name columns existed have NULL there;
        # returning that NULL made the My Games ECO filter exclude them. Since
        # eco.classify is a pure longest-prefix match on the stored moves, we
        # can safely backfill it in-memory (idempotent, no DB migration needed).
        if not eco_code and moves:
            try:
                cls = eco.classify(moves)
            except Exception:
                cls = None
            if cls:
                eco_code = cls.get("eco")
                eco_name = cls.get("name")
        # DEFAULT the mode: pre-migration finished games have NULL mode, which
        # made the Mode filter match nothing even with all modes selected.
        # Treat unknown/NULL as 'casual' so those rows are filterable.
        mode = r[13] or "casual"
        return {
            "id": r[0],
            "human_color": r[1],
            "status": r[2],
            "result": r[3],
            "result_reason": r[4],
            "moves": moves,
            "started_at": str(r[6]),
            "ended_at": (str(r[7]) if r[7] is not None else None),
            "base_seconds": (int(r[8]) if r[8] is not None else None),
            "increment": (int(r[9]) if r[9] is not None else 0),
            "rating_delta": r[10],
            "eco_code": eco_code,
            "eco_name": eco_name,
            "mode": mode,
            # Player rating AFTER the game (rated/FIDE only); None for
            # casual/guest/pre-migration rows (the UI shows nothing extra).
            "player_rating_after": (
                float(r[14]) if r[14] is not None else None),
            # GMT+8 calendar date (YYYY-MM-DD) of the game's END timestamp
            # (ended_at, r[7]), falling back to started_at (r[6]) for
            # in-progress games with no ended_at. Computed server-side via the
            # pure _gmt8_date helper so an instant near midnight buckets to the
            # same GMT+8 day the dashboard uses (e.g. 16:00 UTC -> next day).
            # None when there is no usable timestamp. Never client-supplied.
            "date": _gmt8_date(r[7] if r[7] is not None else r[6]),
        }

    # -- collection operations (FEAT-006) ----------------------------------

    def create_collection(self, user_id, name, parent_id=None):
        """Create a per-user collection (folder). parent_id (nullable) nests it
        under another collection, enabling indefinite subfolders. Returns the
        new collection id, or None on error.

        Ownership integrity: if parent_id is given it MUST reference a
        collection owned by the same user; otherwise the create is refused
        (returns None) so a user cannot graft a folder onto someone else's tree.
        """
        if not name:
            return None
        ph = self._placeholder()
        if parent_id is not None:
            parent = self._execute(
                "SELECT id FROM collections WHERE id = %s AND user_id = %s"
                % (ph, ph), (parent_id, user_id), fetch="one")
            if not parent:
                return None
        sql = (
            "INSERT INTO collections (user_id, parent_id, name)"
            " VALUES (%s, %s, %s)" % (ph, ph, ph)
        )
        params = (user_id, parent_id, name)
        try:
            if self.backend == "postgres":
                row = self._execute(sql + " RETURNING id", params,
                                    fetch="one", commit=True)
                return row[0] if row else None
            with self._lock:
                conn = self._connect()
                cur = conn.cursor()
                cur.execute(sql, params)
                conn.commit()
                return cur.lastrowid
        except Exception:
            return None

    def rename_collection(self, user_id, collection_id, name):
        """Rename a collection the user owns. Returns True if a row changed."""
        if not name:
            return False
        ph = self._placeholder()
        with self._lock:
            conn = self._connect()
            close = self.backend == "postgres"
            try:
                cur = conn.cursor()
                cur.execute(
                    "UPDATE collections SET name = %s"
                    " WHERE id = %s AND user_id = %s" % (ph, ph, ph),
                    (name, collection_id, user_id))
                changed = cur.rowcount
                conn.commit()
                return bool(changed and changed > 0)
            finally:
                if close:
                    conn.close()

    def _collection_owner(self, cur, ph, collection_id):
        cur.execute(
            "SELECT user_id FROM collections WHERE id = %s" % ph,
            (collection_id,))
        row = cur.fetchone()
        return row[0] if row else None

    def delete_collection(self, user_id, collection_id):
        """Delete a collection the user owns, cascading to ALL descendant
        collections and every game_collections membership beneath the subtree.

        SQLite does not enforce ON DELETE CASCADE unless PRAGMA foreign_keys is
        ON per connection (off by default), so -- mirroring delete_user -- we
        do the cascade explicitly: gather the whole subtree of collection ids,
        delete their memberships, then delete the collection rows. Done in one
        transaction so it behaves identically on Postgres and SQLite. Returns
        True if the target collection was owned by the user and deleted.
        """
        ph = self._placeholder()
        with self._lock:
            conn = self._connect()
            close = self.backend == "postgres"
            try:
                cur = conn.cursor()
                # Ownership check first: refuse to touch another user's folder.
                if self._collection_owner(cur, ph, collection_id) != user_id:
                    return False
                # Breadth-first gather of the subtree (target + all descendants).
                subtree = [collection_id]
                frontier = [collection_id]
                while frontier:
                    cur.execute(
                        "SELECT id FROM collections WHERE parent_id = %s" % ph,
                        (frontier.pop(),))
                    children = [r[0] for r in cur.fetchall()]
                    for cid in children:
                        subtree.append(cid)
                        frontier.append(cid)
                for cid in subtree:
                    cur.execute(
                        "DELETE FROM game_collections WHERE collection_id = %s"
                        % ph, (cid,))
                # Delete children before parents to satisfy the self-FK on
                # backends that DO enforce it (Postgres); reverse subtree order
                # puts deepest descendants first.
                for cid in reversed(subtree):
                    cur.execute(
                        "DELETE FROM collections WHERE id = %s" % ph, (cid,))
                conn.commit()
                return True
            finally:
                if close:
                    conn.close()

    def list_collections(self, user_id):
        """Return the user's collections as flat rows (the client assembles the
        tree from parent_id). Ordered by parent_id then name for stable output.
        Each row: {id, parent_id, name, created_at}.
        """
        ph = self._placeholder()
        rows = self._execute(
            "SELECT id, parent_id, name, created_at FROM collections"
            " WHERE user_id = %s ORDER BY id" % ph,
            (user_id,), fetch="all") or []
        return [
            {"id": r[0], "parent_id": r[1], "name": r[2],
             "created_at": str(r[3])}
            for r in rows
        ]

    def add_game_to_collection(self, user_id, collection_id, game_id):
        """Assign a game to a collection, enforcing that BOTH the collection and
        the game belong to user_id. Idempotent (a duplicate membership is a
        no-op). Returns True on success, False if ownership fails."""
        ph = self._placeholder()
        with self._lock:
            conn = self._connect()
            close = self.backend == "postgres"
            try:
                cur = conn.cursor()
                if self._collection_owner(cur, ph, collection_id) != user_id:
                    return False
                cur.execute(
                    "SELECT id FROM games WHERE id = %s AND user_id = %s"
                    % (ph, ph), (game_id, user_id))
                if not cur.fetchone():
                    return False
                # Skip if the membership already exists (idempotent insert).
                cur.execute(
                    "SELECT 1 FROM game_collections"
                    " WHERE game_id = %s AND collection_id = %s" % (ph, ph),
                    (game_id, collection_id))
                if not cur.fetchone():
                    cur.execute(
                        "INSERT INTO game_collections (game_id, collection_id)"
                        " VALUES (%s, %s)" % (ph, ph),
                        (game_id, collection_id))
                conn.commit()
                return True
            finally:
                if close:
                    conn.close()

    def remove_game_from_collection(self, user_id, collection_id, game_id):
        """Remove a game from a collection the user owns. Returns True if a
        membership row was deleted."""
        ph = self._placeholder()
        with self._lock:
            conn = self._connect()
            close = self.backend == "postgres"
            try:
                cur = conn.cursor()
                if self._collection_owner(cur, ph, collection_id) != user_id:
                    return False
                cur.execute(
                    "DELETE FROM game_collections"
                    " WHERE game_id = %s AND collection_id = %s" % (ph, ph),
                    (game_id, collection_id))
                deleted = cur.rowcount
                conn.commit()
                return bool(deleted and deleted > 0)
            finally:
                if close:
                    conn.close()

    def list_games_in_collection(self, user_id, collection_id):
        """Return the games assigned to a collection the user owns, newest
        first, as game dicts (same shape as list_games). Returns [] if the
        collection is not owned by the user."""
        ph = self._placeholder()
        owner = self._execute(
            "SELECT user_id FROM collections WHERE id = %s" % ph,
            (collection_id,), fetch="one")
        if not owner or owner[0] != user_id:
            return []
        rows = self._execute(
            "SELECT g.id, g.human_color, g.status, g.result, g.result_reason,"
            " g.moves, g.started_at, g.ended_at, g.base_seconds, g.increment,"
            " g.rating_delta, g.eco_code, g.eco_name, g.mode,"
            " g.player_rating_after"
            " FROM games g JOIN game_collections gc ON gc.game_id = g.id"
            " WHERE gc.collection_id = %s AND g.user_id = %s"
            " ORDER BY g.started_at DESC, g.id DESC" % (ph, ph),
            (collection_id, user_id), fetch="all") or []
        return [self._game_row_to_dict(r) for r in rows]

    def list_game_collection_ids(self, user_id):
        """Return a mapping {game_id: [collection_id, ...]} for all of the
        user's game memberships, so the My Games UI can show/filter each game's
        collections without an N+1 query."""
        ph = self._placeholder()
        rows = self._execute(
            "SELECT gc.game_id, gc.collection_id FROM game_collections gc"
            " JOIN collections c ON c.id = gc.collection_id"
            " WHERE c.user_id = %s" % ph,
            (user_id,), fetch="all") or []
        out = {}
        for game_id, collection_id in rows:
            out.setdefault(game_id, []).append(collection_id)
        return out

    # -- puzzle operations (FEAT-007 / FEAT-008) ---------------------------

    def insert_puzzle(self, lichess_id, fen, moves, lichess_rating,
                      themes=None):
        """Insert ONE curated puzzle, idempotently on lichess_id.

        Used by the OFFLINE Lichess puzzle-filtering job / loader (never at
        request time). Re-inserting the same lichess_id is a no-op
        (ON CONFLICT DO NOTHING on Postgres; INSERT OR IGNORE on SQLite), so
        the whole job can be re-run without duplicating rows. `moves` is the
        space-separated UCI solution line exactly as Lichess formats it (the
        first move is the opponent's setup move). Returns True if a NEW row was
        inserted, False if it already existed (or on error).
        """
        ph = self._placeholder()
        if self.backend == "postgres":
            sql = (
                "INSERT INTO puzzles"
                " (lichess_id, fen, moves, lichess_rating, themes)"
                " VALUES (%s, %s, %s, %s, %s)"
                " ON CONFLICT (lichess_id) DO NOTHING" % (
                    ph, ph, ph, ph, ph))
        else:
            sql = (
                "INSERT OR IGNORE INTO puzzles"
                " (lichess_id, fen, moves, lichess_rating, themes)"
                " VALUES (%s, %s, %s, %s, %s)" % (ph, ph, ph, ph, ph))
        params = (lichess_id, fen, moves, int(lichess_rating), themes)
        try:
            with self._lock:
                conn = self._connect()
                close = self.backend == "postgres"
                try:
                    cur = conn.cursor()
                    cur.execute(sql, params)
                    inserted = bool(cur.rowcount and cur.rowcount > 0)
                    conn.commit()
                    return inserted
                finally:
                    if close:
                        conn.close()
        except Exception:
            return False

    def count_puzzles(self):
        """Return the total number of curated puzzles (used by FEAT-008 and by
        the offline job to report progress)."""
        row = self._execute("SELECT COUNT(*) FROM puzzles", fetch="one")
        return int(row[0]) if row else 0

    def get_puzzle_by_id(self, puzzle_id):
        """Return a curated puzzle by its numeric primary key, or None."""
        ph = self._placeholder()
        row = self._execute(
            "SELECT id, lichess_id, fen, moves, lichess_rating, themes"
            " FROM puzzles WHERE id = %s" % ph,
            (puzzle_id,), fetch="one")
        return self._puzzle_row_to_dict(row) if row else None

    def get_puzzle_by_lichess_id(self, lichess_id):
        """Return a curated puzzle by its Lichess PuzzleId, or None."""
        ph = self._placeholder()
        row = self._execute(
            "SELECT id, lichess_id, fen, moves, lichess_rating, themes"
            " FROM puzzles WHERE lichess_id = %s" % ph,
            (lichess_id,), fetch="one")
        return self._puzzle_row_to_dict(row) if row else None

    def puzzle_lichess_ids(self):
        """Return the set of lichess_ids already curated into the DB.

        The offline job uses this to RESUME: any puzzle whose id is already
        present is skipped without re-running the engine, so a restart never
        redoes finished work. For millions of rows this is a single indexed
        column scan (lichess_id is UNIQUE, hence indexed)."""
        rows = self._execute(
            "SELECT lichess_id FROM puzzles", fetch="all") or []
        return {r[0] for r in rows}

    def fetch_puzzles_in_rating_band(self, low, high, limit=50):
        """Return curated puzzles whose lichess_rating is within [low, high],
        ordered by rating, capped at `limit`.

        This is the rating-band candidate fetch the FEAT-008 bell-curve
        selection samples from; the puzzles_lichess_rating index makes the
        range scan efficient even on a large curated set."""
        ph = self._placeholder()
        rows = self._execute(
            "SELECT id, lichess_id, fen, moves, lichess_rating, themes"
            " FROM puzzles WHERE lichess_rating >= %s AND lichess_rating <= %s"
            " ORDER BY lichess_rating LIMIT %s" % (ph, ph, ph),
            (int(low), int(high), int(limit)), fetch="all") or []
        return [self._puzzle_row_to_dict(r) for r in rows]

    def puzzle_rating_extent(self):
        """Return (min_lichess_rating, max_lichess_rating) across the curated
        puzzles, or (None, None) if the table is empty.

        Used by the bell-curve sampler to clamp its rating band to the range
        that actually has puzzles (a single indexed aggregate, not a full
        load)."""
        row = self._execute(
            "SELECT MIN(lichess_rating), MAX(lichess_rating) FROM puzzles",
            fetch="one")
        if not row or row[0] is None:
            return None, None
        return int(row[0]), int(row[1])

    def update_puzzle_rating(self, user_id, rating, rd, vol):
        """Set the user's PUZZLE Glicko-2 (rating, RD, volatility).

        Separate from update_rated_rating (game rating) so the two never
        interfere. Called after a puzzle is solved or failed."""
        ph = self._placeholder()
        self._execute(
            "UPDATE users SET puzzle_rating = %s, puzzle_rd = %s,"
            " puzzle_vol = %s WHERE id = %s" % (ph, ph, ph, ph),
            (rating, rd, vol, user_id), commit=True)

    def set_assigned_puzzle(self, user_id, puzzle_id):
        """Persist the user's CURRENTLY-assigned puzzle (None clears it).

        Persisting the assignment is what makes a page reload show the SAME
        puzzle until it is solved or failed."""
        ph = self._placeholder()
        self._execute(
            "UPDATE users SET assigned_puzzle_id = %s WHERE id = %s"
            % (ph, ph),
            (puzzle_id, user_id), commit=True)

    def get_assigned_puzzle(self, user_id):
        """Return the user's currently-assigned puzzle (full dict) or None.

        Resolves the persisted assigned_puzzle_id to the puzzle row. If the id
        is stale (e.g. the puzzle was removed), returns None so the caller
        assigns a fresh one."""
        ph = self._placeholder()
        row = self._execute(
            "SELECT assigned_puzzle_id FROM users WHERE id = %s" % ph,
            (user_id,), fetch="one")
        if not row or row[0] is None:
            return None
        return self.get_puzzle_by_id(int(row[0]))

    @staticmethod
    def _puzzle_row_to_dict(r):
        return {
            "id": r[0],
            "lichess_id": r[1],
            "fen": r[2],
            "moves": (r[3].split() if r[3] else []),
            "lichess_rating": int(r[4]) if r[4] is not None else None,
            "themes": r[5],
        }

    # -- truncation study operations (FEAT-002) ----------------------------

    def insert_truncation_stat(self, lichess_id, original_solver_move_count,
                               solver_moves_removed, plies_removed,
                               lichess_rating, final_fen, final_moves,
                               themes=None):
        """Record ONE truncation-solved puzzle in puzzle_truncation_stats,
        idempotently on lichess_id.

        Used by the OFFLINE Lichess puzzle-filtering job (never at request
        time) for puzzles that FAILED intact but SOLVED after progressive
        front-truncation. These rows are the CORRELATION-STUDY dataset only;
        they are NEVER inserted into `puzzles` and so are never served. Re-
        inserting the same lichess_id is a no-op (ON CONFLICT DO NOTHING on
        Postgres; INSERT OR IGNORE on SQLite) so the job is re-runnable without
        duplicating rows. `final_moves` is the SHORTENED space-separated UCI
        line that solved (Lichess convention: index 0 is the opponent's setup
        move of the shortened line). Returns True if a NEW row was inserted,
        False if it already existed (or on error).
        """
        ph = self._placeholder()
        cols = ("lichess_id, original_solver_move_count, solver_moves_removed,"
                " plies_removed, lichess_rating, final_fen, final_moves, themes")
        vals = ", ".join([ph] * 8)
        if self.backend == "postgres":
            sql = ("INSERT INTO puzzle_truncation_stats (%s) VALUES (%s)"
                   " ON CONFLICT (lichess_id) DO NOTHING" % (cols, vals))
        else:
            sql = ("INSERT OR IGNORE INTO puzzle_truncation_stats (%s)"
                   " VALUES (%s)" % (cols, vals))
        params = (lichess_id, int(original_solver_move_count),
                  int(solver_moves_removed), int(plies_removed),
                  int(lichess_rating), final_fen, final_moves, themes)
        try:
            with self._lock:
                conn = self._connect()
                close = self.backend == "postgres"
                try:
                    cur = conn.cursor()
                    cur.execute(sql, params)
                    inserted = bool(cur.rowcount and cur.rowcount > 0)
                    conn.commit()
                    return inserted
                finally:
                    if close:
                        conn.close()
        except Exception:
            return False

    def count_truncation_stats(self):
        """Return the total number of truncation-study rows (used by the
        offline job to report progress)."""
        row = self._execute(
            "SELECT COUNT(*) FROM puzzle_truncation_stats", fetch="one")
        return int(row[0]) if row else 0

    def truncation_stat_lichess_ids(self):
        """Return the set of lichess_ids already recorded in the truncation
        study. The offline job unions this with puzzle_lichess_ids() + the
        checkpoint to RESUME without re-running the engine on a puzzle it has
        already routed to either output."""
        rows = self._execute(
            "SELECT lichess_id FROM puzzle_truncation_stats", fetch="all") or []
        return {r[0] for r in rows}

    def get_truncation_stat_by_lichess_id(self, lichess_id):
        """Return a truncation-study row (as a dict) by its Lichess PuzzleId,
        or None."""
        ph = self._placeholder()
        row = self._execute(
            "SELECT id, lichess_id, original_solver_move_count,"
            " solver_moves_removed, plies_removed, lichess_rating,"
            " final_fen, final_moves, themes"
            " FROM puzzle_truncation_stats WHERE lichess_id = %s" % ph,
            (lichess_id,), fetch="one")
        if not row:
            return None
        return {
            "id": row[0],
            "lichess_id": row[1],
            "original_solver_move_count": int(row[2]),
            "solver_moves_removed": int(row[3]),
            "plies_removed": int(row[4]),
            "lichess_rating": int(row[5]) if row[5] is not None else None,
            "final_fen": row[6],
            "final_moves": (row[7].split() if row[7] else []),
            "themes": row[8],
        }

    # -- rating history (FEAT-010 Dashboard) -------------------------------

    def append_rating_history(self, user_id, kind, rating, at):
        """Append ONE rating time-series point (idempotent-free append).

        Called on every rating change: game finish (FIDE class / Rated) and
        puzzle solve/fail. kind is one of
        'fide_blitz'|'fide_rapid'|'fide_classical'|'rated'|'puzzle'; rating is
        the value AFTER the change; at is the moment of the change (a datetime
        or an ISO-ish string). DB-agnostic (Postgres + SQLite). Never raises to
        the caller -- a rating write must not fail just because history logging
        does, so errors are swallowed (best-effort durable trail)."""
        if not self.enabled:
            return
        ph = self._placeholder()
        try:
            self._execute(
                "INSERT INTO rating_history (user_id, kind, rating, at)"
                " VALUES (%s, %s, %s, %s)" % (ph, ph, ph, ph),
                (user_id, kind, float(rating), at), commit=True)
        except Exception as exc:  # pragma: no cover - defensive
            print("[storage] append_rating_history note: %s" % exc)

    def list_rating_history(self, user_id, kind=None, since=None, until=None):
        """Return the user's rating history points ordered by (kind, at asc).

        Optional filters: kind restricts to one series; since/until bound the
        `at` timestamp (inclusive) as YYYY-MM-DD (or fuller) strings. Returns a
        list of {kind, rating, at} dicts. DB-agnostic; the string comparison on
        `at` works on both backends because timestamps are stored in
        ISO/`datetime()` lexicographically-sortable form."""
        ph = self._placeholder()
        sql = ("SELECT kind, rating, at FROM rating_history"
               " WHERE user_id = %s" % ph)
        params = [user_id]
        if kind is not None:
            sql += " AND kind = %s" % ph
            params.append(kind)
        if since is not None:
            sql += " AND at >= %s" % ph
            params.append(since)
        if until is not None:
            sql += " AND at <= %s" % ph
            params.append(until)
        sql += " ORDER BY kind ASC, at ASC, id ASC"
        rows = self._execute(sql, tuple(params), fetch="all") or []
        return [{"kind": r[0], "rating": float(r[1]), "at": str(r[2])}
                for r in rows]

    def earliest_rating_history_at(self, user_id):
        """Return the user's EARLIEST rating_history `at` string, or None.

        Read-only min over all of the user's series, used by the dashboard's
        'All' period to set the lower bound of the GMT+8 daily window. No
        writes, no schema change. Timestamps sort lexicographically so MIN(at)
        is the earliest instant on both backends."""
        ph = self._placeholder()
        row = self._execute(
            "SELECT MIN(at) FROM rating_history WHERE user_id = %s" % ph,
            (user_id,), fetch="one")
        if not row or row[0] is None:
            return None
        return str(row[0])
