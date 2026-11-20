"""PreToolUse hook for Antigravity CLI (agy): keep native file writes inside HGP.

agy protocol (always exit 0; hooks.json command, run with the hooks.json directory as cwd):
  Input:  stdin JSON, camelCase: {"toolCall": {"id", "name", "args"}, "cwd", "workspacePaths",
          "transcriptPath", "hookEventName", ...}
  Output: stdout JSON {"decision": "force_ask" | "ask" | "deny", "reason": "...", "overwrite": {...}},
          or nothing ("no opinion"). ``overwrite`` is optional and merged into the tool's args.

This hook only ever answers force_ask, ask or deny. It never answers allow: that would
auto-approve a tool call that agy would otherwise have gated behind its own permission check.

  * Native file tools (write_to_file, write_file, replace_file_content,
    multi_replace_file_content, edit_file): judged by the file they write (args.Path or
    args.TargetFile), not by the session's cwd. Decision order, first match wins:
      1. The target has a path segment exactly `.hgp`: deny, always (even paused, in advisory, with
         HGP_HOOK_ASK=0, or when HGP is not enabled): .hgp/ is HGP-internal and user-controlled.
      2. HGP is not enabled for the repository: silent.
      3. The target is inside artifactDirectoryPath (agy's own artifacts): silent.
      4. The target is outside the session's checkout (another repo or worktree, /tmp): silent,
         since HGP could not record it. Global mode (HGP_GLOBAL_MODE) has no such limit.
      5. HGP mode is advisory or off (and HGP_HOOK_BLOCK does not enforce): silent.
      6. The level is block or strict (policy file, or HGP_HOOK_BLOCK: 0 advisory, strict, any
         other value block): deny.
      7. The session already used HGP: prompt. Otherwise silent.
    A relative target is read against both the agent's cwd and the checkout root; it is in scope
    unless it is outside the checkout both ways (steps 1, 3 and 4 use every reading).
    A missing or non-string target falls back to the cwd-based behaviour (steps 2 and 5 to 7).
    The prompt is force_ask, which always asks. agy's plain ask honours its always-allow cache
    and its mode (accept-edits approved it unseen). HGP_HOOK_ASK picks it: unset, 1 or force ->
    force_ask; ask -> ask; 0 -> no prompt; any other value -> force_ask (an unknown value fails
    toward prompting).
  * run_command: deny a command that runs HGP's mode or hook-policy command, or that writes
    .hgp/mode or .hgp/hook-policy (those controls are user-only, even while HGP is paused).
    Quotes and backslashes are normalised away and nothing else is skipped, not even a commit
    message (a regex that drops text can be made to hide a command), so a message that mentions
    both words is a false deny; `git commit -F <file>` avoids it. With `$` or a backtick the
    words only need to appear, otherwise they must be adjacent. A command that names a control
    path fails closed: a redirect to it, or any writer word anywhere in the command, is a deny;
    only reading it or redirecting some other file is allowed. This is a heuristic, not a shell
    parser. Deny a command that looks file-mutating only at the strict level (block stops native
    file tools and leaves shell work alone); otherwise stay silent. A command is read-only only
    with a read-only prefix and no chaining, substitution or redirection, and then only the
    self-mutating options (`find -delete/-exec/...`, `git --output`, `rg --pre`) are checked:
    its other arguments are text. A mutating command that is let through (not denied) in an
    enabled, unpaused git checkout also records a `git status` baseline for the PostToolUse hook
    (agy_state.py); this hook stays silent about it. The analysis itself is hgp_paths.shell_*,
    shared with the Claude and Codex hooks; without hgp_paths.py beside this hook a small built-in
    check (the fallback block below) keeps the guards and a coarse mutating check.
  * invoke_subagent: put the parent session's root op id in front of the subagent's Prompt, so
    its HGP operations link to this session's history. The id comes from
    ``.hgp/context-<conversationId>.json`` (hgp_set_context, with the conversation id as
    session_id). Live capture (agy 1.5.13): ``overwrite`` only takes effect with ask or
    force_ask (a bare overwrite is denied) and invoke_subagent is not permission-gated, so
    the answer is ``ask`` plus ``overwrite`` (no prompt appeared in the capture). Skipped for the research,
    browser and image-generator types, the branch and share workspaces, a prompt that already
    carries the prefix, or when no valid context exists.

Levels (.hgp/hook-policy ``{"block_mode": bool, "block_shell": bool}``, or HGP_HOOK_BLOCK):
advisory, block (native file tools) and strict (the shell too). HGP_HOOK_BLOCK wins over the
file: 0 -> advisory, strict -> strict, any other value -> block. The file-based policy yields
to a paused mode (as in the other hooks), while any HGP_HOOK_BLOCK other than 0 does not: an
agent can write .hgp/mode and .hgp/hook-policy, so only the hook's environment is trusted.

The agent's own HGP calls arrive as ``call_mcp_tool`` and are never matched here.
"""
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, cast

# Native file tool -> the HGP tool the agent should use instead. agy's tool name is the
# lowercased step type: older agents emit write_to_file/replace_file_content, agy 1.5.x
# emits write_file/edit_file.
HGP_TOOLS = {
    "write_to_file": "hgp_write_file",
    "write_file": "hgp_write_file",
    "replace_file_content": "hgp_edit_file",
    "multi_replace_file_content": "hgp_edit_file",
    "edit_file": "hgp_edit_file",
}

# --- hgp-shell-fallback begin: keep this block identical in the agy, Claude and Codex hooks (a test checks) ---
# The shell analysis lives in hgp_paths (shell_*), installed beside every hook. This block is what a hook
# still has when hgp_paths.py is missing (a broken install): the always-deny guards and a coarse mutating
# check. It may deny more than hgp_paths does, never less than the guards. The messages are shared.
_MODE_DENY = (
    "[HGP] Blocked: `hgp mode` and `hgp hook-policy` must be run by the user directly, not by the agent. "
    "Use `!hgp mode <value>` or `!hgp hook-policy <value>` in your terminal. "
    "If this is only text, such as a commit message, write it to a file and run `git commit -F <file>`."
)
_CONTROL_DENY = (
    "[HGP] Blocked: `.hgp/mode` and `.hgp/hook-policy` are user-controlled settings; the agent "
    "must not write them from the shell. Ask the user to run `!hgp mode <value>` or "
    "`!hgp hook-policy <value>` instead."
)
_FB_QUOTES = str.maketrans("", "", "'\"\\")
_FB_MODE = re.compile(r"\bhgp(?:\.server)?\s+(?:mode|hook-policy)\b")
_FB_HGP_WORD = re.compile(r"(?:(?<![\w./-])|(?<=/bin/))hgp(?:\.server)?(?![\w./-])")
_FB_CONTROL_WORD = re.compile(r"(?<![\w./-])(?:mode|hook-policy)(?![\w./-])")
_FB_CONTROL_PATH = re.compile(r"\.hgp/(?:mode|hook-policy)(?![\w.-])")
_FB_WRITER = re.compile(
    r"\b(?:cp|mv|rm|tee|touch|install|mkdir|rmdir|chmod|chown|ln|truncate|patch|dd|sed|perl|git|find|rg|awk)\b|>"
)
_FB_MUTATING = re.compile(
    r"\b(?:cp|mv|rm|tee|touch|install|mkdir|rmdir|chmod|chown|ln|truncate|patch|dd|sed|perl)\b"
    r"|\bgit\s+(?:checkout|restore|switch|apply|revert|merge|rebase|reset)\b|>"
)


def _fb_control_violation(command: str) -> str | None:
    text = command.translate(_FB_QUOTES)
    if _FB_MODE.search(text):
        return "mode"
    if ("$" in text or "`" in text) and _FB_HGP_WORD.search(text) and _FB_CONTROL_WORD.search(text):
        return "mode"
    # A control path next to any writer word or redirect: fail closed (operands are not resolved).
    return "control-file" if _FB_CONTROL_PATH.search(text) and _FB_WRITER.search(text) else None


def _fb_mutating(command: str) -> str | None:
    found = _FB_MUTATING.search(command.translate(_FB_QUOTES))
    return found.group(0) if found else None


# --- hgp-shell-fallback end ---


def _hgp_paths() -> Any:
    """Return hgp_paths: installed beside this hook, else from the hgp package, else None."""
    try:
        import hgp_paths  # pyright: ignore[reportMissingImports]
    except ImportError:
        try:
            from hgp.hooks import hgp_paths
        except ImportError:
            return None
    return hgp_paths


def _agy_state() -> Any:
    """Return agy_state (shell tracking and subagent helpers): beside this hook, else the package, else None.

    Without it those two features are off; the hook's guarding does not depend on it.
    """
    try:
        import agy_state  # pyright: ignore[reportMissingImports]
    except ImportError:
        try:
            from hgp.hooks.agy import agy_state
        except ImportError:
            return None
    return agy_state


def _enabled(start: Path) -> bool:
    """Return whether HGP is enabled for ``start`` (its storage .hgp/ exists)."""
    check = getattr(_hgp_paths(), "hgp_enabled", None)
    if check is not None:
        return bool(check(start))
    if os.environ.get("HGP_GLOBAL_MODE"):
        return True  # global mode records everywhere, as in the server
    for parent in [start, *start.parents]:  # no hgp_paths: walk up to the checkout root
        if (parent / ".git").exists():
            return (parent / ".hgp").is_dir()
    return (start / ".hgp").is_dir()  # outside git only this directory's store counts


def _paused(start: Path) -> bool:
    """Return whether HGP mode is advisory or off: HGP tools then do not write files.

    HGP_HOOK_BLOCK wins unless it is 0: an agent can write .hgp/mode, not this environment.
    """
    env = os.environ.get("HGP_HOOK_BLOCK")
    if env is not None and env != "0":
        return False
    mode = getattr(_hgp_paths(), "hgp_mode", None)
    return mode is not None and mode(start) in ("advisory", "off")


def _unwrap(value: object) -> object:
    """Decode a JSON-encoded string value (agy's transcript.jsonl stores tool args that way)."""
    if isinstance(value, str) and value.startswith(('"', "{")):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _hgp_loaded(event: object) -> bool:
    """Return whether this session already used HGP tools.

    True once the transcript shows a ``call_mcp_tool`` call whose ServerName is ``hgp``
    (a plain string in transcript_full.jsonl, JSON-encoded in transcript.jsonl).
    Fail-safe: a missing or unreadable transcript reads as False.
    """
    path = cast(dict[str, Any], event).get("transcriptPath") if isinstance(event, dict) else None
    if not isinstance(path, str) or not path:
        return False
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                # `hgp` alone matches nearly every line in an HGP repository; the MCP call marker is rarer
                if "call_mcp_tool" not in line or "hgp" not in line:
                    continue
                try:
                    record: object = json.loads(line)
                except ValueError:
                    continue
                calls = cast(dict[str, Any], record).get("tool_calls") if isinstance(record, dict) else None
                if not isinstance(calls, list):
                    continue
                for call in cast(list[Any], calls):
                    if not isinstance(call, dict) or cast(dict[str, Any], call).get("name") != "call_mcp_tool":
                        continue
                    args = _unwrap(cast(dict[str, Any], call).get("args"))
                    if isinstance(args, dict) and _unwrap(cast(dict[str, Any], args).get("ServerName")) == "hgp":
                        return True
    except OSError:
        return False
    return False


def _event_cwd(event: object) -> Path:
    """HGP_PROJECT_ROOT when it names a directory (as in the server), else the agent's cwd.

    The hook process itself runs in the hooks.json directory, so the event's ``cwd``
    (then its first workspace path) is what identifies the repository.
    """
    env = os.environ.get("HGP_PROJECT_ROOT")
    if env and Path(env).is_dir():
        return Path(env).resolve()
    fields = cast(dict[str, Any], event) if isinstance(event, dict) else {}
    cwd = fields.get("cwd")
    if isinstance(cwd, str) and cwd:
        return Path(cwd)
    workspaces = fields.get("workspacePaths")
    if isinstance(workspaces, list) and workspaces:
        first = cast(list[Any], workspaces)[0]
        if isinstance(first, str) and first:
            return Path(first)
    return Path.cwd()


_LEVELS = ("advisory", "block", "strict")


def _env_level() -> str | None:
    """The level HGP_HOOK_BLOCK forces (trusted: it wins over the policy file), or None when unset.

    0 -> advisory; "strict" -> strict; any other value (1, block, a typo, empty) -> block, so an
    unexpected value fails toward enforcing.
    """
    env = os.environ.get("HGP_HOOK_BLOCK")
    if env is None:
        return None
    if env == "0":
        return "advisory"
    return "strict" if env == "strict" else "block"


def _level_of(text: str) -> str:
    """The level a .hgp/hook-policy text says: advisory, block or strict (unrecognised: advisory)."""
    reader = getattr(_hgp_paths(), "hook_policy_level", None)
    if reader is not None:
        return reader(text) or "advisory"
    stripped = text.strip()  # partial install without hgp_paths.py
    if stripped.startswith("{"):
        try:
            data: object = json.loads(stripped)
        except ValueError:
            return "advisory"
        fields = cast(dict[str, Any], data) if isinstance(data, dict) else {}
        if fields.get("block_mode") is not True:
            return "advisory"
        return "strict" if fields.get("block_shell") is True else "block"
    return "block" if stripped == "block" else "advisory"


def _block_level(start: Path) -> str:
    """advisory, block (native file tools) or strict (the shell too): HGP_HOOK_BLOCK, then the policy file."""
    forced = _env_level()
    if forced is not None:
        return forced
    paths = _hgp_paths()

    def shared_hgp_dir(root: Path) -> Path:
        if paths is not None:
            return Path(paths.shared_hgp_dir(root))
        return root / ".hgp"  # partial install without hgp_paths.py

    for parent in [start, *start.parents]:
        # Shared policy plus a checkout-local legacy one (written before storage was shared):
        # the stricter of the two counts, so upgrading never drops enforcement.
        candidates = dict.fromkeys([shared_hgp_dir(parent) / "hook-policy", parent / ".hgp" / "hook-policy"])
        found = [p for p in candidates if p.exists()]
        if found:
            return max((_level_of(p.read_text()) for p in found), key=_LEVELS.index)
        if (parent / ".git").exists():
            break
    return "advisory"


def _is_readonly(command: str) -> bool:
    """Read-only means a read-only prefix and nothing chained, substituted or redirected (hgp_paths)."""
    check = getattr(_hgp_paths(), "shell_is_readonly", None)
    return bool(check(command)) if check is not None else False  # without hgp_paths nothing is exempt


def _control_violation(command: str) -> str | None:
    """"mode" for a command that changes HGP's mode or hook policy, "control-file" for a shell write to
    .hgp/mode or .hgp/hook-policy, else None. Both are user-only (hgp_paths.shell_control_violation)."""
    check = getattr(_hgp_paths(), "shell_control_violation", None)
    return check(command) if check is not None else _fb_control_violation(command)


def _mutating_pattern(command: str) -> str | None:
    """The pattern that makes the command look file-mutating, or None (hgp_paths.shell_detect_mutating).

    A read-only command is checked against the self-mutating options only: its other arguments are text.
    """
    paths = _hgp_paths()
    detect = getattr(paths, "shell_detect_mutating", None)
    if detect is None:
        return _fb_mutating(command)
    text = paths.shell_normalise(command)
    return detect(text, readonly=_is_readonly(text))


def _ask_decision() -> str | None:
    """Decision for a native write in a session that used HGP, from HGP_HOOK_ASK.

    unset, "1" or "force" -> "force_ask"; "ask" -> "ask"; "0" -> None (no prompt); any other
    value -> "force_ask", so a typo or an unexpected value fails toward prompting.
    """
    value = os.environ.get("HGP_HOOK_ASK")
    if value == "0":
        return None
    return "ask" if value == "ask" else "force_ask"


def _respond(decision: str, reason: str, overwrite: dict[str, str] | None = None) -> None:
    """Print agy's PreToolUse answer. Only force_ask, ask and deny exist here: never auto-approve.

    ``overwrite`` (tool args to replace) rides along with ask or force_ask: agy ignores it when
    there is no decision and denies a bare one.
    """
    if decision in ("force_ask", "ask", "deny"):
        answer: dict[str, Any] = {"decision": decision, "reason": reason}
        if overwrite is not None:
            answer["overwrite"] = overwrite
        print(json.dumps(answer))


def _session_root(start: Path) -> Path:
    """The session's checkout root, as the server derives its project root.

    HGP_PROJECT_ROOT when it names a directory, else the checkout containing ``start``, else
    ``start`` itself (outside git the server's store is the working directory's).
    """
    reader = getattr(_hgp_paths(), "checkout_root", None)
    if reader is not None:
        root = reader(start)
        return Path(root) if root is not None else start
    env = os.environ.get("HGP_PROJECT_ROOT")  # partial install without hgp_paths.py
    if env and Path(env).is_dir():
        return Path(env).resolve()
    for parent in [start, *start.parents]:
        if (parent / ".git").exists():
            return parent
    return start


def _in_hgp_dir(target: Path) -> bool:
    """Whether ``target`` has a path segment exactly `.hgp` (the server's HGP_INTERNAL_PATH rule).

    Checked on the lexically normalised path and on the resolved one, so `a/../.hgp/x` and a
    symlink into .hgp count. The comparison ignores case: a case-insensitive filesystem would
    otherwise let `.HGP/mode` reach the same directory.
    """
    candidates = [Path(os.path.normpath(target))]
    try:
        candidates.append(target.resolve())
    except (OSError, RuntimeError, ValueError):
        pass
    return any(part.casefold() == ".hgp" for path in candidates for part in path.parts)


def _inside(target: Path, root: Path) -> bool:
    """Whether ``target`` is ``root`` or below it, after resolving symlinks (the tail may not exist yet)."""
    try:
        resolved, base = target.resolve(), root.resolve()
    except (OSError, RuntimeError, ValueError):
        return True  # cannot tell: act as if it were in scope
    return resolved == base or base in resolved.parents


_TARGET_KEYS = {
    "write_file": ("Path", "TargetFile"), "edit_file": ("Path", "TargetFile"),
    "write_to_file": ("TargetFile", "Path"), "replace_file_content": ("TargetFile", "Path"),
    "multi_replace_file_content": ("TargetFile", "Path"),
}


def _targets(name: str, args: dict[str, Any], root: Path, cwd: Path) -> list[Path]:
    """The file(s) a native tool may write: args.Path (write_file, edit_file) or args.TargetFile (the others).

    The other key is read as a fallback. Empty when no key holds a non-empty string. A leading
    `~` is the home directory. An absolute path is one target. A relative path has two readings:
    the tool writes it against the agent's ``cwd``, the server resolves it against ``root``; the
    hook keeps both, so `../x` from <root>/sub (really <root>/x) is not mistaken for <root>/../x.
    """
    for key in _TARGET_KEYS[name]:
        raw = args.get(key)
        if isinstance(raw, str) and raw:
            path = Path(os.path.expanduser(raw))
            if path.is_absolute():
                return [path]
            return list(dict.fromkeys([cwd / path, root / path]))
    return []


def _in_artifact_dir(targets: list[Path], event: dict[str, Any], root: Path) -> bool:
    """Whether every reading of the target is in agy's own artifact directory (artifactDirectoryPath).

    An artifact directory that contains the checkout (a misconfigured `/` or `~`) exempts nothing.
    """
    raw = event.get("artifactDirectoryPath")
    if not isinstance(raw, str) or not raw or not Path(raw).is_absolute():
        return False
    artifacts = Path(raw)
    return all(_inside(t, artifacts) for t in targets) and not _inside(root, artifacts)


def _event_dir(event: dict[str, Any], start: Path) -> Path:
    """The agent's cwd, where its tools resolve a relative path; ``start`` when the event has none."""
    cwd = event.get("cwd")
    return Path(cwd) if isinstance(cwd, str) and cwd and Path(cwd).is_absolute() else start


def _main() -> None:
    try:
        raw: object = json.loads(sys.stdin.read())
    except ValueError:
        sys.exit(0)
    if not isinstance(raw, dict):
        sys.exit(0)
    event = cast(dict[str, Any], raw)
    start = _event_cwd(event)  # one directory for the gate and the policy

    call = event.get("toolCall")
    if not isinstance(call, dict):
        sys.exit(0)
    name = cast(dict[str, Any], call).get("name")
    args = cast(dict[str, Any], call).get("args")
    args = cast(dict[str, Any], args) if isinstance(args, dict) else {}

    root = None
    targets: list[Path] = []
    if isinstance(name, str) and name in HGP_TOOLS:
        root = _session_root(start)
        targets = _targets(name, args, root, _event_dir(event, start))
        # A write into .hgp/ is denied whatever the policy, the mode or whether HGP is enabled.
        if any(_in_hgp_dir(t) for t in targets):
            _respond(
                "deny",
                f"[HGP] Blocked: `{name}` targets `.hgp/`, which is HGP-internal and user-controlled "
                "(database, mode, hook-policy). Agents must not write it, with a native tool or an HGP one; "
                "the user changes HGP settings with the `hgp` CLI in their own terminal.",
            )
            sys.exit(0)
    if not _enabled(start):
        sys.exit(0)

    if name == "run_command":
        command = args.get("CommandLine")
        if not isinstance(command, str) or not command:
            sys.exit(0)
        # Mode and policy control is user-only, so these run before the paused and read-only
        # exits: a leading read-only command must not smuggle a change through, and neither
        # may pausing HGP first.
        violation = _control_violation(command)
        if violation is not None:
            _respond("deny", _MODE_DENY if violation == "mode" else _CONTROL_DENY)
            sys.exit(0)
        # Advising or blocking native tools is pointless while HGP tools do not write.
        if _paused(start):
            sys.exit(0)
        # Only strict blocks the shell: block stops native file tools and leaves shell work alone.
        matched = _mutating_pattern(command)
        if matched is not None and _block_level(start) == "strict":
            _respond(
                "deny",
                f"[HGP] Shell command may mutate files (matched: {matched!r}). "
                "If this writes or deletes tracked files, use hgp_* tools so the "
                "operation is recorded in HGP history.",
            )
        elif matched is not None:
            # Let it through, and note the git status so PostToolUse can tell what it changed.
            state = _agy_state()
            if state is not None:
                state.write_baseline(event.get("conversationId"), event.get("stepIdx"), _session_root(start), matched)
        sys.exit(0)

    if name == "invoke_subagent":
        state = _agy_state()
        if state is not None and not _paused(start):
            prompt = state.subagent_prompt(args, event.get("conversationId"), state.hgp_dirs(_session_root(start)))
            if prompt is not None:
                _respond(
                    "ask",
                    "[HGP] Passing this session's root op to the subagent so its HGP operations link to this history.",
                    {"Prompt": prompt},
                )
        sys.exit(0)

    if not isinstance(name, str) or name not in HGP_TOOLS:
        sys.exit(0)
    # HGP records only files under the session's checkout, and agy's own artifacts are not
    # project files; anywhere else the hook has no say (global mode has no checkout limit).
    if root is not None and targets:
        if _in_artifact_dir(targets, event, root):
            sys.exit(0)
        # In scope unless every reading of the target leaves the checkout.
        if not os.environ.get("HGP_GLOBAL_MODE") and not any(_inside(t, root) for t in targets):
            sys.exit(0)
    if _paused(start):
        sys.exit(0)

    msg = (
        f"[HGP] Native `{name}` detected. "
        f"Use `{HGP_TOOLS[name]}` instead to record this file operation in HGP history. "
        f"Set HGP_HOOK_BLOCK=1 or run `hgp hook-policy block` to enforce."
    )
    if _block_level(start) != "advisory":  # block and strict both stop native file tools
        _respond("deny", msg)
    # A session that uses HGP prompts the user before a native write (HGP_HOOK_ASK=0 disables).
    elif (decision := _ask_decision()) is not None and _hgp_loaded(event):
        _respond(decision, msg)
    sys.exit(0)


def main() -> None:
    """Fail-open entry point: an unexpected error never blocks the agent's tool call."""
    try:
        _main()
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)


if __name__ == "__main__":
    main()
