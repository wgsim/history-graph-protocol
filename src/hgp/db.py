"""SQLite database layer for HGP."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any

_log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from hgp.models import EvidenceRef

_SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS operations (
    op_id           TEXT PRIMARY KEY,
    op_type         TEXT NOT NULL CHECK (op_type IN (
                        'artifact', 'hypothesis', 'merge', 'invalidation')),
    status          TEXT NOT NULL DEFAULT 'COMPLETED' CHECK (status IN (
                        'PENDING', 'COMPLETED', 'INVALIDATED', 'MISSING_BLOB')),
    commit_seq      INTEGER UNIQUE,
    agent_id        TEXT NOT NULL,
    object_hash     TEXT,
    chain_hash      TEXT,
    metadata        TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    completed_at    TEXT,
    access_count    REAL NOT NULL DEFAULT 0.0,
    last_accessed   TEXT,
    memory_tier     TEXT NOT NULL DEFAULT 'long_term'
                        CHECK (memory_tier IN ('short_term', 'long_term', 'inactive')),
    file_path       TEXT,
    FOREIGN KEY (object_hash) REFERENCES objects(hash)
);

CREATE INDEX IF NOT EXISTS idx_operations_agent     ON operations(agent_id);
CREATE INDEX IF NOT EXISTS idx_operations_type      ON operations(op_type);
CREATE INDEX IF NOT EXISTS idx_operations_status    ON operations(status);
CREATE INDEX IF NOT EXISTS idx_operations_seq       ON operations(commit_seq);
CREATE INDEX IF NOT EXISTS idx_operations_file_path ON operations(file_path);
CREATE INDEX IF NOT EXISTS idx_operations_file_path_seq ON operations(file_path, commit_seq DESC);

CREATE TABLE IF NOT EXISTS op_edges (
    child_op_id     TEXT NOT NULL,
    parent_op_id    TEXT NOT NULL,
    edge_type       TEXT NOT NULL DEFAULT 'causal' CHECK (edge_type IN ('causal', 'invalidates')),
    PRIMARY KEY (child_op_id, parent_op_id),
    FOREIGN KEY (child_op_id) REFERENCES operations(op_id),
    FOREIGN KEY (parent_op_id) REFERENCES operations(op_id)
);

CREATE INDEX IF NOT EXISTS idx_edges_parent ON op_edges(parent_op_id);
CREATE INDEX IF NOT EXISTS idx_edges_child  ON op_edges(child_op_id);

CREATE TABLE IF NOT EXISTS objects (
    hash            TEXT PRIMARY KEY,
    size            INTEGER NOT NULL,
    mime_type       TEXT,
    status          TEXT NOT NULL DEFAULT 'VALID' CHECK (status IN (
                        'VALID', 'MISSING_BLOB', 'ORPHAN_CANDIDATE')),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    gc_marked_at    TEXT
);

CREATE TABLE IF NOT EXISTS leases (
    lease_id                TEXT PRIMARY KEY,
    agent_id                TEXT NOT NULL,
    subgraph_root_op_id     TEXT NOT NULL,
    chain_hash              TEXT NOT NULL,
    issued_at               TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    expires_at              TEXT NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN (
                                'ACTIVE', 'EXPIRED', 'RELEASED')),
    FOREIGN KEY (subgraph_root_op_id) REFERENCES operations(op_id)
);

CREATE INDEX IF NOT EXISTS idx_leases_agent  ON leases(agent_id);
CREATE INDEX IF NOT EXISTS idx_leases_status ON leases(status);

CREATE TABLE IF NOT EXISTS commit_counter (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    next_seq        INTEGER NOT NULL DEFAULT 1
);

INSERT OR IGNORE INTO commit_counter (id, next_seq) VALUES (1, 1);

CREATE TABLE IF NOT EXISTS git_anchors (
    op_id           TEXT NOT NULL,
    git_commit_sha  TEXT NOT NULL CHECK (length(git_commit_sha) = 40),
    repository      TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (op_id, git_commit_sha),
    FOREIGN KEY (op_id) REFERENCES operations(op_id)
);

CREATE TABLE IF NOT EXISTS op_evidence (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    citing_op_id  TEXT NOT NULL,
    cited_op_id   TEXT NOT NULL,
    relation      TEXT NOT NULL CHECK (relation IN
                    ('supports', 'refutes', 'context', 'method', 'source')),
    scope         TEXT DEFAULT NULL CHECK (scope IS NULL OR length(scope) <= 1024),
    inference     TEXT DEFAULT NULL CHECK (inference IS NULL OR length(inference) <= 4096),
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (citing_op_id, cited_op_id),
    FOREIGN KEY (citing_op_id) REFERENCES operations(op_id),
    FOREIGN KEY (cited_op_id)  REFERENCES operations(op_id)
);

CREATE INDEX IF NOT EXISTS idx_evidence_citing ON op_evidence(citing_op_id);
CREATE INDEX IF NOT EXISTS idx_evidence_cited  ON op_evidence(cited_op_id);
"""

_MIGRATION_FILE_PATH = """
ALTER TABLE operations ADD COLUMN file_path TEXT;
CREATE INDEX IF NOT EXISTS idx_operations_file_path ON operations(file_path);
CREATE INDEX IF NOT EXISTS idx_operations_file_path_seq ON operations(file_path, commit_seq DESC);
"""

# Server-side cap on evidence result sets — prevents reverse fan-out DoS where
# one widely-cited op triggers an unbounded JOIN over all citing ops.
_MAX_EVIDENCE_RESULTS = 200

_MIGRATION_FILE_PATH = """
ALTER TABLE operations ADD COLUMN file_path TEXT;
CREATE INDEX IF NOT EXISTS idx_operations_file_path ON operations(file_path);
CREATE INDEX IF NOT EXISTS idx_operations_file_path_seq ON operations(file_path, commit_seq DESC);
"""


class Database:
    """Thread-safe SQLite wrapper for HGP."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: sqlite3.Connection | None = None

    def initialize(self) -> None:
        """Open connection and apply schema."""
        # isolation_level=None = autocommit mode: required for manual BEGIN IMMEDIATE.
        # Without this, Python sqlite3 auto-begins deferred transactions that conflict
        # with explicit BEGIN IMMEDIATE calls.
        self._conn = sqlite3.connect(
            str(self._path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA_SQL)
        self._apply_migrations()
        # executescript() issues an implicit COMMIT; re-assert per-connection PRAGMA.
        self._conn.execute("PRAGMA foreign_keys = ON")
        # V2 migration: add memory tier columns to existing DBs (each column guarded independently)
        existing_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(operations)").fetchall()}
        if "access_count" not in existing_cols:
            self._conn.execute("ALTER TABLE operations ADD COLUMN access_count REAL NOT NULL DEFAULT 0.0")
        if "last_accessed" not in existing_cols:
            self._conn.execute("ALTER TABLE operations ADD COLUMN last_accessed TEXT")
        if "memory_tier" not in existing_cols:
            self._conn.execute(
                "ALTER TABLE operations ADD COLUMN memory_tier TEXT NOT NULL DEFAULT 'long_term'"
                " CHECK (memory_tier IN ('short_term', 'long_term', 'inactive'))"
            )
        self._apply_migrations()

    def _apply_migrations(self) -> None:
        """Apply pending schema migrations. Safe to call on fresh and existing DBs."""
        assert self._conn
        self._conn.executescript(
            "CREATE TABLE IF NOT EXISTS _hgp_migrations "
            "(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);"
        )
        applied = {
            r[0] for r in self._conn.execute(
                "SELECT name FROM _hgp_migrations"
            ).fetchall()
        }
        if "v4_file_path" not in applied:
            cols = [
                r[1] for r in self._conn.execute(
                    "PRAGMA table_info(operations)"
                ).fetchall()
            ]
            if "file_path" not in cols:
                self._conn.executescript(_MIGRATION_FILE_PATH)
            self._conn.execute(
                "INSERT OR IGNORE INTO _hgp_migrations (name) VALUES (?)",
                ("v4_file_path",),
            )

    def _apply_migrations(self) -> None:
        """Apply pending schema migrations. Safe to call on fresh and existing DBs."""
        assert self._conn
        self._conn.executescript(
            "CREATE TABLE IF NOT EXISTS _hgp_migrations "
            "(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);"
        )
        applied = {
            r[0] for r in self._conn.execute(
                "SELECT name FROM _hgp_migrations"
            ).fetchall()
        }
        if "v4_file_path" not in applied:
            cols = [
                r[1] for r in self._conn.execute(
                    "PRAGMA table_info(operations)"
                ).fetchall()
            ]
            if "file_path" not in cols:
                self._conn.executescript(_MIGRATION_FILE_PATH)
            self._conn.execute(
                "INSERT OR IGNORE INTO _hgp_migrations (name) VALUES (?)",
                ("v4_file_path",),
            )

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def execute(self, sql: str, params: tuple[Any, ...] | dict[str, Any] = ()) -> sqlite3.Cursor:
        assert self._conn, "Database not initialized"
        return self._conn.execute(sql, params)

    def next_commit_seq(self) -> int:
        """Atomically increment and return the next commit sequence number.
        Must be called inside a BEGIN IMMEDIATE transaction."""
        assert self._conn
        self._conn.execute(
            "UPDATE commit_counter SET next_seq = next_seq + 1 WHERE id = 1"
        )
        row = self._conn.execute(
            "SELECT next_seq - 1 FROM commit_counter WHERE id = 1"
        ).fetchone()
        return int(row[0])

    def insert_operation(
        self,
        op_id: str,
        op_type: str,
        agent_id: str,
        commit_seq: int,
        chain_hash: str,
        object_hash: str | None = None,
        metadata: str | None = None,
        mime_type: str | None = None,
        file_path: str | None = None,
    ) -> None:
        assert self._conn
        # Insert into objects FIRST to satisfy the FK constraint on operations.object_hash
        if object_hash is not None:
            self._conn.execute(
                "INSERT OR IGNORE INTO objects (hash, size, mime_type) VALUES (?, 0, ?)",
                (object_hash, mime_type),
            )
        self._conn.execute(
            """
            INSERT INTO operations
                (op_id, op_type, status, commit_seq, agent_id, object_hash, chain_hash, metadata, completed_at, file_path)
            VALUES (?, ?, 'COMPLETED', ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), ?)
            """,
            (op_id, op_type, commit_seq, agent_id, object_hash, chain_hash, metadata, file_path),
        )

    def get_operation(self, op_id: str) -> dict[str, Any] | None:
        assert self._conn
        row = self._conn.execute(
            "SELECT * FROM operations WHERE op_id = ?", (op_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_ops_by_file_path(
        self,
        file_path: str,
        limit: int = 50,
    ) -> list[sqlite3.Row]:
        """Return operations for a given file_path, ordered by commit_seq DESC."""
        assert self._conn
        return self._conn.execute(
            "SELECT * FROM operations WHERE file_path = ? "
            "ORDER BY commit_seq DESC LIMIT ?",
            (file_path, limit),
        ).fetchall()

    def insert_edge(self, child_op_id: str, parent_op_id: str, edge_type: str = "causal") -> None:
        assert self._conn
        self._conn.execute(
            "INSERT OR IGNORE INTO op_edges (child_op_id, parent_op_id, edge_type) VALUES (?, ?, ?)",
            (child_op_id, parent_op_id, edge_type),
        )

    def commit(self) -> None:
        assert self._conn
        try:
            self._conn.execute("COMMIT")
        except sqlite3.OperationalError as exc:
            # Suppress only "no active transaction" (autocommit mode).
            # Re-raise real I/O failures (SQLITE_FULL, SQLITE_IOERR, etc.)
            # which also arrive as OperationalError but with different messages.
            if "no transaction" not in str(exc).lower():
                raise

    def rollback(self) -> None:
        assert self._conn
        try:
            self._conn.execute("ROLLBACK")
        except sqlite3.OperationalError as exc:
            # Suppress only "no active transaction" (autocommit mode).
            if "no transaction" not in str(exc).lower():
                raise

    def begin_immediate(self) -> None:
        assert self._conn
        self._conn.execute("BEGIN IMMEDIATE")

    def begin_deferred(self) -> None:
        assert self._conn
        self._conn.execute("BEGIN DEFERRED")

    def query_operations(
        self,
        status: str | None = None,
        agent_id: str | None = None,
        op_type: str | None = None,
        since_commit_seq: int | None = None,
        include_inactive: bool = False,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        assert self._conn
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if agent_id:
            clauses.append("agent_id = ?")
            params.append(agent_id)
        if op_type:
            clauses.append("op_type = ?")
            params.append(op_type)
        if since_commit_seq is not None:
            clauses.append("commit_seq > ?")
            params.append(since_commit_seq)
        if not include_inactive:
            clauses.append("memory_tier != 'inactive'")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order = """ORDER BY CASE memory_tier
            WHEN 'short_term' THEN 1
            WHEN 'long_term'  THEN 2
            WHEN 'inactive'   THEN 3
            ELSE 4
        END, commit_seq DESC"""
        params.append(limit)
        rows = self._conn.execute(
            f"SELECT * FROM operations {where} {order} LIMIT ?", params
        ).fetchall()
        return [dict(r) for r in rows]

    def object_referenced(self, obj_hash: str) -> bool:
        """Check if an object hash is referenced in the operations table."""
        assert self._conn
        row = self._conn.execute(
            "SELECT 1 FROM operations WHERE object_hash = ? LIMIT 1", (obj_hash,)
        ).fetchone()
        return row is not None

    def upsert_object_status(self, obj_hash: str, status: str) -> None:
        assert self._conn
        self._conn.execute(
            """
            INSERT INTO objects (hash, size, status)
            VALUES (?, 0, ?)
            ON CONFLICT(hash) DO UPDATE SET status = excluded.status
            """,
            (obj_hash, status),
        )

    def update_operation_status(self, op_id: str, status: str) -> None:
        assert self._conn
        self._conn.execute(
            "UPDATE operations SET status = ? WHERE op_id = ?", (status, op_id)
        )

    def record_access(self, op_id: str, weight: float = 1.0) -> None:
        """Increment access counter; update last_accessed only for weight >= 0.4 (depth 0-2)."""
        assert self._conn
        if weight >= 0.4:
            self._conn.execute(
                """UPDATE operations
                   SET access_count = access_count + ?,
                       last_accessed = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                       memory_tier = CASE WHEN memory_tier = 'inactive' THEN 'long_term'
                                         ELSE memory_tier END
                   WHERE op_id = ?""",
                (weight, op_id),
            )
        else:
            self._conn.execute(
                "UPDATE operations SET access_count = access_count + ? WHERE op_id = ?",
                (weight, op_id),
            )

    def set_memory_tier(self, op_id: str, tier: str) -> None:
        assert self._conn
        self._conn.execute(
            "UPDATE operations SET memory_tier = ? WHERE op_id = ?", (tier, op_id)
        )

    def demote_inactive(self, threshold_days: int = 30) -> int:
        """Demote long_term ops to inactive using relative project_pulse baseline.

        inactive if: project_pulse - COALESCE(last_accessed, created_at) > threshold_days
        project_pulse = MAX(last_accessed) or MAX(created_at) if nothing ever accessed.
        """
        assert self._conn
        pulse_row = self._conn.execute(
            "SELECT MAX(COALESCE(last_accessed, created_at)) FROM operations"
        ).fetchone()
        if not pulse_row or pulse_row[0] is None:
            return 0
        project_pulse = pulse_row[0]
        cur = self._conn.execute(
            """UPDATE operations
               SET memory_tier = 'inactive'
               WHERE memory_tier = 'long_term'
                 AND (julianday(?) - julianday(COALESCE(last_accessed, created_at))) > ?""",
            (project_pulse, threshold_days),
        )
        return cur.rowcount

    def expire_leases(self) -> int:
        """Mark all past-expiry ACTIVE leases as EXPIRED. Returns count."""
        assert self._conn
        cur = self._conn.execute(
            """
            UPDATE leases SET status = 'EXPIRED'
            WHERE status = 'ACTIVE'
              AND expires_at < strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            """
        )
        # Demote short_term ops whose only active lease just expired
        # NOT EXISTS is used instead of NOT IN to avoid NULL-sensitivity issues
        self._conn.execute(
            """UPDATE operations SET memory_tier = 'long_term'
               WHERE memory_tier = 'short_term'
                 AND NOT EXISTS (
                     SELECT 1 FROM leases
                     WHERE leases.subgraph_root_op_id = operations.op_id
                       AND leases.status = 'ACTIVE'
                 )"""
        )
        return cur.rowcount

    def insert_evidence(self, citing_op_id: str, refs: list[EvidenceRef]) -> None:
        """Insert evidence rows. Must be called inside an existing transaction."""
        assert self._conn
        # Pre-flight duplicate check: raises ValueError before any SQL so the caller
        # receives a clean message rather than a raw sqlite3.IntegrityError.
        seen: set[str] = set()
        for ref in refs:
            if ref.op_id in seen:
                raise ValueError(f"duplicate cited_op_id in refs: {ref.op_id!r}")
            seen.add(ref.op_id)
        for ref in refs:
            if ref.op_id == citing_op_id:
                raise ValueError(f"self-reference: citing_op_id == cited_op_id == {ref.op_id!r}")
            exists = self._conn.execute(
                "SELECT 1 FROM operations WHERE op_id = ?", (ref.op_id,)
            ).fetchone()
            if not exists:
                raise ValueError(f"cited_op_id not found: {ref.op_id!r}")
            self._conn.execute(
                """INSERT INTO op_evidence
                   (citing_op_id, cited_op_id, relation, scope, inference)
                   VALUES (?, ?, ?, ?, ?)""",
                (citing_op_id, ref.op_id, str(ref.relation), ref.scope, ref.inference),
            )

    def get_evidence(self, op_id: str, max_results: int = _MAX_EVIDENCE_RESULTS) -> list[dict[str, Any]]:
        """Return ops cited by op_id (up to max_results), recording access at flat weight=0.7 (best-effort)."""
        assert self._conn
        max_results = max(1, min(max_results, _MAX_EVIDENCE_RESULTS))  # clamp: 1 ≤ n ≤ cap
        rows = self._conn.execute(
            """SELECT e.cited_op_id, o.op_type, o.status, o.memory_tier,
                      e.relation, e.scope, e.inference, e.created_at
               FROM op_evidence e
               JOIN operations o ON o.op_id = e.cited_op_id
               WHERE e.citing_op_id = :op_id
               LIMIT :max_results""",
            {"op_id": op_id, "max_results": max_results},
        ).fetchall()
        try:
            self.record_access(op_id, weight=1.0)
            for row in rows:
                self.record_access(row["cited_op_id"], weight=0.7)
        except sqlite3.OperationalError:
            pass  # expected: write-lock contention when another writer holds BEGIN IMMEDIATE
        except sqlite3.Error as exc:
            _log.warning("record_access failed unexpectedly in get_evidence op_id=%r: %s", op_id, exc)
        return [dict(r) for r in rows]

    def get_citing_ops(self, op_id: str, max_results: int = _MAX_EVIDENCE_RESULTS) -> list[dict[str, Any]]:
        """Return ops that cited op_id (up to max_results), recording access on cited op only (best-effort)."""
        assert self._conn
        max_results = max(1, min(max_results, _MAX_EVIDENCE_RESULTS))  # clamp: 1 ≤ n ≤ cap
        rows = self._conn.execute(
            """SELECT e.citing_op_id, o.op_type, o.status, o.memory_tier,
                      e.relation, e.scope, e.inference, e.created_at
               FROM op_evidence e
               JOIN operations o ON o.op_id = e.citing_op_id
               WHERE e.cited_op_id = :op_id
               LIMIT :max_results""",
            {"op_id": op_id, "max_results": max_results},
        ).fetchall()
        try:
            self.record_access(op_id, weight=1.0)
        except sqlite3.OperationalError:
            pass  # expected: write-lock contention when another writer holds BEGIN IMMEDIATE
        except sqlite3.Error as exc:
            _log.warning("record_access failed unexpectedly in get_citing_ops op_id=%r: %s", op_id, exc)
        return [dict(r) for r in rows]
