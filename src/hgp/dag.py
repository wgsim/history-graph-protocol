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

_EDGES_IN_SUBGRAPH_DEPTH_SQL = """
WITH RECURSIVE ancestors(op_id, depth) AS (
    SELECT :root_op_id, 0
    UNION
    SELECT e.parent_op_id, a.depth + 1
    FROM op_edges e
    JOIN ancestors a ON e.child_op_id = a.op_id
    WHERE a.depth < :max_depth
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

_ANCESTOR_FULL_SQL = """
WITH RECURSIVE ancestors(op_id, depth) AS (
    SELECT :root_op_id, 0
    UNION
    SELECT e.parent_op_id, a.depth + 1
    FROM op_edges e
    JOIN ancestors a ON e.child_op_id = a.op_id
)
SELECT o.*, a.depth
FROM operations o
JOIN ancestors a ON o.op_id = a.op_id
ORDER BY a.depth, o.op_id
"""

_ANCESTOR_DEPTH_SQL = """
WITH RECURSIVE ancestors(op_id, depth) AS (
    SELECT :root_op_id, 0
    UNION
    SELECT e.parent_op_id, a.depth + 1
    FROM op_edges e
    JOIN ancestors a ON e.child_op_id = a.op_id
    WHERE a.depth < :max_depth
)
SELECT o.*, a.depth
FROM operations o
JOIN ancestors a ON o.op_id = a.op_id
ORDER BY a.depth, o.op_id
"""

_DESCENDANTS_FULL_SQL = """
WITH RECURSIVE descendants(op_id, depth) AS (
    SELECT :root_op_id, 0
    UNION
    SELECT e.child_op_id, d.depth + 1
    FROM op_edges e
    JOIN descendants d ON e.parent_op_id = d.op_id
)
SELECT o.*, d.depth
FROM operations o
JOIN descendants d ON o.op_id = d.op_id
ORDER BY d.depth, o.commit_seq
"""

_DESCENDANTS_DEPTH_SQL = """
WITH RECURSIVE descendants(op_id, depth) AS (
    SELECT :root_op_id, 0
    UNION
    SELECT e.child_op_id, d.depth + 1
    FROM op_edges e
    JOIN descendants d ON e.parent_op_id = d.op_id
    WHERE d.depth < :max_depth
)
SELECT o.*, d.depth
FROM operations o
JOIN descendants d ON o.op_id = d.op_id
ORDER BY d.depth, o.commit_seq
"""


MAX_CHAIN_HASH_DEPTH = 500


def compute_chain_hash(db: Database, root_op_id: str, _max_depth: int = MAX_CHAIN_HASH_DEPTH) -> str:
    """Compute the chain_hash for a subgraph rooted at root_op_id.

    Includes both operations (nodes) and edges for structural sensitivity.
    Traversal is depth-bounded by _max_depth to prevent DoS on deep DAGs.
    """
    ops = db.execute(_ANCESTOR_DEPTH_SQL, {"root_op_id": root_op_id, "max_depth": _max_depth}).fetchall()
    edges = db.execute(
        _EDGES_IN_SUBGRAPH_DEPTH_SQL,
        {"root_op_id": root_op_id, "max_depth": _max_depth},
    ).fetchall()

    ops_part = "|".join(
        f"{row['op_id']}:{row['status']}:{row['commit_seq']}" for row in ops
    )
    edges_part = "|".join(
        f"{row['child_op_id']}>{row['parent_op_id']}:{row['edge_type']}" for row in edges
    )
    canonical = f"OPS[{ops_part}]EDGES[{edges_part}]"
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def get_ancestors(db: Database, root_op_id: str, max_depth: int | None = None) -> list[dict[str, Any]]:
    """Return all ancestor operations including root, with CTE depth column."""
    if max_depth is None:
        rows = db.execute(_ANCESTOR_FULL_SQL, {"root_op_id": root_op_id}).fetchall()
    else:
        rows = db.execute(_ANCESTOR_DEPTH_SQL, {"root_op_id": root_op_id, "max_depth": max_depth}).fetchall()
    return [dict(r) for r in rows]


def get_descendants(db: Database, root_op_id: str, max_depth: int | None = None) -> list[dict[str, Any]]:
    """Return all descendant operations including root, with CTE depth column."""
    if max_depth is None:
        rows = db.execute(_DESCENDANTS_FULL_SQL, {"root_op_id": root_op_id}).fetchall()
    else:
        rows = db.execute(_DESCENDANTS_DEPTH_SQL, {"root_op_id": root_op_id, "max_depth": max_depth}).fetchall()
    return [dict(r) for r in rows]
