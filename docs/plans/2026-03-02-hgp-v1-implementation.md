# HGP V1 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement the History Graph Protocol MCP server — a crash-resilient semantic layer over MCP that tracks causal history of multi-agent workflows using SQLite + CAS.

**Architecture:** Python 3.12 MCP server (FastMCP) with stdlib `sqlite3` (WAL + BEGIN IMMEDIATE), a SHA-256 content-addressable blob store with 5-step crash-safe write path, and a DAG-based operation tracker with Subgraph CAS (`chain_hash`) for optimistic concurrency control. All design decisions are captured in `HGP_Technical_Design.md` v0.2.0.

**Tech Stack:** Python 3.12+, `mcp[cli]` (FastMCP), `pydantic>=2`, `uv`, `pytest`, `pytest-asyncio`, `pyright` (strict)

> **Plan Version:** 0.2.0 — 3-Way Approved (Claude + ChatGPT + Gemini)
> **Key fixes:** CAS fsync uses write fd, sqlite3 isolation_level=None, merge chain_hash uses all parents, returned chain_hash is post-insert, lease validate in locked TX

---

## Task 0: Project Scaffold

**Files:**
- Create: `pyproject.toml`
- Create: `src/hgp/__init__.py`
- Create: `src/hgp/errors.py`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`
- Create: `.gitignore`

**Step 1: Initialize project with uv**

```bash
cd /home/woogwangsim/git_repos/history-graph-protocol
uv init --name hgp --python 3.12
# Remove the auto-generated hello.py if any
rm -f hello.py
```

**Step 2: Create pyproject.toml**

```toml
[project]
name = "hgp"
version = "0.2.0"
requires-python = ">=3.12"
dependencies = [
    "mcp[cli]>=1.0.0",
    "pydantic>=2.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.24",
    "pyright>=1.1",
    "ruff>=0.8",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/hgp"]

[tool.pyright]
typeCheckingMode = "strict"
pythonVersion = "3.12"

[tool.ruff]
target-version = "py312"
line-length = 100

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.ruff.lint]
select = ["E", "F", "I"]
```

**Step 3: Install dependencies**

```bash
uv sync --all-extras
```

Expected: lockfile created, venv populated.

**Step 4: Create src/hgp/__init__.py**

```python
"""History Graph Protocol — crash-resilient semantic layer over MCP."""

__version__ = "0.2.0"
```

**Step 5: Create src/hgp/errors.py**

```python
"""HGP error types."""

from __future__ import annotations


class HGPError(Exception):
    """Base HGP error."""
    code: str = "HGP_ERROR"


class ChainStaleError(HGPError):
    """Subgraph mutated concurrently — chain_hash mismatch."""
    code = "CHAIN_STALE"


class LeaseExpiredError(HGPError):
    """Lease token has expired."""
    code = "LEASE_EXPIRED"


class ParentNotFoundError(HGPError):
    """Referenced parent operation does not exist."""
    code = "PARENT_NOT_FOUND"


class BlobWriteError(HGPError):
    """CAS blob write failed (fsync/rename)."""
    code = "BLOB_WRITE_FAILED"


class InvalidHashError(HGPError):
    """Provided hash does not match computed hash."""
    code = "INVALID_HASH"


class PayloadTooLargeError(HGPError):
    """Payload exceeds 10 MB V1 limit."""
    code = "PAYLOAD_TOO_LARGE"
```

**Step 6: Create tests/conftest.py**

```python
"""Shared test fixtures."""

from __future__ import annotations

import pytest
import tempfile
from pathlib import Path


@pytest.fixture
def tmp_dir(tmp_path: Path) -> Path:
    """Temporary directory for test isolation."""
    return tmp_path


@pytest.fixture
def hgp_dirs(tmp_path: Path) -> dict[str, Path]:
    """Create HGP directory structure."""
    content_dir = tmp_path / ".hgp_content"
    staging_dir = content_dir / ".staging"
    content_dir.mkdir()
    staging_dir.mkdir()
    db_path = tmp_path / "hgp.db"
    return {
        "root": tmp_path,
        "content_dir": content_dir,
        "staging_dir": staging_dir,
        "db_path": db_path,
    }
```

**Step 7: Create .gitignore**

```gitignore
__pycache__/
*.pyc
.venv/
dist/
.hgp_content/
*.db
*.db-wal
*.db-shm
.mypy_cache/
.ruff_cache/
```

**Step 8: Commit**

```bash
git init
git add pyproject.toml src/ tests/ .gitignore docs/
git commit -m "feat: initial project scaffold with pyproject.toml and error types"
```

---

## Task 1: Core Models (Pydantic)

**Files:**
- Create: `src/hgp/models.py`
- Create: `tests/test_models.py`

**Step 1: Write the failing test**

```python
# tests/test_models.py
from hgp.models import (
    Operation, OpEdge, StoredObject, Lease, GitAnchor,
    OpType, OpStatus, EdgeType, LeaseStatus, ObjectStatus,
)
import uuid
from datetime import datetime, timedelta


def test_operation_defaults():
    op = Operation(op_type=OpType.ARTIFACT, agent_id="agent-1")
    assert op.status == OpStatus.COMPLETED  # Will fail until implemented
    assert op.commit_seq is None
    assert uuid.UUID(op.op_id)  # Valid UUID


def test_lease_model():
    now = datetime.utcnow()
    lease = Lease(
        agent_id="agent-1",
        subgraph_root_op_id=str(uuid.uuid4()),
        chain_hash="sha256:abc",
        expires_at=now + timedelta(minutes=5),
    )
    assert lease.status == LeaseStatus.ACTIVE
    assert uuid.UUID(lease.lease_id)
```

**Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_models.py -v
```

Expected: `ImportError: No module named 'hgp.models'`

**Step 3: Create src/hgp/models.py**

```python
"""HGP Core Types — Pydantic models for internal and MCP interface use."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class OpType(StrEnum):
    ARTIFACT = "artifact"
    HYPOTHESIS = "hypothesis"
    MERGE = "merge"
    INVALIDATION = "invalidation"


class OpStatus(StrEnum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    INVALIDATED = "INVALIDATED"
    MISSING_BLOB = "MISSING_BLOB"


class EdgeType(StrEnum):
    CAUSAL = "causal"
    INVALIDATES = "invalidates"


class LeaseStatus(StrEnum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    RELEASED = "RELEASED"


class ObjectStatus(StrEnum):
    VALID = "VALID"
    MISSING_BLOB = "MISSING_BLOB"
    ORPHAN_CANDIDATE = "ORPHAN_CANDIDATE"


class Operation(BaseModel):
    op_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    op_type: OpType
    status: OpStatus = OpStatus.COMPLETED
    commit_seq: int | None = None
    agent_id: str
    object_hash: str | None = None
    chain_hash: str | None = None
    metadata: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: datetime | None = None


class OpEdge(BaseModel):
    child_op_id: str
    parent_op_id: str
    edge_type: EdgeType = EdgeType.CAUSAL


class StoredObject(BaseModel):
    hash: str  # "sha256:<hex>"
    size: int
    mime_type: str | None = None
    status: ObjectStatus = ObjectStatus.VALID
    created_at: datetime = Field(default_factory=datetime.utcnow)
    gc_marked_at: datetime | None = None


class Lease(BaseModel):
    lease_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    agent_id: str
    subgraph_root_op_id: str
    chain_hash: str
    issued_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: datetime
    status: LeaseStatus = LeaseStatus.ACTIVE


class GitAnchor(BaseModel):
    op_id: str
    git_commit_sha: str  # 40-char hex
    repository: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ReconcileReport(BaseModel):
    missing_blobs: list[str] = Field(default_factory=list)
    orphan_candidates: list[str] = Field(default_factory=list)
    staging_cleaned: int = 0
    skipped_young_blobs: int = 0
    errors: list[str] = Field(default_factory=list)
```

**Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_models.py -v
```

Expected: all PASS

**Step 5: Commit**

```bash
git add src/hgp/models.py tests/test_models.py
git commit -m "feat: add Pydantic core models"
```

---

## Task 2: SQLite Database Layer

**Files:**
- Create: `src/hgp/db.py`
- Create: `tests/test_db.py`

**Step 1: Write failing tests**

```python
# tests/test_db.py
from __future__ import annotations

import pytest
from pathlib import Path
from hgp.db import Database
from hgp.models import OpType, OpStatus


def test_schema_creation(hgp_dirs: dict):
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    # Verify tables exist
    tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    table_names = {row[0] for row in tables}
    assert "operations" in table_names
    assert "op_edges" in table_names
    assert "objects" in table_names
    assert "leases" in table_names
    assert "commit_counter" in table_names
    assert "git_anchors" in table_names


def test_insert_and_query_operation(hgp_dirs: dict):
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    op_id = "test-op-001"
    db.insert_operation(
        op_id=op_id,
        op_type=OpType.ARTIFACT,
        agent_id="agent-1",
        commit_seq=1,
        chain_hash="sha256:abc",
    )
    op = db.get_operation(op_id)
    assert op is not None
    assert op["status"] == OpStatus.COMPLETED
    assert op["commit_seq"] == 1


def test_commit_counter_increments(hgp_dirs: dict):
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    seq1 = db.next_commit_seq()
    seq2 = db.next_commit_seq()
    assert seq2 == seq1 + 1


def test_wal_mode_enabled(hgp_dirs: dict):
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    row = db.execute("PRAGMA journal_mode").fetchone()
    assert row[0] == "wal"
```

**Step 2: Run to confirm failure**

```bash
uv run pytest tests/test_db.py -v
```

Expected: `ImportError: No module named 'hgp.db'`

**Step 3: Create src/hgp/db.py**

```python
"""SQLite database layer for HGP."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

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
    FOREIGN KEY (object_hash) REFERENCES objects(hash)
);

CREATE INDEX IF NOT EXISTS idx_operations_agent  ON operations(agent_id);
CREATE INDEX IF NOT EXISTS idx_operations_type   ON operations(op_type);
CREATE INDEX IF NOT EXISTS idx_operations_status ON operations(status);
CREATE INDEX IF NOT EXISTS idx_operations_seq    ON operations(commit_seq);

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

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
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
    ) -> None:
        assert self._conn
        self._conn.execute(
            """
            INSERT INTO operations
                (op_id, op_type, status, commit_seq, agent_id, object_hash, chain_hash, metadata, completed_at)
            VALUES (?, ?, 'COMPLETED', ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            """,
            (op_id, op_type, commit_seq, agent_id, object_hash, chain_hash, metadata),
        )
        if object_hash:
            self._conn.execute(
                "INSERT OR IGNORE INTO objects (hash, size, mime_type) VALUES (?, 0, ?)",
                (object_hash, mime_type),
            )

    def get_operation(self, op_id: str) -> dict[str, Any] | None:
        assert self._conn
        row = self._conn.execute(
            "SELECT * FROM operations WHERE op_id = ?", (op_id,)
        ).fetchone()
        return dict(row) if row else None

    def insert_edge(self, child_op_id: str, parent_op_id: str, edge_type: str = "causal") -> None:
        assert self._conn
        self._conn.execute(
            "INSERT OR IGNORE INTO op_edges (child_op_id, parent_op_id, edge_type) VALUES (?, ?, ?)",
            (child_op_id, parent_op_id, edge_type),
        )

    def commit(self) -> None:
        assert self._conn
        self._conn.commit()

    def rollback(self) -> None:
        assert self._conn
        self._conn.rollback()

    def begin_immediate(self) -> None:
        assert self._conn
        self._conn.execute("BEGIN IMMEDIATE")

    def query_operations(
        self,
        status: str | None = None,
        agent_id: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        assert self._conn
        clauses = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if agent_id:
            clauses.append("agent_id = ?")
            params.append(agent_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self._conn.execute(
            f"SELECT * FROM operations {where} LIMIT ?", params
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
        return cur.rowcount
```

**Step 4: Run tests**

```bash
uv run pytest tests/test_db.py -v
```

Expected: all PASS

**Step 5: Commit**

```bash
git add src/hgp/db.py tests/test_db.py
git commit -m "feat: add SQLite database layer with WAL, schema, and core queries"
```

---

## Task 3: Content-Addressable Storage (CAS)

**Files:**
- Create: `src/hgp/cas.py`
- Create: `tests/test_cas.py`

**Step 1: Write failing tests**

```python
# tests/test_cas.py
from __future__ import annotations

import hashlib
import pytest
from pathlib import Path
from hgp.cas import CAS
from hgp.errors import PayloadTooLargeError


def test_store_and_read(hgp_dirs: dict):
    cas = CAS(hgp_dirs["content_dir"])
    payload = b"hello, world"
    obj_hash = cas.store(payload)
    assert obj_hash.startswith("sha256:")
    assert cas.read(obj_hash) == payload


def test_deduplication(hgp_dirs: dict):
    cas = CAS(hgp_dirs["content_dir"])
    payload = b"duplicate content"
    h1 = cas.store(payload)
    h2 = cas.store(payload)
    assert h1 == h2
    # Only one file on disk
    hex_hash = h1.removeprefix("sha256:")
    matches = list(hgp_dirs["content_dir"].rglob(hex_hash[2:]))
    assert len(matches) == 1


def test_hash_correctness(hgp_dirs: dict):
    cas = CAS(hgp_dirs["content_dir"])
    payload = b"test content"
    obj_hash = cas.store(payload)
    expected = "sha256:" + hashlib.sha256(payload).hexdigest()
    assert obj_hash == expected


def test_missing_blob_returns_none(hgp_dirs: dict):
    cas = CAS(hgp_dirs["content_dir"])
    result = cas.read("sha256:" + "a" * 64)
    assert result is None


def test_blob_exists(hgp_dirs: dict):
    cas = CAS(hgp_dirs["content_dir"])
    payload = b"exists test"
    obj_hash = cas.store(payload)
    assert cas.exists(obj_hash)
    assert not cas.exists("sha256:" + "b" * 64)


def test_payload_too_large(hgp_dirs: dict):
    cas = CAS(hgp_dirs["content_dir"])
    large = b"x" * (11 * 1024 * 1024)  # 11 MB
    with pytest.raises(PayloadTooLargeError):
        cas.store(large)


def test_list_all_blobs_with_mtime(hgp_dirs: dict):
    cas = CAS(hgp_dirs["content_dir"])
    cas.store(b"first")
    cas.store(b"second")
    blobs = list(cas.list_all_blobs_with_mtime())
    assert len(blobs) == 2
    assert all(h.startswith("sha256:") for h, _ in blobs)
```

**Step 2: Run to confirm failure**

```bash
uv run pytest tests/test_cas.py -v
```

Expected: `ImportError: No module named 'hgp.cas'`

**Step 3: Create src/hgp/cas.py**

```python
"""Content-Addressable Storage for HGP blobs."""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Iterator

from hgp.errors import PayloadTooLargeError

MAX_PAYLOAD_BYTES = 10 * 1024 * 1024  # 10 MB V1 limit


class CAS:
    """WORM Content-Addressable blob store with 5-step crash-safe write path."""

    def __init__(self, content_dir: Path) -> None:
        self._content_dir = content_dir
        self._staging_dir = content_dir / ".staging"
        self._staging_dir.mkdir(parents=True, exist_ok=True)

    def store(self, payload: bytes) -> str:
        """Store payload, return 'sha256:<hex>'. Idempotent (WORM)."""
        if len(payload) > MAX_PAYLOAD_BYTES:
            raise PayloadTooLargeError(
                f"Payload {len(payload)} bytes exceeds {MAX_PAYLOAD_BYTES} byte limit"
            )

        # Step 1: Compute hash
        hex_hash = hashlib.sha256(payload).hexdigest()
        object_key = f"sha256:{hex_hash}"
        final_dir = self._content_dir / hex_hash[:2]
        final_path = final_dir / hex_hash[2:]

        # Fast path: already exists (deduplication)
        if final_path.exists():
            return object_key

        final_dir.mkdir(parents=True, exist_ok=True)
        staging_path = self._staging_dir / f"{uuid.uuid4()}.tmp"

        # Step 2: Write to staging + fsync file
        # IMPORTANT: fsync must be called on the write fd, not a re-opened O_RDONLY fd.
        # write_bytes() closes the fd; we open with O_WRONLY to get a durable fsync.
        with open(staging_path, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())

        # Step 3: Atomic rename
        try:
            os.rename(str(staging_path), str(final_path))
        except OSError:
            if final_path.exists():
                # Concurrent writer produced the same hash — idempotent success
                staging_path.unlink(missing_ok=True)
                return object_key
            raise

        # Step 4: fsync source and destination directories
        for dir_path in [self._staging_dir, final_dir]:
            dfd = os.open(str(dir_path), os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)

        return object_key

    def read(self, object_hash: str) -> bytes | None:
        """Read blob by hash. Returns None if missing."""
        path = self._hash_to_path(object_hash)
        if path.exists():
            return path.read_bytes()
        return None

    def exists(self, object_hash: str) -> bool:
        return self._hash_to_path(object_hash).exists()

    def list_all_blobs_with_mtime(self) -> Iterator[tuple[str, datetime]]:
        """Yield (object_hash, mtime) for all stored blobs."""
        for subdir in self._content_dir.iterdir():
            if subdir.name.startswith(".") or not subdir.is_dir():
                continue
            for blob_file in subdir.iterdir():
                if blob_file.is_file():
                    hex_hash = subdir.name + blob_file.name
                    mtime = datetime.fromtimestamp(blob_file.stat().st_mtime)
                    yield f"sha256:{hex_hash}", mtime

    def _hash_to_path(self, object_hash: str) -> Path:
        hex_hash = object_hash.removeprefix("sha256:")
        return self._content_dir / hex_hash[:2] / hex_hash[2:]
```

**Step 4: Run tests**

```bash
uv run pytest tests/test_cas.py -v
```

Expected: all PASS

**Step 5: Commit**

```bash
git add src/hgp/cas.py tests/test_cas.py
git commit -m "feat: add CAS with 5-step crash-safe write path and deduplication"
```

---

## Task 4: DAG Operations + chain_hash

**Files:**
- Create: `src/hgp/dag.py`
- Create: `tests/test_dag.py`

**Step 1: Write failing tests**

```python
# tests/test_dag.py
from __future__ import annotations

import pytest
from pathlib import Path
from hgp.db import Database
from hgp.dag import compute_chain_hash, get_ancestors, get_descendants


def _make_db(hgp_dirs: dict) -> Database:
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    return db


def test_chain_hash_single_node(hgp_dirs: dict):
    db = _make_db(hgp_dirs)
    db.begin_immediate()
    seq = db.next_commit_seq()
    db.insert_operation("op-1", "artifact", "agent-1", seq, "sha256:placeholder")
    db.commit()
    h = compute_chain_hash(db, "op-1")
    assert h.startswith("sha256:")


def test_chain_hash_includes_edges(hgp_dirs: dict):
    """Two DAGs with same nodes but different edges must have different hashes."""
    db = _make_db(hgp_dirs)
    db.begin_immediate()
    db.insert_operation("op-a", "artifact", "agent-1", 1, "sha256:aa")
    db.insert_operation("op-b", "artifact", "agent-1", 2, "sha256:bb")
    db.commit()

    # DAG 1: op-b is child of op-a (op-a → op-b)
    db.begin_immediate()
    db.insert_edge("op-b", "op-a", "causal")
    db.commit()
    hash_dag1 = compute_chain_hash(db, "op-b")

    # Remove edge and create reverse
    db.execute("DELETE FROM op_edges WHERE child_op_id='op-b' AND parent_op_id='op-a'")
    db.commit()
    db.begin_immediate()
    db.insert_edge("op-a", "op-b", "causal")
    db.commit()
    hash_dag2 = compute_chain_hash(db, "op-a")

    assert hash_dag1 != hash_dag2


def test_chain_hash_changes_on_status_change(hgp_dirs: dict):
    db = _make_db(hgp_dirs)
    db.begin_immediate()
    db.insert_operation("op-x", "artifact", "agent-1", 1, "sha256:xx")
    db.commit()
    h1 = compute_chain_hash(db, "op-x")
    db.update_operation_status("op-x", "INVALIDATED")
    db.commit()
    h2 = compute_chain_hash(db, "op-x")
    assert h1 != h2


def test_chain_hash_deterministic(hgp_dirs: dict):
    db = _make_db(hgp_dirs)
    db.begin_immediate()
    db.insert_operation("op-d", "artifact", "agent-1", 1, "sha256:dd")
    db.commit()
    h1 = compute_chain_hash(db, "op-d")
    h2 = compute_chain_hash(db, "op-d")
    assert h1 == h2


def test_chain_hash_merge_two_parents(hgp_dirs: dict):
    """Merge op: chain_hash must reflect BOTH parent branches."""
    db = _make_db(hgp_dirs)
    db.begin_immediate()
    db.insert_operation("branch-a", "artifact", "agent-1", 1, "sha256:aa")
    db.insert_operation("branch-b", "artifact", "agent-2", 2, "sha256:bb")
    db.insert_operation("merge", "merge", "agent-1", 3, "sha256:mm")
    db.insert_edge("merge", "branch-a", "causal")
    db.insert_edge("merge", "branch-b", "causal")
    db.commit()

    # Mutating branch-a should change merge's chain_hash
    h_before = compute_chain_hash(db, "merge")
    db.update_operation_status("branch-a", "INVALIDATED")
    db.commit()
    h_after = compute_chain_hash(db, "merge")
    assert h_before != h_after


def test_get_ancestors(hgp_dirs: dict):
    db = _make_db(hgp_dirs)
    db.begin_immediate()
    db.insert_operation("root", "artifact", "agent-1", 1, "sha256:r")
    db.insert_operation("mid", "artifact", "agent-1", 2, "sha256:m")
    db.insert_operation("leaf", "artifact", "agent-1", 3, "sha256:l")
    db.insert_edge("mid", "root")
    db.insert_edge("leaf", "mid")
    db.commit()
    ancestors = get_ancestors(db, "leaf")
    assert {a["op_id"] for a in ancestors} == {"leaf", "mid", "root"}
```

**Step 2: Run to confirm failure**

```bash
uv run pytest tests/test_dag.py -v
```

Expected: `ImportError: No module named 'hgp.dag'`

**Step 3: Create src/hgp/dag.py**

```python
"""DAG traversal and chain_hash computation."""

from __future__ import annotations

import hashlib
from typing import Any

from hgp.db import Database

_ANCESTOR_SQL = """
WITH RECURSIVE ancestors(op_id) AS (
    SELECT :root_op_id
    UNION
    SELECT e.parent_op_id
    FROM op_edges e
    JOIN ancestors a ON e.child_op_id = a.op_id
)
SELECT o.op_id, o.status, o.commit_seq
FROM operations o
JOIN ancestors a ON o.op_id = a.op_id
ORDER BY o.op_id
"""

_EDGES_IN_SUBGRAPH_SQL = """
WITH RECURSIVE ancestors(op_id) AS (
    SELECT :root_op_id
    UNION
    SELECT e.parent_op_id
    FROM op_edges e
    JOIN ancestors a ON e.child_op_id = a.op_id
)
SELECT e.child_op_id, e.parent_op_id, e.edge_type
FROM op_edges e
WHERE e.child_op_id IN (SELECT op_id FROM ancestors)
  AND e.parent_op_id IN (SELECT op_id FROM ancestors)
ORDER BY e.child_op_id, e.parent_op_id
"""

_DESCENDANTS_SQL = """
WITH RECURSIVE descendants(op_id) AS (
    SELECT :root_op_id
    UNION
    SELECT e.child_op_id
    FROM op_edges e
    JOIN descendants d ON e.parent_op_id = d.op_id
)
SELECT o.*
FROM operations o
JOIN descendants d ON o.op_id = d.op_id
ORDER BY o.commit_seq
"""


def compute_chain_hash(db: Database, root_op_id: str) -> str:
    """Compute the chain_hash for a subgraph rooted at root_op_id.

    Includes both operations (nodes) and edges for structural sensitivity.
    Always traverses the full ancestor graph — no depth limit.
    """
    ops = db.execute(_ANCESTOR_SQL, {"root_op_id": root_op_id}).fetchall()
    edges = db.execute(_EDGES_IN_SUBGRAPH_SQL, {"root_op_id": root_op_id}).fetchall()

    ops_part = "|".join(
        f"{row['op_id']}:{row['status']}:{row['commit_seq']}" for row in ops
    )
    edges_part = "|".join(
        f"{row['child_op_id']}>{row['parent_op_id']}:{row['edge_type']}" for row in edges
    )
    canonical = f"OPS[{ops_part}]EDGES[{edges_part}]"
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def get_ancestors(db: Database, root_op_id: str) -> list[dict[str, Any]]:
    """Return all ancestor operations including root, sorted by op_id."""
    rows = db.execute(_ANCESTOR_SQL, {"root_op_id": root_op_id}).fetchall()
    return [dict(r) for r in rows]


def get_descendants(db: Database, root_op_id: str) -> list[dict[str, Any]]:
    """Return all descendant operations including root, sorted by commit_seq."""
    rows = db.execute(_DESCENDANTS_SQL, {"root_op_id": root_op_id}).fetchall()
    return [dict(r) for r in rows]
```

**Note:** The `_ANCESTOR_SQL` and `_EDGES_IN_SUBGRAPH_SQL` use SQLite named parameters (`:root_op_id`). Pass as a dict: `db.execute(sql, {"root_op_id": ...})`. Make sure `Database.execute` accepts both tuple and dict params.

**Step 4: Update Database.execute to accept dict params**

In `src/hgp/db.py`, change:
```python
def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
```
to:
```python
def execute(self, sql: str, params: tuple[Any, ...] | dict[str, Any] = ()) -> sqlite3.Cursor:
```

**Step 5: Run tests**

```bash
uv run pytest tests/test_dag.py -v
```

Expected: all PASS

**Step 6: Commit**

```bash
git add src/hgp/dag.py tests/test_dag.py src/hgp/db.py
git commit -m "feat: add DAG traversal and chain_hash with edge-inclusion"
```

---

## Task 5: Lease Token Manager

**Files:**
- Create: `src/hgp/lease.py`
- Create: `tests/test_lease.py`

**Step 1: Write failing tests**

```python
# tests/test_lease.py
from __future__ import annotations

import pytest
import time
from datetime import datetime, timedelta
from hgp.db import Database
from hgp.dag import compute_chain_hash
from hgp.lease import LeaseManager
from hgp.errors import ChainStaleError, LeaseExpiredError


def _setup(hgp_dirs: dict) -> tuple[Database, LeaseManager]:
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    db.begin_immediate()
    db.insert_operation("root-op", "artifact", "agent-1", 1, "sha256:abc")
    db.commit()
    mgr = LeaseManager(db)
    return db, mgr


def test_acquire_lease(hgp_dirs: dict):
    db, mgr = _setup(hgp_dirs)
    lease = mgr.acquire(agent_id="agent-1", subgraph_root_op_id="root-op", ttl_seconds=300)
    assert lease.status.value == "ACTIVE"
    assert lease.subgraph_root_op_id == "root-op"
    assert lease.chain_hash.startswith("sha256:")


def test_validate_valid_lease(hgp_dirs: dict):
    db, mgr = _setup(hgp_dirs)
    lease = mgr.acquire("agent-1", "root-op", ttl_seconds=300)
    result = mgr.validate(lease.lease_id)
    assert result["valid"] is True


def test_validate_stale_chain_hash(hgp_dirs: dict):
    db, mgr = _setup(hgp_dirs)
    lease = mgr.acquire("agent-1", "root-op", ttl_seconds=300)
    # Mutate the subgraph
    db.update_operation_status("root-op", "INVALIDATED")
    db.commit()
    result = mgr.validate(lease.lease_id)
    assert result["valid"] is False
    assert result["reason"] == "CHAIN_STALE"


def test_release_lease(hgp_dirs: dict):
    db, mgr = _setup(hgp_dirs)
    lease = mgr.acquire("agent-1", "root-op", ttl_seconds=300)
    mgr.release(lease.lease_id)
    row = db.execute("SELECT status FROM leases WHERE lease_id=?", (lease.lease_id,)).fetchone()
    assert row["status"] == "RELEASED"
```

**Step 2: Run to confirm failure**

```bash
uv run pytest tests/test_lease.py -v
```

Expected: `ImportError: No module named 'hgp.lease'`

**Step 3: Create src/hgp/lease.py**

```python
"""Lease token management for HGP epoch validation."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from hgp.dag import compute_chain_hash
from hgp.db import Database
from hgp.models import Lease, LeaseStatus


class LeaseManager:
    def __init__(self, db: Database) -> None:
        self._db = db

    def acquire(
        self,
        agent_id: str,
        subgraph_root_op_id: str,
        ttl_seconds: int = 300,
    ) -> Lease:
        """Acquire a lease on a subgraph. Auto-releases any prior active lease."""
        now = datetime.utcnow()
        expires_at = now + timedelta(seconds=ttl_seconds)
        chain_hash = compute_chain_hash(self._db, subgraph_root_op_id)

        # Auto-release previous active lease for this agent+subgraph
        self._db.execute(
            """
            UPDATE leases SET status = 'RELEASED'
            WHERE agent_id = ? AND subgraph_root_op_id = ? AND status = 'ACTIVE'
            """,
            (agent_id, subgraph_root_op_id),
        )

        lease_id = str(uuid.uuid4())
        self._db.execute(
            """
            INSERT INTO leases (lease_id, agent_id, subgraph_root_op_id,
                                chain_hash, expires_at, status)
            VALUES (?, ?, ?, ?, ?, 'ACTIVE')
            """,
            (lease_id, agent_id, subgraph_root_op_id,
             chain_hash, expires_at.isoformat() + "Z"),
        )
        self._db.commit()

        return Lease(
            lease_id=lease_id,
            agent_id=agent_id,
            subgraph_root_op_id=subgraph_root_op_id,
            chain_hash=chain_hash,
            issued_at=now,
            expires_at=expires_at,
            status=LeaseStatus.ACTIVE,
        )

    def validate(self, lease_id: str, extend: bool = True) -> dict[str, Any]:
        """Validate lease is still valid and chain_hash hasn't changed.

        Uses BEGIN IMMEDIATE to atomically check + extend TTL, preventing
        a race where two concurrent validates could both see valid and extend.
        """
        # Read-only pre-check (no lock) for fast rejection
        row = self._db.execute(
            "SELECT * FROM leases WHERE lease_id = ?", (lease_id,)
        ).fetchone()

        if not row:
            return {"valid": False, "reason": "LEASE_NOT_FOUND"}

        now = datetime.utcnow()
        expires_at = datetime.fromisoformat(row["expires_at"].rstrip("Z"))

        if row["status"] != "ACTIVE" or now > expires_at:
            return {"valid": False, "reason": "LEASE_EXPIRED"}

        # Recompute chain_hash and conditionally extend under write lock
        self._db.begin_immediate()
        try:
            current_hash = compute_chain_hash(self._db, row["subgraph_root_op_id"])
            if current_hash != row["chain_hash"]:
                self._db.rollback()
                return {
                    "valid": False,
                    "reason": "CHAIN_STALE",
                    "current_chain_hash": current_hash,
                }

            original_ttl = int(
                (expires_at - datetime.fromisoformat(row["issued_at"].rstrip("Z"))).total_seconds()
            )
            new_expires = now + timedelta(seconds=original_ttl)
            if extend:
                self._db.execute(
                    "UPDATE leases SET expires_at = ? WHERE lease_id = ?",
                    (new_expires.isoformat() + "Z", lease_id),
                )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise

        return {
            "valid": True,
            "chain_hash": current_hash,
            "expires_at": new_expires.isoformat() + "Z",
        }

    def release(self, lease_id: str) -> None:
        self._db.execute(
            "UPDATE leases SET status = 'RELEASED' WHERE lease_id = ?",
            (lease_id,),
        )
        self._db.commit()
```

**Step 4: Run tests**

```bash
uv run pytest tests/test_lease.py -v
```

Expected: all PASS

**Step 5: Commit**

```bash
git add src/hgp/lease.py tests/test_lease.py
git commit -m "feat: add lease token manager with acquire/validate/release"
```

---

## Task 6: Crash Recovery Reconciler

**Files:**
- Create: `src/hgp/reconciler.py`
- Create: `tests/test_reconciler.py`

**Step 1: Write failing tests**

```python
# tests/test_reconciler.py
from __future__ import annotations

import time
from pathlib import Path
from datetime import datetime, timedelta
from hgp.db import Database
from hgp.cas import CAS
from hgp.reconciler import Reconciler


def _setup(hgp_dirs: dict) -> tuple[Database, CAS, Reconciler]:
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    cas = CAS(hgp_dirs["content_dir"])
    reconciler = Reconciler(db, cas, hgp_dirs["content_dir"])
    return db, cas, reconciler


def test_rule1_2_completed_with_missing_blob(hgp_dirs: dict):
    """DB says COMPLETED but blob missing → MISSING_BLOB."""
    db, cas, rec = _setup(hgp_dirs)
    db.begin_immediate()
    db.insert_operation("op-1", "artifact", "agent-1", 1, "sha256:" + "a" * 64)
    db.commit()
    report = rec.reconcile()
    assert "sha256:" + "a" * 64 in report.missing_blobs


def test_rule3_orphan_blob_old(hgp_dirs: dict):
    """Blob exists with no DB ref and mtime > grace → ORPHAN_CANDIDATE."""
    db, cas, rec = _setup(hgp_dirs)
    payload = b"orphan content"
    obj_hash = cas.store(payload)
    # Mock mtime to be old
    hex_hash = obj_hash.removeprefix("sha256:")
    blob_path = hgp_dirs["content_dir"] / hex_hash[:2] / hex_hash[2:]
    old_time = (datetime.now() - timedelta(hours=1)).timestamp()
    import os; os.utime(blob_path, (old_time, old_time))

    report = rec.reconcile()
    assert obj_hash in report.orphan_candidates


def test_rule3_orphan_blob_young_skipped(hgp_dirs: dict):
    """Blob within grace period is NOT classified as orphan."""
    db, cas, rec = _setup(hgp_dirs)
    payload = b"young content"
    obj_hash = cas.store(payload)
    report = rec.reconcile()
    assert obj_hash not in report.orphan_candidates
    assert report.skipped_young_blobs >= 1


def test_staging_cleanup(hgp_dirs: dict):
    """Stale .tmp files older than grace period are removed."""
    staging = hgp_dirs["content_dir"] / ".staging"
    old_tmp = staging / "stale.tmp"
    old_tmp.write_bytes(b"leftover")
    import os
    old_time = (datetime.now() - timedelta(hours=1)).timestamp()
    os.utime(old_tmp, (old_time, old_time))

    _, cas, rec = _setup(hgp_dirs)
    report = rec.reconcile()
    assert not old_tmp.exists()
    assert report.staging_cleaned >= 1
```

**Step 2: Run to confirm failure**

```bash
uv run pytest tests/test_reconciler.py -v
```

Expected: `ImportError: No module named 'hgp.reconciler'`

**Step 3: Create src/hgp/reconciler.py**

```python
"""Crash recovery reconciler — 3-rule deterministic consistency check."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from hgp.cas import CAS
from hgp.db import Database
from hgp.models import ReconcileReport

ORPHAN_GRACE_PERIOD = timedelta(minutes=15)


class Reconciler:
    def __init__(self, db: Database, cas: CAS, content_dir: Path) -> None:
        self._db = db
        self._cas = cas
        self._staging_dir = content_dir / ".staging"

    def reconcile(self, dry_run: bool = False) -> ReconcileReport:
        report = ReconcileReport()
        now = datetime.utcnow()

        # Rules 1 & 2: COMPLETED op with missing blob → MISSING_BLOB
        completed_ops = self._db.query_operations(status="COMPLETED")
        for op in completed_ops:
            obj_hash = op.get("object_hash")
            if obj_hash and not self._cas.exists(obj_hash):
                report.missing_blobs.append(obj_hash)
                if not dry_run:
                    self._db.update_operation_status(op["op_id"], "MISSING_BLOB")

        # Rule 3: Blob with no DB reference + older than grace → ORPHAN_CANDIDATE
        for obj_hash, mtime in self._cas.list_all_blobs_with_mtime():
            if not self._db.object_referenced(obj_hash):
                if now - mtime > ORPHAN_GRACE_PERIOD:
                    report.orphan_candidates.append(obj_hash)
                    if not dry_run:
                        self._db.upsert_object_status(obj_hash, "ORPHAN_CANDIDATE")
                else:
                    report.skipped_young_blobs += 1

        # Clean stale staging files older than grace period
        if self._staging_dir.exists():
            for tmp_file in self._staging_dir.glob("*.tmp"):
                try:
                    mtime = datetime.fromtimestamp(tmp_file.stat().st_mtime)
                    if now - mtime > ORPHAN_GRACE_PERIOD:
                        if not dry_run:
                            tmp_file.unlink()
                        report.staging_cleaned += 1
                except FileNotFoundError:
                    pass  # Concurrent cleanup

        if not dry_run:
            self._db.commit()

        return report
```

**Step 4: Run tests**

```bash
uv run pytest tests/test_reconciler.py -v
```

Expected: all PASS

**Step 5: Commit**

```bash
git add src/hgp/reconciler.py tests/test_reconciler.py
git commit -m "feat: add crash recovery reconciler with 3-rule deterministic logic"
```

---

## Task 7: MCP Server (hgp_create_operation + hgp_query_*)

**Files:**
- Create: `src/hgp/server.py`
- Create: `tests/test_integration.py`

**Step 1: Write integration tests**

```python
# tests/test_integration.py
from __future__ import annotations

import base64
import pytest
from pathlib import Path
from hgp.db import Database
from hgp.cas import CAS
from hgp.dag import compute_chain_hash
from hgp.lease import LeaseManager
from hgp.reconciler import Reconciler


def _make_components(hgp_dirs: dict) -> tuple[Database, CAS, LeaseManager, Reconciler]:
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    cas = CAS(hgp_dirs["content_dir"])
    mgr = LeaseManager(db)
    rec = Reconciler(db, cas, hgp_dirs["content_dir"])
    return db, cas, mgr, rec


def test_create_root_operation(hgp_dirs: dict):
    """Create a root artifact operation with no parents."""
    db, cas, mgr, rec = _make_components(hgp_dirs)
    payload = b"my first artifact"
    encoded = base64.b64encode(payload).decode()
    obj_hash = cas.store(payload)

    db.begin_immediate()
    seq = db.next_commit_seq()
    chain_hash = "sha256:" + "0" * 64  # genesis hash
    db.insert_operation("op-root", "artifact", "agent-1", seq, chain_hash,
                        object_hash=obj_hash)
    db.commit()

    op = db.get_operation("op-root")
    assert op["status"] == "COMPLETED"
    assert op["object_hash"] == obj_hash
    assert cas.read(obj_hash) == payload


def test_create_child_operation(hgp_dirs: dict):
    """Create parent → child with chain_hash validation."""
    db, cas, mgr, rec = _make_components(hgp_dirs)

    # Create parent
    db.begin_immediate()
    db.insert_operation("parent", "artifact", "agent-1", 1, "sha256:" + "0" * 64)
    db.commit()

    parent_hash = compute_chain_hash(db, "parent")

    # Create child with chain_hash
    db.begin_immediate()
    seq = db.next_commit_seq()
    db.insert_operation("child", "artifact", "agent-1", seq, parent_hash)
    db.insert_edge("child", "parent", "causal")
    db.commit()

    op = db.get_operation("child")
    assert op["status"] == "COMPLETED"


def test_chain_stale_detection(hgp_dirs: dict):
    """Simulates CHAIN_STALE: agent holds old chain_hash while subgraph changes."""
    db, cas, mgr, rec = _make_components(hgp_dirs)

    db.begin_immediate()
    db.insert_operation("op-a", "artifact", "agent-1", 1, "sha256:" + "0" * 64)
    db.commit()

    # Agent 1 snapshots chain_hash
    agent1_hash = compute_chain_hash(db, "op-a")

    # Agent 2 mutates subgraph first
    db.begin_immediate()
    db.insert_operation("op-b", "artifact", "agent-2", 2, "sha256:" + "1" * 64)
    db.insert_edge("op-b", "op-a", "causal")
    db.commit()

    # Agent 1 tries to commit with stale hash
    current_hash = compute_chain_hash(db, "op-a")
    assert current_hash != agent1_hash  # CHAIN_STALE detected


def test_concurrent_chain_stale(hgp_dirs: dict):
    """Two connections: agent-1 succeeds, agent-2 sees CHAIN_STALE."""
    import sqlite3 as _sqlite3
    db, cas, mgr, rec = _make_components(hgp_dirs)

    db.begin_immediate()
    db.insert_operation("genesis", "artifact", "agent-1", 1, "sha256:" + "0" * 64)
    db.commit()

    # Both agents snapshot the same chain_hash
    agent1_hash = compute_chain_hash(db, "genesis")
    agent2_hash = agent1_hash  # Same snapshot

    # Agent 1 commits first (mutates subgraph)
    db.begin_immediate()
    db.insert_operation("op-a1", "artifact", "agent-1", 2, agent1_hash)
    db.insert_edge("op-a1", "genesis", "causal")
    db.commit()

    # Agent 2 now checks its snapshot — must be stale
    current = compute_chain_hash(db, "genesis")
    assert current != agent2_hash  # CHAIN_STALE would be returned


def test_cas_failure_no_db_write(hgp_dirs: dict):
    """If CAS fails (payload too large), no DB record should be created."""
    db, cas, mgr, rec = _make_components(hgp_dirs)
    from hgp.errors import PayloadTooLargeError
    import base64

    large_payload = base64.b64encode(b"x" * (11 * 1024 * 1024)).decode()
    with pytest.raises(PayloadTooLargeError):
        cas.store(base64.b64decode(large_payload))

    ops = db.query_operations()
    assert len(ops) == 0  # No partial DB state


def test_full_lease_commit_flow(hgp_dirs: dict):
    """Full flow: acquire → validate → commit."""
    db, cas, mgr, rec = _make_components(hgp_dirs)

    db.begin_immediate()
    db.insert_operation("genesis", "artifact", "agent-1", 1, "sha256:" + "0" * 64)
    db.commit()

    # Acquire lease
    lease = mgr.acquire("agent-1", "genesis", ttl_seconds=300)
    assert lease.status.value == "ACTIVE"

    # Validate (PING before LLM compute)
    result = mgr.validate(lease.lease_id)
    assert result["valid"] is True

    # Commit child with lease's chain_hash
    db.begin_immediate()
    current = compute_chain_hash(db, "genesis")
    assert current == lease.chain_hash  # Still valid
    seq = db.next_commit_seq()
    db.insert_operation("child-op", "artifact", "agent-1", seq, current)
    db.insert_edge("child-op", "genesis", "causal")
    mgr.release(lease.lease_id)
    db.commit()

    assert db.get_operation("child-op")["status"] == "COMPLETED"
```

**Step 2: Run to verify tests pass with current components**

```bash
uv run pytest tests/test_integration.py -v
```

Expected: all PASS (these test the component layer, not MCP tools yet)

**Step 3: Create src/hgp/server.py**

```python
"""HGP MCP Server — FastMCP entry point."""

from __future__ import annotations

import base64
import json
import uuid
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from hgp.cas import CAS
from hgp.dag import compute_chain_hash, get_ancestors, get_descendants
from hgp.db import Database
from hgp.errors import ChainStaleError, LeaseExpiredError, ParentNotFoundError
from hgp.lease import LeaseManager
from hgp.models import ReconcileReport
from hgp.reconciler import Reconciler

# ── Server initialization ───────────────────────────────────

HGP_DIR = Path.home() / ".hgp"
HGP_CONTENT_DIR = HGP_DIR / ".hgp_content"
HGP_DB_PATH = HGP_DIR / "hgp.db"

mcp = FastMCP("hgp")

_db: Database | None = None
_cas: CAS | None = None
_lease_mgr: LeaseManager | None = None
_reconciler: Reconciler | None = None


def _get_components() -> tuple[Database, CAS, LeaseManager, Reconciler]:
    global _db, _cas, _lease_mgr, _reconciler
    if _db is None:
        HGP_DIR.mkdir(parents=True, exist_ok=True)
        HGP_CONTENT_DIR.mkdir(exist_ok=True)
        _db = Database(HGP_DB_PATH)
        _db.initialize()
        _cas = CAS(HGP_CONTENT_DIR)
        _lease_mgr = LeaseManager(_db)
        _reconciler = Reconciler(_db, _cas, HGP_CONTENT_DIR)
        _db.expire_leases()
        _db.commit()
        _reconciler.reconcile()
    assert _db and _cas and _lease_mgr and _reconciler
    return _db, _cas, _lease_mgr, _reconciler


# ── MCP Tools ───────────────────────────────────────────────

@mcp.tool()
def hgp_create_operation(
    op_type: str,
    agent_id: str,
    parent_op_ids: list[str] | None = None,
    invalidates_op_ids: list[str] | None = None,
    payload: str | None = None,
    mime_type: str | None = None,
    lease_id: str | None = None,
    chain_hash: str | None = None,
    # For merge ops with multiple parents, caller must specify which root to
    # validate the chain_hash against. If None, uses parent_op_ids[0].
    subgraph_root_op_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a new operation in the causal history DAG.

    For merge operations (multiple parent_op_ids), specify subgraph_root_op_id
    explicitly to define which subgraph the chain_hash guards. The merge
    operation becomes a child of ALL listed parents.
    """
    db, cas, lease_mgr, _ = _get_components()

    # Validate parents exist
    for pid in (parent_op_ids or []):
        if not db.get_operation(pid):
            raise ParentNotFoundError(f"Parent operation not found: {pid}")

    # Determine validation root: explicit > first parent > None (genesis)
    root_op_id = subgraph_root_op_id or (parent_op_ids[0] if parent_op_ids else None)

    # Phase 1: Pre-flight chain_hash check (advisory, before acquiring write lock)
    if chain_hash and root_op_id:
        current = compute_chain_hash(db, root_op_id)
        if current != chain_hash:
            raise ChainStaleError(f"CHAIN_STALE: expected {chain_hash}, got {current}")

    # Phase 2: Write blob to CAS (idempotent, outside transaction)
    object_hash: str | None = None
    if payload:
        raw = base64.b64decode(payload)
        object_hash = cas.store(raw)

    # Phase 3: Atomic DB commit (BEGIN IMMEDIATE)
    op_id = str(uuid.uuid4())
    db.begin_immediate()
    try:
        # Re-validate chain_hash under write lock (prevents TOCTOU)
        if chain_hash and root_op_id:
            current = compute_chain_hash(db, root_op_id)
            if current != chain_hash:
                db.rollback()
                raise ChainStaleError(f"CHAIN_STALE (under lock): expected {chain_hash}")

        seq = db.next_commit_seq()

        # Use placeholder chain_hash during insert; update after edges are inserted
        db.insert_operation(
            op_id=op_id,
            op_type=op_type,
            agent_id=agent_id,
            commit_seq=seq,
            chain_hash="sha256:pending",  # Will be updated below
            object_hash=object_hash,
            metadata=json.dumps(metadata) if metadata else None,
            mime_type=mime_type,
        )

        # Insert causal edges (new op is child of all parents)
        for pid in (parent_op_ids or []):
            db.insert_edge(op_id, pid, "causal")

        # Insert invalidation edges + mark invalidated ops
        for inv_id in (invalidates_op_ids or []):
            db.insert_edge(op_id, inv_id, "invalidates")
            db.update_operation_status(inv_id, "INVALIDATED")

        # Compute final chain_hash AFTER all edges are inserted (post-insert state)
        new_root = subgraph_root_op_id or op_id  # For genesis: hash of self
        final_chain_hash = compute_chain_hash(db, new_root)
        db.execute(
            "UPDATE operations SET chain_hash = ? WHERE op_id = ?",
            (final_chain_hash, op_id),
        )

        if lease_id:
            db.execute(
                "UPDATE leases SET status = 'RELEASED' WHERE lease_id = ? AND status = 'ACTIVE'",
                (lease_id,),
            )

        db.commit()
    except Exception:
        db.rollback()
        raise

    return {
        "op_id": op_id,
        "status": "COMPLETED",
        "commit_seq": seq,
        "object_hash": object_hash,
        "chain_hash": final_chain_hash,  # Post-insert state — immediately usable
    }


@mcp.tool()
def hgp_query_operations(
    op_id: str | None = None,
    agent_id: str | None = None,
    op_type: str | None = None,
    status: str | None = None,
    since_commit_seq: int | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Query operations with optional filters."""
    db, _, _, _ = _get_components()
    if op_id:
        op = db.get_operation(op_id)
        return [op] if op else []
    return db.query_operations(status=status, agent_id=agent_id, limit=limit)


@mcp.tool()
def hgp_query_subgraph(
    root_op_id: str,
    direction: str = "ancestors",
    max_depth: int = 50,
    include_invalidated: bool = False,
) -> dict[str, Any]:
    """Traverse the causal subgraph from root_op_id."""
    db, _, _, _ = _get_components()
    chain_hash = compute_chain_hash(db, root_op_id)
    if direction == "ancestors":
        ops = get_ancestors(db, root_op_id)
    else:
        ops = get_descendants(db, root_op_id)
    if not include_invalidated:
        ops = [o for o in ops if o["status"] != "INVALIDATED"]
    return {"root_op_id": root_op_id, "chain_hash": chain_hash, "operations": ops}


@mcp.tool()
def hgp_acquire_lease(
    agent_id: str,
    subgraph_root_op_id: str,
    ttl_seconds: int = 300,
) -> dict[str, Any]:
    """Acquire a lease on a subgraph for optimistic concurrency."""
    _, _, lease_mgr, _ = _get_components()
    lease = lease_mgr.acquire(agent_id, subgraph_root_op_id, ttl_seconds)
    return {
        "lease_id": lease.lease_id,
        "chain_hash": lease.chain_hash,
        "expires_at": lease.expires_at.isoformat() + "Z",
    }


@mcp.tool()
def hgp_validate_lease(lease_id: str, extend: bool = True) -> dict[str, Any]:
    """Validate (PING) a lease token before LLM compute."""
    _, _, lease_mgr, _ = _get_components()
    return lease_mgr.validate(lease_id, extend=extend)


@mcp.tool()
def hgp_release_lease(lease_id: str) -> dict[str, Any]:
    """Release a lease token explicitly."""
    _, _, lease_mgr, _ = _get_components()
    lease_mgr.release(lease_id)
    return {"released": True, "lease_id": lease_id}


@mcp.tool()
def hgp_get_artifact(object_hash: str) -> dict[str, Any]:
    """Retrieve blob content from CAS by hash."""
    _, cas, _, _ = _get_components()
    data = cas.read(object_hash)
    if data is None:
        return {"error": "NOT_FOUND", "object_hash": object_hash}
    return {
        "object_hash": object_hash,
        "size": len(data),
        "content": base64.b64encode(data).decode(),
    }


@mcp.tool()
def hgp_anchor_git(
    op_id: str,
    git_commit_sha: str,
    repository: str | None = None,
) -> dict[str, Any]:
    """Link an HGP operation to a Git commit SHA."""
    db, _, _, _ = _get_components()
    if len(git_commit_sha) != 40:
        return {"error": "INVALID_SHA", "message": "git_commit_sha must be 40 hex chars"}
    db.execute(
        "INSERT OR IGNORE INTO git_anchors (op_id, git_commit_sha, repository) VALUES (?, ?, ?)",
        (op_id, git_commit_sha, repository),
    )
    db.commit()
    return {"anchored": True, "op_id": op_id, "git_commit_sha": git_commit_sha}


@mcp.tool()
def hgp_reconcile(dry_run: bool = False) -> dict[str, Any]:
    """Run crash recovery reconciler."""
    _, _, _, reconciler = _get_components()
    report = reconciler.reconcile(dry_run=dry_run)
    return report.model_dump()


if __name__ == "__main__":
    mcp.run()
```

**Step 4: Run full test suite**

```bash
uv run pytest tests/ -v
```

Expected: all PASS

**Step 5: Commit**

```bash
git add src/hgp/server.py tests/test_integration.py
git commit -m "feat: add FastMCP server with all 9 HGP tools"
```

---

## Task 8: Type Check + Smoke Test

**Step 1: Run pyright**

```bash
uv run pyright src/
```

Fix any type errors reported. Common fixes needed:
- Add `# type: ignore` only for stdlib sqlite3 Row typing gaps
- Ensure all return types are annotated

**Step 2: Smoke test the server manually**

```bash
uv run python -c "
from hgp.server import mcp
print('MCP server tools:', [t.name for t in mcp._tool_manager.list_tools()])
"
```

Expected: prints all 9 tool names.

**Step 3: Run all tests with coverage check**

```bash
uv run pytest tests/ -v --tb=short
```

Expected: all PASS, no failures.

**Step 4: Final commit**

```bash
git add -A
git commit -m "feat: HGP v0.2.0 — complete V1 implementation (Claude + ChatGPT + Gemini approved TDD)"
```

---

## Verification Checklist (before declaring done)

- [ ] All 8 tasks committed with meaningful messages
- [ ] `uv run pytest tests/ -v` — all PASS
- [ ] `uv run pyright src/` — no errors
- [ ] `hgp_create_operation` stores blob + commits DAG atomically
- [ ] `chain_hash` changes when edges OR node status changes
- [ ] `BEGIN IMMEDIATE` used in all write paths
- [ ] Reconciler skips young blobs (grace period)
- [ ] All 9 MCP tools registered in server.py
