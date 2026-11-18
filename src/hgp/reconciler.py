"""Crash recovery reconciler — 5-rule deterministic consistency check."""

from __future__ import annotations

import hashlib
import logging
import re
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from hgp.cas import CAS
from hgp.db import Database
from hgp.hooks.hgp_paths import checkout_value_from_metadata, find_linked_worktree_gitdir
from hgp.models import ReconcileReport
from hgp.project import ensure_worktree_id, read_worktree_id

_log = logging.getLogger(__name__)

_STAGING_FILE_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\.tmp$"
)

ORPHAN_GRACE_PERIOD = timedelta(minutes=15)
PENDING_GRACE_PERIOD = timedelta(minutes=5)


def _file_matches_hash(file_path: str, expected_hash: str) -> bool:
    """Return True if file_path exists and its SHA-256 matches expected_hash.

    Returns False for ENOENT (file absent).  For other OS errors (e.g. EACCES)
    logs a warning and returns False so the caller treats the op as
    unrecoverable rather than silently promoting it to STALE_PENDING.
    """
    try:
        data = Path(file_path).read_bytes()
        computed = f"sha256:{hashlib.sha256(data).hexdigest()}"
        return computed == expected_hash
    except FileNotFoundError:
        return False
    except OSError as exc:
        _log.warning("_file_matches_hash: unexpected OSError for %r: %s", file_path, exc)
        return False


def _registration_live(gitdir: Path) -> bool | None:
    """Return None when Git's registration cannot be checked reliably."""
    try:
        (gitdir / "locked").stat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        _log.warning("Orphan retirement deferred: cannot check lock for worktree registration %s: %s", gitdir, exc)
        return None
    else:
        return True

    try:
        raw = (gitdir / "gitdir").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        _log.warning("Orphan retirement deferred: cannot read worktree registration %s/gitdir: %s", gitdir, exc)
        return None
    if not raw:
        _log.warning("Orphan retirement deferred: empty worktree registration %s/gitdir", gitdir)
        return None
    pointer = gitdir / raw
    try:
        pointer.stat()
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError as exc:
        _log.warning(
            "Orphan retirement deferred: cannot check checkout pointer %s for registration %s: %s",
            pointer, gitdir, exc,
        )
        return None
    return True


class Reconciler:
    def __init__(
        self,
        db: Database,
        cas: CAS,
        content_dir: Path,
        project_root: Path | None = None,
    ) -> None:
        self._db = db
        self._cas = cas
        self._staging_dir = content_dir / ".staging"
        self._project_root = project_root.resolve() if project_root is not None else None
        gitdir = find_linked_worktree_gitdir(self._project_root) if self._project_root is not None else None
        self.checkout_id = ensure_worktree_id(gitdir) if gitdir is not None else None
        self._linked_root = str(self._project_root) if gitdir is not None else None
        self._worktrees_dir = None
        if gitdir is not None:
            self._worktrees_dir = gitdir.parent
        elif self._project_root is not None:
            self._worktrees_dir = self._project_root / ".git" / "worktrees"

    def _op_in_scope(self, op: dict[str, Any], file_path: str) -> bool:
        """Return whether a pending file operation belongs to this checkout."""
        if self._project_root is None:
            return True
        checkout_id = checkout_value_from_metadata(op.get("metadata"), "id")
        if checkout_id is not None:
            if checkout_id != self.checkout_id:
                return False
        elif checkout_value_from_metadata(op.get("metadata"), "root") != self._linked_root:
            return False
        try:
            Path(self._resolve(file_path) or file_path).resolve().relative_to(self._project_root)
            return True
        except (OSError, RuntimeError, ValueError):
            return False

    def _resolve(self, file_path: str) -> str | None:
        """Return the absolute path for a stored file key.

        Repo-relative keys resolve against this checkout (in-scope ops were
        recorded here); None when there is no checkout root to resolve against.
        """
        if Path(file_path).is_absolute():
            return file_path
        if self._project_root is None:
            return None
        return str(self._project_root / file_path)

    def _registered_worktree_ids(self) -> set[str] | None:
        """Return IDs of worktrees whose checkout still exists (or is locked).

        A checkout deleted without ``git worktree remove`` leaves its registration
        behind as "prunable" until Git prunes it, possibly months later; it no
        longer counts. None when the registrations cannot be listed: then no
        other checkout's operation is retired.
        """
        if self._worktrees_dir is None:
            return set()
        try:
            registrations = list(self._worktrees_dir.iterdir())
        except FileNotFoundError:
            return set()
        except OSError as exc:
            _log.warning(
                "Orphan retirement deferred: cannot list worktree registrations in %s: %s",
                self._worktrees_dir, exc,
            )
            return None
        identifiers: set[str] = set()
        for gitdir in registrations:
            try:
                mode = gitdir.stat().st_mode
            except OSError as exc:
                _log.warning("Orphan retirement deferred: cannot inspect worktree registration %s: %s", gitdir, exc)
                return None
            if not stat.S_ISDIR(mode):
                continue
            live = _registration_live(gitdir)
            if live is None:
                return None
            if not live:
                continue
            identifier = read_worktree_id(gitdir)
            if identifier is None:
                try:
                    (gitdir / "hgp-worktree-id").lstat()
                except FileNotFoundError:
                    # HGP never assigned an ID here, so this worktree owns no ID-tagged op.
                    continue
                except OSError:
                    pass
                # A marker exists but cannot be read: it could own an op we are checking.
                _log.warning("Orphan retirement deferred: unreadable or invalid worktree ID in %s", gitdir)
                return None
            identifiers.add(identifier)
        return identifiers

    def reconcile(self, dry_run: bool = False) -> ReconcileReport:
        report = ReconcileReport()
        now = datetime.now(timezone.utc)

        # Rules 1 & 2: COMPLETED op with missing blob → MISSING_BLOB
        # Only object_hash references a CAS blob; chain_hash is a computed digest, not a stored blob.
        completed_ops = self._db.query_operations(status="COMPLETED", include_inactive=True)
        for op in completed_ops:
            candidate = op.get("object_hash")
            if candidate and not self._cas.exists(candidate):
                report.missing_blobs.append(candidate)
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
                if not _STAGING_FILE_RE.fullmatch(tmp_file.name):
                    continue
                try:
                    mtime = datetime.fromtimestamp(tmp_file.stat().st_mtime, tz=timezone.utc)
                    if now - mtime > ORPHAN_GRACE_PERIOD:
                        if not dry_run:
                            tmp_file.unlink()
                        report.staging_cleaned += 1
                except FileNotFoundError:
                    pass  # Concurrent cleanup — expected, ignore
                except OSError as exc:
                    report.errors.append(f"staging cleanup error for {tmp_file.name}: {exc}")

        # Rule 5: PENDING op recovery — finalize or triage stuck PENDING ops
        pending_ops = self._db.query_operations(status="PENDING", include_inactive=True)
        registered_ids: set[str] | None = None
        registrations_read = False
        for op in pending_ops:
            created_at_str = op.get("created_at", "")
            try:
                created_at = datetime.fromisoformat(
                    created_at_str.replace("Z", "+00:00")
                )
            except (ValueError, AttributeError) as exc:
                report.errors.append(
                    f"PENDING op {op.get('op_id')!r} has unparseable "
                    f"created_at={created_at_str!r}: {exc}"
                )
                continue
            age = now - created_at
            if age <= PENDING_GRACE_PERIOD:
                report.pending_skipped_young += 1
                continue
            file_path = op.get("file_path")
            if not file_path:
                # Not a file-tracking op — skip
                continue
            if not self._op_in_scope(op, file_path):
                # Another checkout's op: leave it to that checkout's reconciler,
                # unless its worktree was removed and nobody will ever finish it.
                checkout_id = checkout_value_from_metadata(op.get("metadata"), "id")
                if checkout_id is not None:
                    if not registrations_read:
                        registered_ids = self._registered_worktree_ids()
                        registrations_read = True
                    orphaned = registered_ids is not None and checkout_id not in registered_ids
                else:
                    root = checkout_value_from_metadata(op.get("metadata"), "root")
                    orphaned = root is not None and not Path(root).is_dir()
                if orphaned:
                    if not dry_run:
                        self._db.update_operation_status(op["op_id"], "STALE_PENDING")
                    report.pending_orphaned += 1
                continue
            abs_path = self._resolve(file_path)
            if abs_path is None:
                continue  # repo-relative key, but no checkout root to resolve it against
            op_type = op.get("op_type")
            op_id = op["op_id"]
            if op_type == "artifact":
                obj_hash = op.get("object_hash") or ""
                recovered = (
                    bool(obj_hash)
                    and self._cas.exists(obj_hash)
                    and _file_matches_hash(abs_path, obj_hash)
                )
                if recovered:
                    if not dry_run:
                        try:
                            self._db.finalize_operation(op_id)
                        except Exception as exc:
                            report.errors.append(
                                f"artifact finalize failed for {op_id}: {exc}"
                            )
                            continue
                    report.pending_recovered += 1
                else:
                    if not dry_run:
                        self._db.update_operation_status(op_id, "STALE_PENDING")
                    report.pending_stale += 1
            elif op_type == "invalidation":
                file_gone = not Path(abs_path).exists()
                if file_gone:
                    if not dry_run:
                        targets = self._db.get_invalidated_targets(op_id)
                        self._db.begin_immediate()
                        try:
                            self._db.finalize_operation(op_id)
                            for target_id in targets:
                                self._db.update_operation_status(target_id, "INVALIDATED")
                            self._db.commit()
                        except Exception as exc:
                            self._db.rollback()
                            report.errors.append(
                                f"invalidation recovery failed for {op_id}: {exc}"
                            )
                            continue
                    report.pending_recovered += 1
                else:
                    if not dry_run:
                        self._db.update_operation_status(op_id, "STALE_PENDING")
                    report.pending_stale += 1

        # Rule 4: Tier demotion — ops not accessed within threshold become inactive
        if not dry_run:
            report.demoted_to_inactive = self._db.demote_inactive(threshold_days=30)
            self._db.commit()
        else:
            pulse_row = self._db.execute(
                "SELECT MAX(COALESCE(last_accessed, created_at)) FROM operations"
            ).fetchone()
            if pulse_row and pulse_row[0]:
                row = self._db.execute(
                    """SELECT COUNT(*) FROM operations
                       WHERE memory_tier = 'long_term'
                         AND (julianday(?) - julianday(COALESCE(last_accessed, created_at))) > 30""",
                    (pulse_row[0],),
                ).fetchone()
                report.demoted_to_inactive = row[0] if row else 0

        return report
