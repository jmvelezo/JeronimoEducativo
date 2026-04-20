import os
import sqlite3
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
SCHEMA = BASE_DIR / "schema.sql"


def _resolve_database_path() -> Path:
    explicit = os.environ.get("JEROCOIN_DATABASE") or os.environ.get("JEROCOIN_DB") or os.environ.get("PANCHICOIN_DATABASE") or os.environ.get("PANCHICOIN_DB")
    if explicit:
        return Path(explicit)
    preferred = BASE_DIR / "jerocoin.db"
    legacy = BASE_DIR / "panchicoin.db"
    if preferred.exists() or not legacy.exists():
        return preferred
    return legacy


DATABASE = _resolve_database_path()

SERVICE_TRACK_BY_TEAM_TYPE = {
    "robotica": "robotica",
    "desarrollo": "programacion",
}
PORTFOLIO_SERVICE_BY_TRACK = {
    "robotica": "programacion_robotica",
    "programacion": "programacion_robotica",
    "web_html": "pagina_web_simple",
}

DELIVERY_EXTRA_COLUMNS = {
    "code_text": "TEXT",
    "original_filename": "TEXT",
    "stored_filename": "TEXT",
    "file_size": "INTEGER NOT NULL DEFAULT 0",
}

PORTFOLIO_EXTRA_COLUMNS = {
    "skills": "TEXT",
    "tools": "TEXT",
    "work_style": "TEXT",
    "service_category": "TEXT",
}

TEAM_EXTRA_COLUMNS = {
    "service_track": "TEXT",
    "course_label": "TEXT",
    "market_role": "TEXT",
    "profile_blurb": "TEXT",
    "logo_original_filename": "TEXT",
    "logo_stored_filename": "TEXT",
}

CYCLE_EXTRA_COLUMNS = {
    "started": "INTEGER NOT NULL DEFAULT 0",
}

TRANSACTION_EXTRA_COLUMNS = {
    "cycle_id": "INTEGER",
}

INTERVENTOR_ASSIGNMENT_EXTRA_COLUMNS = {
    "student_id": "INTEGER",
}

CONTRACT_EXTRA_COLUMNS = {
    "contract_origin": "TEXT",
    "service_category": "TEXT",
    "admin_offer_id": "INTEGER",
    "client_team_id": "INTEGER",
    "provider_team_id": "INTEGER",
    "client_team_type": "TEXT",
    "provider_team_type": "TEXT",
    "provider_service_track": "TEXT",
    "requested_delivery_date": "TEXT",
    "request_message": "TEXT",
    "request_file_path": "TEXT",
    "request_original_filename": "TEXT",
    "request_stored_filename": "TEXT",
    "request_file_size": "INTEGER NOT NULL DEFAULT 0",
    "paused_by_deadline": "INTEGER NOT NULL DEFAULT 0",
    "paused_at": "TEXT",
    "pause_reason": "TEXT",
    "last_interventor_user_id": "INTEGER",
    "last_interventor_comment": "TEXT",
    "last_interventor_action": "TEXT",
    "last_interventor_signed_at": "TEXT",
}


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row[1] for row in rows}


def _ensure_delivery_columns(conn: sqlite3.Connection) -> None:
    existing = _table_columns(conn, "deliveries")
    for column_name, definition in DELIVERY_EXTRA_COLUMNS.items():
        if column_name not in existing:
            conn.execute(f"ALTER TABLE deliveries ADD COLUMN {column_name} {definition}")


def _ensure_portfolio_columns(conn: sqlite3.Connection) -> None:
    existing = _table_columns(conn, "portfolios")
    for column_name, definition in PORTFOLIO_EXTRA_COLUMNS.items():
        if column_name not in existing:
            conn.execute(f"ALTER TABLE portfolios ADD COLUMN {column_name} {definition}")
    existing = _table_columns(conn, "portfolios")
    if "service_category" in existing:
        rows = conn.execute(
            "SELECT p.id, t.service_track, t.team_type FROM portfolios p JOIN teams t ON t.id = p.team_id WHERE p.service_category IS NULL OR p.service_category = ''"
        ).fetchall()
        for row in rows:
            fallback_track = row["service_track"] or SERVICE_TRACK_BY_TEAM_TYPE.get(row["team_type"], "programacion")
            service_category = PORTFOLIO_SERVICE_BY_TRACK.get(fallback_track, "programacion_robotica")
            conn.execute("UPDATE portfolios SET service_category = ? WHERE id = ?", (service_category, row["id"]))


def _ensure_team_columns(conn: sqlite3.Connection) -> None:
    existing = _table_columns(conn, "teams")
    for column_name, definition in TEAM_EXTRA_COLUMNS.items():
        if column_name not in existing:
            conn.execute(f"ALTER TABLE teams ADD COLUMN {column_name} {definition}")
    existing = _table_columns(conn, "teams")
    if "service_track" in existing:
        for team_type, service_track in SERVICE_TRACK_BY_TEAM_TYPE.items():
            conn.execute(
                "UPDATE teams SET service_track = ? WHERE (service_track IS NULL OR service_track = '') AND team_type = ?",
                (service_track, team_type),
            )
    existing = _table_columns(conn, "teams")
    if "course_label" in existing:
        rows = conn.execute(
            """
            SELECT t.id, (
                SELECT s.course
                FROM team_members tm
                JOIN students s ON s.id = tm.student_id
                WHERE tm.team_id = t.id AND tm.active = 1 AND s.active = 1
                ORDER BY s.course, tm.id
                LIMIT 1
            ) AS inferred_course
            FROM teams t
            WHERE t.course_label IS NULL OR t.course_label = ''
            """
        ).fetchall()
        for row in rows:
            if row["inferred_course"]:
                conn.execute("UPDATE teams SET course_label = ? WHERE id = ?", (row["inferred_course"], row["id"]))
    if "market_role" in existing:
        rows = conn.execute("SELECT id, team_type, service_track FROM teams WHERE market_role IS NULL OR market_role = ''").fetchall()
        for row in rows:
            if row["team_type"] == "robotica":
                role = "client_only"
            elif row["service_track"] == "web_html":
                role = "provider_only"
            else:
                role = "both"
            conn.execute("UPDATE teams SET market_role = ? WHERE id = ?", (role, row["id"]))


def _ensure_cycle_columns(conn: sqlite3.Connection) -> None:
    existing = _table_columns(conn, "cycles")
    for column_name, definition in CYCLE_EXTRA_COLUMNS.items():
        if column_name not in existing:
            conn.execute(f"ALTER TABLE cycles ADD COLUMN {column_name} {definition}")
            if column_name == "started":
                conn.execute("UPDATE cycles SET started = 1 WHERE status = 'open'")


def _ensure_transaction_columns(conn: sqlite3.Connection) -> None:
    existing = _table_columns(conn, "transactions")
    for column_name, definition in TRANSACTION_EXTRA_COLUMNS.items():
        if column_name not in existing:
            conn.execute(f"ALTER TABLE transactions ADD COLUMN {column_name} {definition}")




def _ensure_interventor_assignment_columns(conn: sqlite3.Connection) -> None:
    existing = _table_columns(conn, "interventor_assignments")
    for column_name, definition in INTERVENTOR_ASSIGNMENT_EXTRA_COLUMNS.items():
        if column_name not in existing:
            conn.execute(f"ALTER TABLE interventor_assignments ADD COLUMN {column_name} {definition}")

def _ensure_contract_columns(conn: sqlite3.Connection) -> None:
    existing = _table_columns(conn, "contracts")
    for column_name, definition in CONTRACT_EXTRA_COLUMNS.items():
        if column_name not in existing:
            conn.execute(f"ALTER TABLE contracts ADD COLUMN {column_name} {definition}")
    existing = _table_columns(conn, "contracts")
    if "contract_origin" in existing:
        conn.execute("UPDATE contracts SET contract_origin = 'team_request' WHERE contract_origin IS NULL OR contract_origin = ''")
    if "service_category" in existing:
        conn.execute(
            "UPDATE contracts SET service_category = COALESCE(NULLIF(service_category, ''), (SELECT p.service_category FROM portfolios p WHERE p.id = contracts.portfolio_id), 'programacion_robotica')"
        )
    if "client_team_id" in existing:
        conn.execute("UPDATE contracts SET client_team_id = COALESCE(client_team_id, robotics_team_id)")
    if "provider_team_id" in existing:
        conn.execute("UPDATE contracts SET provider_team_id = COALESCE(provider_team_id, development_team_id)")
    if "client_team_type" in existing:
        conn.execute(
            "UPDATE contracts SET client_team_type = COALESCE(NULLIF(client_team_type, ''), (SELECT t.team_type FROM teams t WHERE t.id = COALESCE(contracts.client_team_id, contracts.robotics_team_id)), 'robotica')"
        )
    if "provider_team_type" in existing:
        conn.execute(
            "UPDATE contracts SET provider_team_type = COALESCE(NULLIF(provider_team_type, ''), (SELECT t.team_type FROM teams t WHERE t.id = COALESCE(contracts.provider_team_id, contracts.development_team_id)), 'desarrollo')"
        )
    if "provider_service_track" in existing:
        conn.execute(
            "UPDATE contracts SET provider_service_track = COALESCE(NULLIF(provider_service_track, ''), (SELECT t.service_track FROM teams t WHERE t.id = COALESCE(contracts.provider_team_id, contracts.development_team_id)), 'programacion')"
        )


def _backfill_transaction_cycles(conn: sqlite3.Connection) -> None:
    import re

    rows = conn.execute(
        "SELECT id, transaction_type, description, created_at FROM transactions WHERE cycle_id IS NULL ORDER BY id"
    ).fetchall()
    if not rows:
        return

    contract_map = {
        row["id"]: row["cycle_id"]
        for row in conn.execute("SELECT id, cycle_id FROM contracts WHERE cycle_id IS NOT NULL").fetchall()
    }
    cycle_names = conn.execute("SELECT id, name FROM cycles").fetchall()

    for row in rows:
        cycle_id = None
        description = row["description"] or ""
        if row["transaction_type"] in {"reserve", "contract_payment", "refund"}:
            match = re.search(r"contrato #(\d+)", description)
            if match:
                cycle_id = contract_map.get(int(match.group(1)))
        if cycle_id is None and row["transaction_type"] in {"maintenance", "penalty"}:
            for cycle in cycle_names:
                if description.startswith(f"{cycle['name']}:"):
                    cycle_id = cycle["id"]
                    break
        if cycle_id is not None:
            conn.execute("UPDATE transactions SET cycle_id = ? WHERE id = ?", (cycle_id, row["id"]))


def _ensure_cycle_teams_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cycle_teams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cycle_id INTEGER NOT NULL,
            team_id INTEGER NOT NULL,
            team_type_snapshot TEXT NOT NULL CHECK(team_type_snapshot IN ('robotica', 'desarrollo')),
            locked_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(cycle_id, team_id),
            FOREIGN KEY (cycle_id) REFERENCES cycles(id) ON DELETE CASCADE,
            FOREIGN KEY (team_id) REFERENCES teams(id) ON DELETE CASCADE
        )
        """
    )
    cycles = conn.execute("SELECT id, status FROM cycles").fetchall()
    for cycle in cycles:
        linked = conn.execute(
            "SELECT COUNT(*) AS c FROM cycle_teams WHERE cycle_id = ?", (cycle["id"],)
        ).fetchone()["c"]
        if linked:
            continue
        teams = conn.execute("SELECT id, team_type FROM teams").fetchall()
        for team in teams:
            conn.execute(
                "INSERT OR IGNORE INTO cycle_teams (cycle_id, team_id, team_type_snapshot) VALUES (?, ?, ?)",
                (cycle["id"], team["id"], team["team_type"]),
            )



def _ensure_team_gallery_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS team_gallery (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team_id INTEGER NOT NULL,
            caption TEXT,
            original_filename TEXT,
            stored_filename TEXT NOT NULL UNIQUE,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (team_id) REFERENCES teams(id) ON DELETE CASCADE
        )
        """
    )



def _ensure_admin_offers_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_offers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            service_category TEXT NOT NULL DEFAULT 'programacion_robotica' CHECK(service_category IN ('programacion_robotica', 'pagina_web_simple', 'landing_html', 'automatizacion', 'otro')),
            reward_amount INTEGER NOT NULL DEFAULT 0,
            created_by_user_id INTEGER,
            cycle_id INTEGER,
            taken_by_team_id INTEGER,
            deadline TEXT,
            status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'taken', 'closed', 'cancelled')),
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (created_by_user_id) REFERENCES users(id) ON DELETE SET NULL,
            FOREIGN KEY (cycle_id) REFERENCES cycles(id) ON DELETE SET NULL,
            FOREIGN KEY (taken_by_team_id) REFERENCES teams(id) ON DELETE SET NULL
        )
        """
    )


def _ensure_ai_assistant_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_assistant_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contract_id INTEGER NOT NULL,
            asked_by_user_id INTEGER NOT NULL,
            question TEXT NOT NULL,
            pasted_code TEXT,
            source_kind TEXT NOT NULL CHECK(source_kind IN ('pasted_code', 'latest_delivery_code', 'latest_delivery_file')),
            source_excerpt TEXT,
            response_text TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('answered', 'blocked', 'error')) DEFAULT 'answered',
            model_name TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (contract_id) REFERENCES contracts(id) ON DELETE CASCADE,
            FOREIGN KEY (asked_by_user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(SCHEMA.read_text(encoding="utf-8"))
        _ensure_delivery_columns(conn)
        _ensure_team_columns(conn)
        _ensure_portfolio_columns(conn)
        _ensure_cycle_columns(conn)
        _ensure_transaction_columns(conn)
        _ensure_interventor_assignment_columns(conn)
        _ensure_contract_columns(conn)
        _ensure_cycle_teams_table(conn)
        _ensure_team_gallery_table(conn)
        _ensure_admin_offers_table(conn)
        _ensure_ai_assistant_table(conn)
        _backfill_transaction_cycles(conn)
        conn.commit()


def query_all(sql: str, params: tuple[Any, ...] = ()):
    with get_connection() as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall()


def query_one(sql: str, params: tuple[Any, ...] = ()):
    with get_connection() as conn:
        cur = conn.execute(sql, params)
        return cur.fetchone()


def execute(sql: str, params: tuple[Any, ...] = ()) -> int:
    with get_connection() as conn:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid


def execute_many(sql: str, rows: list[tuple[Any, ...]]) -> None:
    with get_connection() as conn:
        conn.executemany(sql, rows)
        conn.commit()
