Findings

1. High: `Path.resolve(strict=True)` is still underspecified for create/write targets.
- In both documents, target paths are described as being resolved with `Path.resolve(strict=True)`.
- That works for existing paths, but it fails for legitimate writes to new files because the leaf does not exist yet.
- The spec needs an explicit rule: resolve the existing parent chain strictly, validate containment against `working_tree_root`, then join the final leaf name without requiring the leaf to pre-exist.
- Without that, direct implementation will either reject valid writes or drift into ad hoc behavior per call site.

2. High: Git layout detection is still defined by substring matching, not by structural path rules.
- In `implementation_plan.md` / “Core Path Resolution”, the submodule and worktree branches are keyed off “contains `/modules/`” and “contains `/worktrees/`”.
- That is brittle across platforms and can misclassify paths that merely contain those strings.
- The spec should require component-wise checks against canonical Git layouts after resolution, not substring checks.
- This matters because root authority is derived from that branch.

3. Medium: The storage schema for tracked file identity is still ambiguous.
- The plan says store paths relative to `working_tree_root` “or as a `(worktree_id, relative_path)` tuple”.
- Those are materially different models.
- If file identity is repo-relative, cross-worktree history naturally converges.
- If file identity is keyed by `worktree_id`, the same logical repo file fragments across worktrees.
- The design should pick one canonical representation now; otherwise implementation and migrations will diverge.

4. Medium: WAL fallback caching needs an explicit invalidation/re-probe contract.
- The `.hgp/nfs_mode` cache avoids repeated failed WAL probes, which is good.
- But the spec does not say when or how that flag is cleared after transient failures, mount changes, or repository relocation.
- Without that, a repo can get stuck in `TRUNCATE` long after the underlying condition is gone.
- This is not a blocker, but it should be specified before implementation.

5. Medium: Atomic replace is specified, but crash durability is not.
- Same-directory temp file + `os.replace()` gives atomic visibility, not full durability.
- If you want the CAS store, `ignore_list`, and similar metadata files to survive power loss or host crash cleanly, the spec should require flushing the temp file and fsyncing the containing directory before/after replace as appropriate.
- If durability is intentionally out of scope, say that explicitly.

6. Low: Secret protection is policy-only, not control-backed.
- “No credentials, API tokens, or secrets may be stored” is a requirement, but the docs do not define an enforcement point.
- If you do not intend to implement redaction/validation before DB/CAS writes, the document should downgrade this to a documented operational constraint rather than a security guarantee.

7. Low: A few maturity gaps remain in operational semantics.
- `uv latest` is not reproducible enough for an “official technical stack”; pin tested ranges.
- `HOOK_CAPTURE_FAILED` is listed in the error taxonomy, but fail-open hooks are specified to swallow errors and exit `0`; define which downstream component can actually emit that code.
- The error taxonomy would be more implementable if each error had retryability/operator-action semantics.

Open question I would resolve before coding
- Submodule detection policy itself is reasonable, but the detection mechanism must be structural, not substring-based.

Final sign-off

This is close, but I would not call it completely airtight yet.

I would approve implementation with caveats after tightening the four structural points above:
- non-existent target path handling,
- structural Git layout detection,
- one canonical file-identity schema,
- WAL fallback cache invalidation.

Everything else is second-order hardening and can be documented as explicit constraints if you want to keep the first implementation tight. I also cross-checked the design with Gemini and Claude via `model-bridge`; their independent concerns overlapped most strongly on WAL fallback behavior, path/schema ambiguity, and a few remaining portability edges.

--- [Routing Log] ---
[1] Primary (codex): Trying...
    [SUCCESS]