"""F3K Base Station — SQLite schema definition.

Call init_db(path) once on startup; it is safe to call on an existing DB.
"""

import os
import sqlite3


def init_db(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    db = sqlite3.connect(path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")

    db.executescript("""
        CREATE TABLE IF NOT EXISTS pilots (
            id                   INTEGER PRIMARY KEY,
            name                 TEXT NOT NULL,
            gliderscore_pilot_no INTEGER
        );

        CREATE TABLE IF NOT EXISTS flights (
            id          INTEGER PRIMARY KEY,
            pilot_id    INTEGER,
            duration_ms INTEGER,
            recorded_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (pilot_id) REFERENCES pilots(id)
        );

        CREATE TABLE IF NOT EXISTS competitions (
            id                  INTEGER PRIMARY KEY,
            name                TEXT NOT NULL,
            discipline          TEXT NOT NULL CHECK(discipline IN ('F3K', 'F5K', 'MIXED')),
            date                TEXT NOT NULL,
            gliderscore_comp_no INTEGER,
            prep_time_s         INTEGER NOT NULL DEFAULT 120,
            land_time_s         INTEGER NOT NULL DEFAULT 30,
            heat_gap_s          INTEGER NOT NULL DEFAULT 30,
            round_gap_s         INTEGER NOT NULL DEFAULT 30,
            focus_time_s        INTEGER NOT NULL DEFAULT 45,
            count_last_s        INTEGER NOT NULL DEFAULT 15
        );

        CREATE TABLE IF NOT EXISTS rounds (
            id             INTEGER PRIMARY KEY,
            competition_id INTEGER NOT NULL,
            round_no       INTEGER NOT NULL,
            task           TEXT NOT NULL,
            working_time_s INTEGER NOT NULL,
            discipline     TEXT NOT NULL CHECK(discipline IN ('F3K', 'F5K')),
            FOREIGN KEY (competition_id) REFERENCES competitions(id)
        );

        CREATE TABLE IF NOT EXISTS groups (
            id       INTEGER PRIMARY KEY,
            round_id INTEGER NOT NULL,
            group_no INTEGER NOT NULL,
            FOREIGN KEY (round_id) REFERENCES rounds(id)
        );

        CREATE TABLE IF NOT EXISTS group_pilots (
            group_id INTEGER NOT NULL,
            pilot_id INTEGER NOT NULL,
            PRIMARY KEY (group_id, pilot_id),
            FOREIGN KEY (group_id) REFERENCES groups(id),
            FOREIGN KEY (pilot_id) REFERENCES pilots(id)
        );

        CREATE TABLE IF NOT EXISTS competition_pilots (
            competition_id INTEGER NOT NULL,
            pilot_id       INTEGER NOT NULL,
            PRIMARY KEY (competition_id, pilot_id),
            FOREIGN KEY (competition_id) REFERENCES competitions(id),
            FOREIGN KEY (pilot_id)       REFERENCES pilots(id)
        );
    """)
    db.commit()

    # Extend tables with new columns — no-op if they already exist
    _add_flight_columns(db)
    _migrate_groups(db)
    _migrate_pilots(db)

    return db


def _migrate_pilots(db: sqlite3.Connection) -> None:
    existing = {row[1] for row in db.execute("PRAGMA table_info(pilots)")}
    if "gliderscore_pilot_no" not in existing:
        db.execute("ALTER TABLE pilots ADD COLUMN gliderscore_pilot_no INTEGER")
    db.commit()


def _migrate_groups(db: sqlite3.Connection) -> None:
    existing = {row[1] for row in db.execute("PRAGMA table_info(groups)")}
    if "dummy_count" not in existing:
        db.execute("ALTER TABLE groups ADD COLUMN dummy_count INTEGER NOT NULL DEFAULT 0")
    if "completed" not in existing:
        db.execute("ALTER TABLE groups ADD COLUMN completed BOOLEAN NOT NULL DEFAULT 0")
    db.commit()


def _add_flight_columns(db: sqlite3.Connection) -> None:
    existing = {row[1] for row in db.execute("PRAGMA table_info(flights)")}
    additions = [
        ("group_id",   "INTEGER"),
        ("flight_no",  "INTEGER"),
        ("altitude_m", "REAL"),
        ("penalty",    "INTEGER DEFAULT 0"),
    ]
    for col_name, col_type in additions:
        if col_name not in existing:
            db.execute(f"ALTER TABLE flights ADD COLUMN {col_name} {col_type}")
    db.commit()
