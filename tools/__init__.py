# Marker so chess_amateur/tools is an importable package for the OFFLINE
# Lichess puzzle-filtering job and its tests. NOTHING here is imported by the
# web app (app.py) at request time; the puzzle-filtering pipeline runs ONCE,
# offline, in the sandbox and bulk-loads its curated output into Postgres.
