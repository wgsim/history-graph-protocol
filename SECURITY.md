# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.3.x   | ✅        |
| < 0.3   | ❌        |

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Report vulnerabilities via
[GitHub's private vulnerability reporting](https://github.com/wgsim/history-graph-protocol/security/advisories/new),
or by email to the maintainer via the address listed on the
[GitHub profile](https://github.com/wgsim).

Include:

- A description of the vulnerability and its potential impact
- Steps to reproduce or a minimal proof-of-concept
- Affected version(s)

You can expect an acknowledgement within **7 days** and a resolution timeline
within **30 days** for confirmed issues.

## Scope

HGP is a local MCP server that runs as a subprocess of an AI agent host. It
reads and writes files within a single git repository and stores an SQLite
database in `.hgp/`. It does not make outbound network connections.

Out of scope:

- Vulnerabilities in MCP host applications (Claude Code, Gemini CLI, Codex, etc.)
- Issues arising from running HGP with elevated system privileges (not its
  intended use)
- Denial-of-service via malformed inputs that are already rejected with a
  controlled error
