"""PreToolUse hook for Antigravity CLI (agy): keep native file writes inside HGP.

agy protocol (always exit 0; hooks.json command, run with the hooks.json directory as cwd):
  Input:  stdin JSON, camelCase: {"toolCall": {"id", "name", "args"}, "cwd", "workspacePaths",
          "transcriptPath", "hookEventName", ...}
  Output: stdout JSON {"decision": "force_ask" | "ask" | "deny", "reason": "..."}, or nothing
          ("no opinion").

This hook only ever answers force_ask, ask or deny. It never answers allow: that would
auto-approve a tool call that agy would otherwise have gated behind its own permission check.

  * Native file tools (write_to_file, write_file, replace_file_content,
    multi_replace_file_content, edit_file): deny under the block policy
    (HGP_HOOK_BLOCK=1 or ``hgp hook-policy block``); otherwise prompt once the session has
    already used HGP; otherwise stay silent. The prompt is force_ask, which always asks. agy's
    plain ask honours its always-allow cache and its mode (accept-edits approved it unseen).
    HGP_HOOK_ASK picks it: unset, 1 or force -> force_ask; ask -> ask; 0 -> no prompt; any
    other value -> force_ask (an unknown value fails toward prompting).
  * run_command: deny a command that runs HGP's mode or hook-policy command, or that writes
    .hgp/mode or .hgp/hook-policy (those controls are user-only, even while HGP is paused).
    Quotes and backslashes are normalised away and nothing else is skipped, not even a commit
    message (a regex that drops text can be made to hide a command), so a message that mentions
    both words is a false deny; `git commit -F <file>` avoids it. With `$` or a backtick the
    words only need to appear, otherwise they must be adjacent. A command that names a control
    path fails closed: a redirect to it, or any writer word anywhere in the command, is a deny;
    only reading it or redirecting some other file is allowed. This is a heuristic, not a shell
    parser. Deny a command that looks file-mutating under the block policy; otherwise stay
    silent. A command is read-only only with a read-only prefix and no chaining, substitution
    or redirection, and then only the self-mutating options (`find -delete/-exec/...`,
    `git --output`, `rg --pre`) are checked: its other arguments are text.

Silent when HGP is not enabled for the repository, and while HGP mode is advisory or off
(HGP tools then do not write files). HGP_HOOK_BLOCK=1 always wins over the mode file: the
file-based policy yields to a paused mode (as in the other hooks), and since an agent can write
.hgp/mode and .hgp/hook-policy, only the hook's environment is a trusted channel.

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

# ── Read-only command prefixes — skip pattern matching for these ──────────────
_READONLY_PREFIXES = (
    "git log",
    "git status",
    "git diff",
    "git show",
    "git branch",
    "git tag",
    "git ls",
    "git stash list",
    "ls ",
    "ls\t",
    "head ",
    "tail ",
    "grep ",
    "rg ",
    "find ",
    "wc ",
    "diff ",
    "less ",
    "more ",
    "file ",
    "stat ",
    "pwd",
    "date",
    "which ",
    "type ",
    "uname",
)

# ── Mutating patterns (regex) ─────────────────────────────────────────────────
_HIGH_PATTERNS = [
    re.compile(r"\bcp\b"),
    re.compile(r"\bmv\b"),
    re.compile(r"\brm\b"),
    re.compile(r"\btee\b"),
    re.compile(r"\btouch\b"),
    re.compile(r"\binstall\b"),
    re.compile(r"\bmkdir\b"),
    re.compile(r"\brmdir\b"),
    re.compile(r"\bchmod\b"),
    re.compile(r"\bchown\b"),
    re.compile(r"\bln\b"),
    re.compile(r"\btruncate\b"),
    # git commands that rewrite working-tree files
    re.compile(r"\bgit\s+checkout\b"),
    re.compile(r"\bgit\s+restore\b"),
    re.compile(r"\bgit\s+switch\b"),
    re.compile(r"\bgit\s+apply\b"),
    re.compile(r"\bgit\s+revert\b"),
    re.compile(r"\bgit\s+merge\b"),
    re.compile(r"\bgit\s+rebase\b"),
    re.compile(r"\bgit\s+reset\b"),
    # patch tools
    re.compile(r"\bpatch\b"),
]

# Redirects. A read-only command (_is_readonly) has none, so only it skips these.
_REDIRECT_PATTERNS = [
    re.compile(r"(?<![|&])\s*>(?!>)"),  # stdout redirect (not >>)
    re.compile(r">>"),                   # append redirect
    re.compile(r"\bawk\b.*>"),           # awk with redirect
]

_MEDIUM_PATTERNS = [
    # In-place editors: sed with any option cluster holding `i` (-i, -Ei, -i.bak) or --in-place
    # (and its abbreviations); perl with such a cluster (-pi, -i.bak), but not -Mmodule or -Ilib.
    # The scan stops at a pipe, `&` or newline, so `sed ... | grep -i x` is not a hit.
    re.compile(r"\bsed\b[^|&\n]*\s(?:-[A-Za-z]*i|--i[a-z-]*)"),
    re.compile(r"\bperl\b[^|&\n]*\s-(?![MmI])[A-Za-z0-9]*i"),
    re.compile(r"\bdd\b"),
]

# Any sed or perl next to a control path counts as a write, whatever its options (fail closed).
_IN_PLACE_EDITOR_WORDS = re.compile(r"\b(?:sed|perl)\b")

# Options that make a read-only-looking command write files or run another command. These are
# the only patterns a read-only command (_is_readonly) is checked against: `find . -delete`,
# `git diff --output=f`, `rg --pre cmd`. Its other arguments are text (`grep -rn install src`).
_SELF_MUTATING_PATTERNS = [
    re.compile(r"\bfind\b.*\s-(?:delete|exec|execdir|ok|okdir|fprint|fprint0|fprintf|fls)\b", re.S),
    re.compile(r"\bgit\b.*\s--output(?![\w-])", re.S),
    re.compile(r"\brg\b.*\s--pre(?![\w-])", re.S),
    # git options that run a program: --upload-pack, --receive-pack, --exec, --ext-diff and their
    # unambiguous abbreviations (--up, --rec, --exe, --ext), -c key=value (core.pager, alias.x=!cmd,
    # core.fsmonitor, ...) and --config-env. --update*, --recurse-submodules and --extended-regexp
    # are other options and not matched.
    re.compile(
        r"\bgit\b.*\s(?:--(?:up(?!d)|rec(?!u)|exe|ext(?!ended)|config-env)[\w-]*|-c\s*[A-Za-z][\w.-]*=)", re.S,
    ),
]

# Mode and hook policy are user-only controls. These patterns run on a command with its quotes and
# backslashes removed (_normalise), so `'hgp' "mo"de` reads like the plain command.
_HGP_MODE_PATTERN = re.compile(r"\bhgp(?:\.server)?\s+(?:mode|hook-policy)\b")
# Standalone words, for when substitution splits the command (`H=hgp; $H mode off`). A path
# component such as `src/hgp` or `hgp_paths.py` is not the word, but `.../bin/hgp` is.
_HGP_WORD = re.compile(r"(?:(?<![\w./-])|(?<=/bin/))hgp(?:\.server)?(?![\w./-])")
_CONTROL_WORD = re.compile(r"(?<![\w./-])(?:mode|hook-policy)(?![\w./-])")

# The files behind those controls: the agent can write them, so the shell must not.
_CONTROL_PATH = re.compile(r"\.hgp/(?:mode|hook-policy)(?![\w.-])")
_REDIRECT_TARGET = re.compile(r"\d*&?>>?\s*(\S+)")  # `>|` is turned into `>` first

# A command containing one of these chains, substitutes or redirects: never read-only.
_CONTROL_OPERATORS = (";", "&", "|", "\n", "\r", "$(", "`", ">", "<(")
_QUOTES_AND_BACKSLASHES = str.maketrans("", "", "'\"\\")


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

    HGP_HOOK_BLOCK=1 always wins: an agent can write .hgp/mode, not this environment.
    """
    if os.environ.get("HGP_HOOK_BLOCK") == "1":
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


def _resolve_block_mode(start: Path) -> bool:
    """Check HGP_HOOK_BLOCK env var, then fall back to .hgp/hook-policy file."""
    env = os.environ.get("HGP_HOOK_BLOCK")
    if env is not None:
        return env == "1"
    paths = _hgp_paths()

    def shared_hgp_dir(root: Path) -> Path:
        if paths is not None:
            return Path(paths.shared_hgp_dir(root))
        return root / ".hgp"  # partial install without hgp_paths.py

    def is_block(text: str) -> bool:
        if paths is not None:
            return paths.parse_hook_policy(text) is True
        stripped = text.strip()  # partial install without hgp_paths.py
        if stripped.startswith("{"):
            try:
                return json.loads(stripped).get("block_mode") is True
            except (ValueError, AttributeError):
                return False
        return stripped == "block"

    for parent in [start, *start.parents]:
        # Shared policy plus a checkout-local legacy one (written before storage
        # was shared): block if either says so, so upgrading never drops enforcement.
        candidates = dict.fromkeys([shared_hgp_dir(parent) / "hook-policy", parent / ".hgp" / "hook-policy"])
        found = [p for p in candidates if p.exists()]
        if found:
            return any(is_block(p.read_text()) for p in found)
        if (parent / ".git").exists():
            break
    return False


def _is_readonly(command: str) -> bool:
    """Read-only means a read-only prefix and nothing chained, substituted or redirected.

    A prefix alone would exempt `ls && cp a b` or `git status; echo x > f`.
    """
    stripped = command.strip()
    if any(op in stripped for op in _CONTROL_OPERATORS):
        return False
    return any(stripped.startswith(p) for p in _READONLY_PREFIXES)


def _normalise(command: str) -> str:
    """Drop quotes and backslashes so `h"g"p mo\\de` reads like the plain command. Not a shell parser."""
    return command.translate(_QUOTES_AND_BACKSLASHES)


def _detect_mutating(command: str, readonly: bool = False) -> str | None:
    """Return the first matched pattern description, or None if command looks safe.

    A read-only command (see _is_readonly) is checked against the self-mutating options only
    (`find -delete`, `git --output`, `rg --pre`, git's program-running options): its other
    arguments are text, so `grep -rn install src` is fine. Any other command gets every pattern.
    The in-place editors and `dd` are left to the full set: a read-only prefix reaches them only
    as text or through `find -exec` and `rg --pre`, which are caught. `git fetch` and `git remote`
    are not read-only prefixes: `git fetch --upload-pack=cmd` runs a program.
    """
    if readonly:
        patterns = _SELF_MUTATING_PATTERNS
    else:
        patterns = [*_HIGH_PATTERNS, *_SELF_MUTATING_PATTERNS, *_REDIRECT_PATTERNS, *_MEDIUM_PATTERNS]
    for pat in patterns:
        if pat.search(command):
            return pat.pattern
    return None


def _changes_hgp_controls(command: str) -> bool:
    """Whether a command runs HGP's mode or hook-policy command, or hides doing so.

    Quotes and backslashes are dropped first; nothing else is skipped, not even a commit message
    (a regex that removed text could be made to hide a real command). `$` or a backtick means a
    variable or command substitution may be assembling the command, so the words only have to
    appear somewhere. Without one they must be adjacent.
    """
    text = _normalise(command)
    if _HGP_MODE_PATTERN.search(text):
        return True
    return ("$" in text or "`" in text) and bool(_HGP_WORD.search(text) and _CONTROL_WORD.search(text))


def _writes_hgp_control_file(text: str) -> bool:
    """Whether a normalised command may write .hgp/mode or .hgp/hook-policy. Fails closed.

    A command that names a control path is denied if a redirect anywhere in it targets one, or
    if it contains any writer word anywhere (the mutating word patterns, `dd`, the in-place
    editors, the self-mutating options; any `sed` or `perl` counts, whatever its options).
    Operands are not resolved, so `cp .hgp/mode /tmp/x` and
    `cat .hgp/hook-policy | tee log.txt` are denied too. Only reading it, or redirecting some
    other file with no writer word, is allowed.
    """
    if not _CONTROL_PATH.search(text):
        return False
    if any(_CONTROL_PATH.search(target) for target in _REDIRECT_TARGET.findall(text.replace(">|", ">"))):
        return True
    writers = (*_HIGH_PATTERNS, *_SELF_MUTATING_PATTERNS, *_MEDIUM_PATTERNS, _IN_PLACE_EDITOR_WORDS)
    return any(pat.search(text) for pat in writers)


def _ask_decision() -> str | None:
    """Decision for a native write in a session that used HGP, from HGP_HOOK_ASK.

    unset, "1" or "force" -> "force_ask"; "ask" -> "ask"; "0" -> None (no prompt); any other
    value -> "force_ask", so a typo or an unexpected value fails toward prompting.
    """
    value = os.environ.get("HGP_HOOK_ASK")
    if value == "0":
        return None
    return "ask" if value == "ask" else "force_ask"


def _respond(decision: str, reason: str) -> None:
    """Print agy's PreToolUse answer. Only force_ask, ask and deny exist here: never auto-approve."""
    if decision in ("force_ask", "ask", "deny"):
        print(json.dumps({"decision": decision, "reason": reason}))


def _main() -> None:
    try:
        raw: object = json.loads(sys.stdin.read())
    except ValueError:
        sys.exit(0)
    if not isinstance(raw, dict):
        sys.exit(0)
    event = cast(dict[str, Any], raw)
    start = _event_cwd(event)  # one directory for the gate and the policy
    if not _enabled(start):
        sys.exit(0)

    call = event.get("toolCall")
    if not isinstance(call, dict):
        sys.exit(0)
    name = cast(dict[str, Any], call).get("name")
    args = cast(dict[str, Any], call).get("args")
    args = cast(dict[str, Any], args) if isinstance(args, dict) else {}

    if name == "run_command":
        command = args.get("CommandLine")
        if not isinstance(command, str) or not command:
            sys.exit(0)
        text = _normalise(command)
        # Mode and policy control is user-only, so these run before the paused and read-only
        # exits: a leading read-only command must not smuggle a change through, and neither
        # may pausing HGP first.
        if _changes_hgp_controls(command):
            _respond(
                "deny",
                "[HGP] Blocked: `hgp mode` and `hgp hook-policy` must be run by the user directly, "
                "not by the agent. Use `!hgp mode <value>` or `!hgp hook-policy <value>` in your terminal. "
                "If this is only text, such as a commit message, write it to a file and run `git commit -F <file>`.",
            )
            sys.exit(0)
        if _writes_hgp_control_file(text):
            _respond(
                "deny",
                "[HGP] Blocked: `.hgp/mode` and `.hgp/hook-policy` are user-controlled settings; the agent "
                "must not write them from the shell. Ask the user to run `!hgp mode <value>` or "
                "`!hgp hook-policy <value>` instead.",
            )
            sys.exit(0)
        # Advising or blocking native tools is pointless while HGP tools do not write.
        if _paused(start):
            sys.exit(0)
        matched = _detect_mutating(text, readonly=_is_readonly(text))
        if matched is not None and _resolve_block_mode(start):
            _respond(
                "deny",
                f"[HGP] Shell command may mutate files (matched: {matched!r}). "
                "If this writes or deletes tracked files, use hgp_* tools so the "
                "operation is recorded in HGP history.",
            )
        sys.exit(0)

    if name not in HGP_TOOLS:
        sys.exit(0)
    if _paused(start):
        sys.exit(0)

    msg = (
        f"[HGP] Native `{name}` detected. "
        f"Use `{HGP_TOOLS[name]}` instead to record this file operation in HGP history. "
        f"Set HGP_HOOK_BLOCK=1 or run `hgp hook-policy block` to enforce."
    )
    if _resolve_block_mode(start):
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
