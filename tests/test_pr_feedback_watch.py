"""Coverage for the scheduled PR feedback workflow."""

import runpy
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.mark.parametrize(
    ("kind", "fragment"),
    [
        ("comment", "issuecomment-21"),
        ("inline", "discussion_r21"),
        ("review", "pullrequestreview-21"),
    ],
)
def test_feedback_notifies_once(kind, fragment):
    script = Path(__file__).parents[1] / ".github/scripts/watch_pr_feedback.py"
    notify = runpy.run_path(str(script))["notify_pr"]
    scope = notify.__globals__
    repo = "wgsim/history-graph-protocol"
    cutoff = scope["datetime"].fromisoformat("2026-11-18T19:00:00.000Z")
    recent = "2026-11-18T19:01:00Z"
    old = {"id": 20, "created_at": "2026-11-18T18:59:00Z", "body": "before activation"}
    existing_bot = {"id": 23, "created_at": recent, "body": "PR alert", "user": {"login": scope["BOT_LOGIN"]}}
    item = {"id": 21, "created_at": recent, "submitted_at": recent, "state": "COMMENTED", "body": "feedback"}
    comments = [old, existing_bot]
    inline = []
    reviews = [{"id": 22, "submitted_at": None, "state": "PENDING"}]
    {"comment": comments, "inline": inline, "review": reviews}[kind].append(item)
    issue_endpoint = f"repos/{repo}/issues/42/comments"
    contents = {
        f"{issue_endpoint}?per_page=100": comments,
        f"repos/{repo}/pulls/42/comments?per_page=100": inline,
        f"repos/{repo}/pulls/42/reviews?per_page=100": reviews,
    }

    def api(endpoint, *, paginate=False):
        assert paginate
        return contents[endpoint]

    def post(args, *, check):
        assert args[:6] == ["gh", "api", "--method", "POST", issue_endpoint, "--raw-field"]
        assert check
        body = args[6].removeprefix("body=")
        assert body.startswith("@wgsim")
        assert f"https://github.com/{repo}/pull/42#{fragment}" in body
        assert "#issuecomment-20" not in body
        assert "#issuecomment-23" not in body
        assert "#pullrequestreview-22" not in body
        comments.append({"id": 100, "created_at": recent, "body": body, "user": {"login": scope["BOT_LOGIN"]}})

    with patch.dict(scope, api=api), patch.object(scope["subprocess"], "run", side_effect=post) as send:
        notify(repo, 42, cutoff)
        notify(repo, 42, cutoff)
    send.assert_called_once()
