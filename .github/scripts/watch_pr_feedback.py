"""Notify the maintainer of feedback posted to any open PR since activation."""

import json
import os
import re
import subprocess
from datetime import datetime

BOT_LOGIN = "github-actions[bot]"
MARKER = re.compile(r"<!-- hgp-pr-feedback ((?:[cir][0-9]+ ?)+) -->")


def api(endpoint, *, paginate=False):
    args = ["gh", "api", endpoint]
    if paginate:
        args.extend(("--paginate", "--jq", ".[] | @json"))
    output = subprocess.check_output(args, text=True)
    if paginate:
        return [json.loads(line) for line in output.splitlines()]
    return json.loads(output)


def feedback(pr_url, comments, inline, reviews, since):
    """Return notification links keyed by feedback type and GitHub ID."""
    items = []
    for prefix, entries, timestamp_key, label, anchor in (
        ("c", comments, "created_at", "PR comment", "issuecomment"),
        ("i", inline, "created_at", "Inline review comment", "discussion_r"),
        ("r", reviews, "submitted_at", "Submitted review", "pullrequestreview"),
    ):
        for entry in entries:
            if (entry.get("user") or {}).get("login") == BOT_LOGIN:
                continue
            if prefix == "r" and entry["state"] == "PENDING":
                continue
            created = entry.get(timestamp_key)
            if not created or datetime.fromisoformat(created) < since:
                continue
            number = entry["id"]
            fragment = f"{anchor}{number}" if prefix == "i" else f"{anchor}-{number}"
            items.append((f"{prefix}{number}", f"- [{label}]({pr_url}#{fragment})"))
    return items


def notify_pr(repo, pr_number, since):
    issue = f"repos/{repo}/issues/{pr_number}/comments"
    pull = f"repos/{repo}/pulls/{pr_number}"
    comments = api(f"{issue}?per_page=100", paginate=True)
    sent = {
        key
        for comment in comments
        if (comment.get("user") or {}).get("login") == BOT_LOGIN
        for match in MARKER.finditer(comment["body"])
        for key in match[1].split()
    }
    items = feedback(
        f"https://github.com/{repo}/pull/{pr_number}",
        comments,
        api(f"{pull}/comments?per_page=100", paginate=True),
        api(f"{pull}/reviews?per_page=100", paginate=True),
        since,
    )
    pending = [(key, link) for key, link in items if key not in sent]
    # Keep each notification below GitHub's comment size limit.
    for offset in range(0, len(pending), 100):
        batch = pending[offset : offset + 100]
        links = "\n".join(link for _, link in batch)
        marker = " ".join(key for key, _ in batch)
        body = f"@wgsim New feedback is ready to review.\n\n{links}\n\n<!-- hgp-pr-feedback {marker} -->"
        subprocess.run(
            ["gh", "api", "--method", "POST", issue, "--raw-field", f"body={body}", "--silent"],
            check=True,
        )


def main():
    repo = os.environ["GH_REPO"]
    workflow = api(f"repos/{repo}/actions/workflows/pr-feedback-watch.yml")
    since = datetime.fromisoformat(workflow["created_at"])
    failed = False
    for pull in api(f"repos/{repo}/pulls?state=open&per_page=100", paginate=True):
        try:
            notify_pr(repo, pull["number"], since)
        except subprocess.CalledProcessError:
            # An API failure for one PR should not block notifications for the rest.
            print(f"Could not check feedback for PR #{pull['number']}", flush=True)
            failed = True
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
