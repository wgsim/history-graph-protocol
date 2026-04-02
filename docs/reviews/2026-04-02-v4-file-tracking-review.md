V4 file tracking branch review
Date: 2026-04-02 UTC
Scope: main...feat/v4-file-tracking (commit HEAD b0329b8 at review time)
Assumption: review scope is the committed branch diff only. Current uncommitted workspace changes were excluded from findings and validation.

Summary
- Branch shape: feature branch adds V4 file tracking tools, project-root enforcement, DB schema changes, and Claude/Gemini hook wiring.
- Diff size: 15 files changed, 1240 insertions, 25 deletions.
- Recommendation: do not merge yet.
- Reason: the branch introduces correctness bugs around filesystem/HGP divergence, stale move history, and undocumented API contract changes.

Validation performed
- Reviewed `git diff main...HEAD` and `git show HEAD:<path>` for all changed source, test, and docs files.
- Commit-state syntax check passed for changed Python files via `compile(...)`.
- Commit-state JSON parse check passed for `.claude/settings.json` and `.gemini/settings.json`.
- Supplemental independent review pass from Gemini highlighted the same core risks: non-atomic file mutation, stale move semantics, and raw `file_path` identity.
- Not run: `pytest` against the exact reviewed commit. The local worktree currently contains uncommitted changes in reviewed files, so a normal test run here would not exercise the same code that was reviewed.

Findings
1. High: write/append/edit/delete can mutate the filesystem before HGP recording is durably accepted.
   Code:
   - `src/hgp/server.py` HEAD lines 456-517
   - `src/hgp/server.py` HEAD lines 520-551
   - `src/hgp/server.py` HEAD lines 554-586
   - `src/hgp/server.py` HEAD lines 589-631
   - `src/hgp/server.py` HEAD lines 635-686
   Detail:
   - `hgp_write_file`, `hgp_append_file`, and `hgp_edit_file` write the file first, then call `_record_file_op()`.
   - `_record_file_op()` still performs evidence validation, CAS writes, parent edge inserts, and DB inserts after the file has already changed.
   - `hgp_delete_file()` unlinks the file before starting the DB transaction.
   - Deterministic failure paths exist:
     - invalid `evidence_refs` returns an error after the file was already written
     - missing `parent_op_ids` or bad `previous_op_id` will hit FK failures after the file was already changed/deleted
     - CAS or DB failures also occur after the filesystem side effect
   Impact:
   - The branch can leave real files changed while HGP history is missing or partially recorded.
   - This directly contradicts the V4 documentation claim that file writes are atomic.

2. High: `hgp_move_file()` can move the file without producing consistent old-path/new-path history.
   Code:
   - `src/hgp/server.py` HEAD lines 690-765
   - `docs/tools-reference.md` HEAD lines 882-903
   Detail:
   - The rename happens at lines 713-716 before evidence validation and before `cas.store(...)`.
   - If `evidence_refs` is invalid or exceeds the cap, the tool returns an error after the file has already moved.
   - If `cas.store(...)` fails, the file is already at `new_path` and no HGP operation has been recorded yet.
   - Old-path invalidation depends entirely on the optional caller-supplied `previous_op_id` at lines 742-744.
   - No operation with `file_path=old_path` is created for the move itself, so `hgp_file_history(old_path)` cannot reliably show the move event.
   Impact:
   - A caller can get a moved file with missing history.
   - If `previous_op_id` is omitted, the old path can remain logically active in HGP even though the file no longer exists there.
   - This does not meet the documented contract that the move atomically invalidates the old path's operation.

3. Medium: file identity is stored as the raw caller string, so the same file can split into multiple histories.
   Code:
   - `src/hgp/project.py` HEAD lines 17-53
   - `src/hgp/server.py` HEAD lines 483-492
   - `src/hgp/server.py` HEAD lines 544-545
   - `src/hgp/server.py` HEAD lines 579-580
   - `src/hgp/server.py` HEAD lines 624-625
   - `src/hgp/server.py` HEAD lines 668-668
   - `src/hgp/server.py` HEAD lines 738-740
   - `src/hgp/db.py` HEAD lines 245-256
   - `src/hgp/db.py` HEAD lines 293-334
   Detail:
   - Root checks resolve the path to validate it is inside the project root, but the stored `file_path` is still the original unnormalized input string.
   - Query paths are matched with exact string equality.
   - The implementation does not enforce the documented "absolute path" contract, nor does it canonicalize the stored/query key.
   Impact:
   - `/repo/src/a.py`, `/repo/./src/a.py`, a symlinked path, or a relative path can all point to the same file but create/query different histories.
   - That undermines the main V4 feature: reliable per-file history lookup.

4. Medium: `hgp_query_operations()` has a silent response-schema change and incomplete documentation.
   Code:
   - `src/hgp/server.py` HEAD lines 252-287
   - `docs/tools-reference.md` HEAD lines 122-179
   Detail:
   - The implementation now returns `{"operations": [...]}`.
   - The public docs still say the tool returns a bare list, and the example still shows a bare JSON array.
   - The new `file_path` parameter is implemented in code but missing from the reference section.
   Impact:
   - Existing clients built against the documented schema will break silently.
   - The branch introduces an API change without a compatibility note or migration guidance.

Testing gaps
- `tests/test_file_ops.py` covers happy paths and a few input errors, but it does not cover rollback/partial-failure paths:
  - invalid `evidence_refs` after filesystem mutation
  - bad `parent_op_ids` / `previous_op_id`
  - CAS failure after file write or rename
  - `hgp_move_file()` without `previous_op_id`
- `tests/test_file_ops.py` does not cover path normalization or relative-vs-absolute path collisions.
- `tests/test_db.py` migration coverage is shallow:
  - `test_migration_idempotent` reopens a freshly initialized V4 DB
  - it does not simulate an actual pre-V4 schema missing `file_path`

Open question
- The branch documentation intentionally removes `*.db` from `.gitignore` and treats the HGP DB as versioned project state.
  Code:
  - `.gitignore` diff
  - `docs/architecture.md` HEAD lines 492-502
  Note:
  - This may be intentional design, but it is a meaningful repository-policy change with storage and workflow implications.
  - If that policy is deliberate, it should likely be approved explicitly rather than arriving as an incidental part of the V4 tool work.

Final assessment
- The branch is directionally useful, but the current implementation is not yet safe to merge.
- The blockers are correctness and contract issues, not style or incidental cleanup.
- The minimum acceptable next step is to make filesystem/HGP updates failure-atomic, canonicalize file identity, and either preserve or explicitly version the `hgp_query_operations` response contract.
