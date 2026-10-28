import sqlite3
from contextlib import contextmanager
from typing import Iterator

from app.config import get_settings

# Append-only. Each entry runs once, in order, and is recorded in schema_migrations.
MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE llm_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            model TEXT NOT NULL,
            feature TEXT NOT NULL DEFAULT '',
            prompt_tokens INTEGER,
            completion_tokens INTEGER,
            latency_ms INTEGER NOT NULL,
            ok INTEGER NOT NULL,
            error TEXT
        );
        CREATE TABLE solicitations (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            source TEXT NOT NULL CHECK (source IN ('text', 'pdf', 'sam')),
            sam_notice_id TEXT,
            filename TEXT,
            file_id TEXT,
            title TEXT,
            raw_text TEXT NOT NULL,
            section_c TEXT,
            section_l TEXT,
            section_m TEXT,
            due_date TEXT,
            naics TEXT,
            set_aside TEXT,
            place_of_performance TEXT,
            llm_filled TEXT NOT NULL DEFAULT '[]'
        );
        """,
    ),
    (
        2,
        """
        CREATE TABLE firms (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            hubzone INTEGER NOT NULL DEFAULT 0,
            certifications TEXT NOT NULL DEFAULT '[]',
            naics_codes TEXT NOT NULL DEFAULT '[]',
            size_standard TEXT,
            place_of_performance TEXT,
            past_performance TEXT NOT NULL DEFAULT '[]'
        );
        CREATE TABLE decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            solicitation_id TEXT NOT NULL,
            firm_id INTEGER NOT NULL,
            verdict TEXT NOT NULL CHECK (verdict IN ('bid', 'no_bid', 'needs_review')),
            score INTEGER NOT NULL,
            reasons TEXT NOT NULL DEFAULT '[]'
        );
        """,
    ),
    (
        3,
        """
        ALTER TABLE llm_calls ADD COLUMN cached_tokens INTEGER;
        CREATE TABLE matrix_rows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            solicitation_id TEXT NOT NULL,
            firm_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            requirement TEXT NOT NULL,
            section_ref TEXT NOT NULL CHECK (section_ref IN ('C', 'L', 'M')),
            firm_capability TEXT NOT NULL,
            gap TEXT NOT NULL CHECK (gap IN ('none', 'partial', 'full')),
            evidence_needed TEXT NOT NULL,
            draft_response TEXT NOT NULL
        );
        CREATE INDEX idx_matrix_rows_sol_firm ON matrix_rows (solicitation_id, firm_id);
        """,
    ),
    (
        4,
        """
        ALTER TABLE matrix_rows ADD COLUMN requirement_type TEXT NOT NULL DEFAULT 'capability'
            CHECK (requirement_type IN ('capability', 'eligibility', 'submission'));
        """,
    ),
    (
        5,
        """
        CREATE TABLE artifacts (
            solicitation_id TEXT NOT NULL,
            firm_id INTEGER NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('narrative', 'capability')),
            created_at TEXT NOT NULL,
            content TEXT NOT NULL,
            PRIMARY KEY (solicitation_id, firm_id, kind)
        );
        """,
    ),
    (
        6,
        """
        CREATE TABLE matrix_rows_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            solicitation_id TEXT NOT NULL,
            firm_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            requirement TEXT NOT NULL,
            section_ref TEXT NOT NULL CHECK (section_ref IN ('C', 'L', 'M')),
            firm_capability TEXT NOT NULL,
            gap TEXT NOT NULL CHECK (gap IN ('none', 'partial', 'full')),
            evidence_needed TEXT NOT NULL,
            draft_response TEXT NOT NULL,
            requirement_type TEXT NOT NULL DEFAULT 'capability'
                CHECK (requirement_type IN ('capability', 'eligibility', 'submission', 'evaluation'))
        );
        INSERT INTO matrix_rows_new (id, created_at, solicitation_id, firm_id, position, requirement, section_ref,
            firm_capability, gap, evidence_needed, draft_response, requirement_type)
            SELECT id, created_at, solicitation_id, firm_id, position, requirement, section_ref,
            firm_capability, gap, evidence_needed, draft_response, requirement_type FROM matrix_rows;
        DROP TABLE matrix_rows;
        ALTER TABLE matrix_rows_new RENAME TO matrix_rows;
        CREATE INDEX idx_matrix_rows_sol_firm ON matrix_rows (solicitation_id, firm_id);
        """,
    ),
    (
        7,
        """
        ALTER TABLE decisions ADD COLUMN verdict_source TEXT NOT NULL DEFAULT 'model'
            CHECK (verdict_source IN ('model', 'rule'));
        ALTER TABLE decisions ADD COLUMN rule_reason TEXT NOT NULL DEFAULT '';
        """,
    ),
    (
        8,
        """
        ALTER TABLE llm_calls ADD COLUMN run_id TEXT;
        CREATE INDEX idx_llm_calls_run ON llm_calls (run_id);
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            firm_id INTEGER NOT NULL,
            solicitation_id TEXT NOT NULL,
            status TEXT NOT NULL,
            est_cost_usd REAL NOT NULL DEFAULT 0,
            events TEXT NOT NULL DEFAULT '[]'
        );
        ALTER TABLE firms ADD COLUMN demo INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE solicitations ADD COLUMN demo INTEGER NOT NULL DEFAULT 0;
        """,
    ),
    (
        9,
        """
        ALTER TABLE llm_calls ADD COLUMN audio_seconds REAL;
        CREATE TABLE pending_entries (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            firm_id INTEGER NOT NULL,
            run_id TEXT NOT NULL,
            transcript TEXT NOT NULL,
            entry TEXT NOT NULL,
            evidence TEXT NOT NULL,
            verified INTEGER NOT NULL DEFAULT 0
        );
        """,
    ),
]


def connect(path=None) -> sqlite3.Connection:
    path = path or get_settings().db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def session() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)")
    done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    for version, sql in MIGRATIONS:
        if version in done:
            continue
        conn.executescript(sql)
        conn.execute("INSERT INTO schema_migrations (version) VALUES (?)", (version,))
        conn.commit()


def init_db() -> None:
    conn = connect()
    try:
        migrate(conn)
    finally:
        conn.close()
