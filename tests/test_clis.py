"""Tests for the two CLIs: contract validation and the review entry point."""

from __future__ import annotations

from pathlib import Path

from autoresearch.contract_cli import main as contract_main
from autoresearch.llm import CompleterError
from autoresearch.review_cli import main as review_main

GOOD = """
benchmarks:
  - name: demo
    command: run me
    metric: success_rate
    direction: max
budgets: {gpu_hours_per_run: 8, runs_per_week: 10}
scope: {allowed: [src/]}
roadmap: docs/roadmap.md
"""


def test_contract_cli_accepts_valid(tmp_path: Path, capsys) -> None:
    path = tmp_path / ".autoresearch.yaml"
    path.write_text(GOOD)
    assert contract_main([str(path), "org/repo"]) == 0
    out = capsys.readouterr().out
    assert "is valid" in out
    assert "src/" in out
    assert ".github" in out  # shows the always-forbidden set


def test_contract_cli_reports_the_error(tmp_path: Path, capsys) -> None:
    path = tmp_path / ".autoresearch.yaml"
    path.write_text(GOOD.replace("allowed: [src/]", "allowed: ['.github/workflows']"))
    assert contract_main([str(path), "org/repo"]) == 1
    assert "overlaps forbidden" in capsys.readouterr().out


def test_contract_cli_missing_file(tmp_path: Path, capsys) -> None:
    assert contract_main([str(tmp_path / "nope.yaml")]) == 2
    assert "no contract" in capsys.readouterr().out


def test_review_cli_fails_closed_without_bot_login(monkeypatch, caplog) -> None:
    """Without a bot login the reviewer cannot honor 'never review bot PRs'."""
    monkeypatch.setenv("PR_REPO", "org/repo")
    monkeypatch.setenv("PR_NUMBER", "1")
    monkeypatch.delenv("REVIEW_BOT_LOGIN", raising=False)
    assert review_main() == 0
    assert "REVIEW_BOT_LOGIN is unset" in caplog.text


def test_review_cli_never_fails_the_build_on_api_errors(monkeypatch, caplog) -> None:
    monkeypatch.setenv("PR_REPO", "org/repo")
    monkeypatch.setenv("PR_NUMBER", "1")
    monkeypatch.setenv("REVIEW_BOT_LOGIN", "some-bot")
    monkeypatch.setenv("ANTHROPIC_REVIEWER_KEY", "test-key")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)  # -> ValueError from the provider
    assert review_main() == 0
    assert "did not complete" in caplog.text


def test_review_cli_skips_without_api_key(monkeypatch, caplog) -> None:
    """An unset Actions secret arrives as an empty string; skip, don't crash."""
    monkeypatch.setenv("PR_REPO", "org/repo")
    monkeypatch.setenv("PR_NUMBER", "1")
    monkeypatch.setenv("REVIEW_BOT_LOGIN", "some-bot")
    monkeypatch.setenv("ANTHROPIC_REVIEWER_KEY", "")
    assert review_main() == 0
    assert "ANTHROPIC_REVIEWER_KEY is unset" in caplog.text


def test_completer_failures_are_expected_failures() -> None:
    """Operational LLM errors must never propagate out of the advisory CLI."""
    from autoresearch.llm import CompleterError, TruncatedError
    from autoresearch.review_cli import EXPECTED_FAILURES

    assert CompleterError in EXPECTED_FAILURES
    assert TruncatedError in EXPECTED_FAILURES


class FakeReviewClient:
    """Stands in for GitHubClient in review_cli tests; records content fetches."""

    posted: list

    def __init__(self, auth: object = None, author: str = "human-dev") -> None:
        self.posted = []
        self.author = author
        self.content_fetches: list[str] = []

    def get_pull_request(self, repo: str, number: int) -> dict:
        return {
            "title": "t",
            "body": "b",
            "user": {"login": self.author},
            "labels": [],
            "head": {"sha": "abc123"},
        }

    def get_pull_request_diff(self, repo: str, number: int) -> str:
        return "--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,3 @@\n line1\n+line2\n line3\n"

    def get_pull_request_files(self, repo: str, number: int, max_pages: int = 5) -> list[dict]:
        return [{"filename": "x.py", "status": "modified"}]

    def get_file_content(self, repo: str, path: str, ref: str) -> str:
        self.content_fetches.append(path)
        return "def f(): pass"

    def list_comments(self, repo: str, number: int, max_pages: int = 20) -> list[dict]:
        return [c for c in self.posted if c.get("kind") != "review"]

    def list_pr_reviews(self, repo: str, number: int, max_pages: int = 10) -> list[dict]:
        return [c for c in self.posted if c.get("kind") == "review"]

    def comment(self, repo: str, number: int, body: str) -> None:
        # deliberately type User: round counting must be identity-agnostic
        # (self-hosters post reviews with machine-user PATs)
        self.posted.append({"body": body, "user": {"type": "User"}})

    def create_pr_review(self, repo: str, number: int, body: str, comments=None) -> None:
        self.posted.append(
            {
                "body": body,
                "user": {"type": "User"},
                "kind": "review",
                "inline": list(comments or []),
            }
        )


def _cli_env(monkeypatch) -> None:
    monkeypatch.setenv("PR_REPO", "org/repo")
    monkeypatch.setenv("PR_NUMBER", "1")
    monkeypatch.setenv("REVIEW_BOT_LOGIN", "some-bot")
    monkeypatch.setenv("ANTHROPIC_REVIEWER_KEY", "k")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    # ambient env must not flip the explicit-request path under a test
    monkeypatch.delenv("REVIEW_EXPLICIT_REQUEST", raising=False)


def test_review_cli_posts_a_skip_stub_when_the_model_api_refuses(monkeypatch) -> None:
    """A missing round must not be invisible: when the completer dies to an
    API refusal (credits, auth), the thread gets a stub — under its OWN
    marker, so it never counts as a round and never rides as context."""
    import autoresearch.review_cli as cli

    fake_client = FakeReviewClient()

    def fake_review(pr, completer, bot_login, today=None, explicit_request=False):
        raise CompleterError("BadRequestError: credit balance is too low")

    monkeypatch.setattr(cli, "GitHubClient", lambda auth: fake_client)
    monkeypatch.setattr(cli, "review", fake_review)
    monkeypatch.setattr(cli, "AnthropicCompleter", lambda **kw: object())
    _cli_env(monkeypatch)
    assert cli.main() == 0  # still never fails the target's CI
    (stub,) = [c["body"] for c in fake_client.posted]
    assert stub.lstrip().startswith(cli.SKIP_MARKER)
    assert "could not run" in stub and "credit balance" in stub
    from autoresearch.review import MARKER
    from autoresearch.verifier import VERIFY_MARKER

    assert MARKER not in stub and VERIFY_MARKER not in stub


def test_skip_stub_posting_failure_is_swallowed(monkeypatch, caplog) -> None:
    import autoresearch.review_cli as cli
    from autoresearch.github import GitHubError

    class RefusingClient(FakeReviewClient):
        def comment(self, repo, number, body):
            raise GitHubError(403, "/repos/org/repo", "forbidden")

    cli.post_skip_stub(
        RefusingClient(),  # type: ignore[arg-type]
        "org/repo",
        1,
        "advisory review",
        CompleterError("x"),
    )
    assert "could not post the skip stub" in caplog.text


def test_human_pr_review_is_inline_and_comment_event_only(monkeypatch) -> None:
    """Human PRs get the Reviews-API round (body summary + anchored inline
    findings); bot PRs under an explicit label stay issue comments so the
    round can ride into follow-up wakes as context."""
    import autoresearch.review_cli as cli
    from autoresearch.review import Finding, ReviewResult

    fake_client = FakeReviewClient()

    def fake_review(pr, completer, bot_login, today=None, explicit_request=False):
        return ReviewResult(
            [Finding(file="x.py", line=2, confidence="high", summary="Bug", detail="Real.")],
            notes="",
        )

    monkeypatch.setattr(cli, "GitHubClient", lambda auth: fake_client)
    monkeypatch.setattr(cli, "review", fake_review)
    monkeypatch.setattr(cli, "AnthropicCompleter", lambda **kw: object())
    _cli_env(monkeypatch)
    assert cli.main() == 0
    (posted,) = fake_client.posted
    assert posted["kind"] == "review"  # Reviews API, event hard-coded COMMENT
    assert posted["inline"] and posted["inline"][0]["path"] == "x.py"
    assert "Round 1" in posted["body"]


def test_review_fallback_carries_the_full_findings(monkeypatch) -> None:
    """If the Reviews API rejects the post, the fallback comment must carry
    the findings themselves — not a body that says they are attached to
    lines they never reached (round-1 finding)."""
    import autoresearch.review_cli as cli
    from autoresearch.github import GitHubError
    from autoresearch.review import Finding, ReviewResult

    class RefusingReviews(FakeReviewClient):
        def create_pr_review(self, repo, number, body, comments=None):
            raise GitHubError(422, "/reviews", "line outside diff")

    fake_client = RefusingReviews()

    def fake_review(pr, completer, bot_login, today=None, explicit_request=False):
        return ReviewResult(
            [Finding(file="x.py", line=2, confidence="high", summary="Bug", detail="Real.")],
            notes="",
        )

    monkeypatch.setattr(cli, "GitHubClient", lambda auth: fake_client)
    monkeypatch.setattr(cli, "review", fake_review)
    monkeypatch.setattr(cli, "AnthropicCompleter", lambda **kw: object())
    _cli_env(monkeypatch)
    assert cli.main() == 0
    (posted,) = fake_client.posted
    assert posted.get("kind") != "review"  # plain comment fallback
    assert "Bug" in posted["body"] and "`x.py`:2" in posted["body"]
    assert "attached inline" not in posted["body"]


def test_bot_pr_explicit_round_stays_an_issue_comment(monkeypatch) -> None:
    import autoresearch.review_cli as cli
    from autoresearch.review import Finding, ReviewResult

    fake_client = FakeReviewClient(author="some-bot")  # PR author == bot login

    def fake_review(pr, completer, bot_login, today=None, explicit_request=False):
        return ReviewResult(
            [Finding(file="x.py", line=2, confidence="high", summary="Bug", detail="Real.")],
            notes="",
        )

    monkeypatch.setattr(cli, "GitHubClient", lambda auth: fake_client)
    monkeypatch.setattr(cli, "review", fake_review)
    monkeypatch.setattr(cli, "AnthropicCompleter", lambda **kw: object())
    _cli_env(monkeypatch)
    monkeypatch.setenv("REVIEW_EXPLICIT_REQUEST", "true")
    assert cli.main() == 0
    (posted,) = fake_client.posted
    assert posted.get("kind") != "review"  # plain comment: wake context reads these


def test_round_numbering_spans_comments_and_reviews(monkeypatch) -> None:
    """Switching posting styles must not reset the round counter."""
    import autoresearch.review_cli as cli
    from autoresearch.review import MARKER

    fake_client = FakeReviewClient()
    fake_client.posted.append(
        {"body": f"{MARKER}\nold round", "user": {"type": "User"}}  # issue-comment round
    )
    fake_client.posted.append(
        {"body": f"{MARKER}\nolder", "user": {"type": "User"}, "kind": "review", "inline": []}
    )
    _stamp, label = cli._round_stamp(
        fake_client,  # type: ignore[arg-type]
        "org/repo",
        1,
        MARKER,
        {"head": {"sha": "abc"}},
    )
    assert label == "**Round 3**"


def test_review_cli_threads_date_and_context(monkeypatch) -> None:
    """main() must pass today= (the live false-positive fix) and the fetched
    file context through to review()."""
    import autoresearch.review_cli as cli

    captured: dict = {}
    fake_client = FakeReviewClient()

    def fake_review(pr, completer, bot_login, today=None, explicit_request=False):
        captured["today"] = today
        captured["context"] = tuple(pr.context_files)
        from autoresearch.review import ReviewResult

        return ReviewResult(findings=[], notes="")

    monkeypatch.setattr(cli, "GitHubClient", lambda auth: fake_client)
    monkeypatch.setattr(cli, "review", fake_review)
    monkeypatch.setattr(cli, "AnthropicCompleter", lambda **kw: object())
    _cli_env(monkeypatch)
    assert cli.main() == 0
    assert captured["today"] is not None and len(captured["today"]) == 10
    assert captured["context"] == (("x.py", "def f(): pass"),)


def test_review_cli_skips_context_fetch_for_bot_prs(monkeypatch) -> None:
    """Bot-authored PRs must not pay the contents-API fan-out."""
    import autoresearch.review_cli as cli

    fake_client = FakeReviewClient(author="Some-Bot")
    monkeypatch.setattr(cli, "GitHubClient", lambda auth: fake_client)
    monkeypatch.setattr(cli, "AnthropicCompleter", lambda **kw: object())
    _cli_env(monkeypatch)
    assert cli.main() == 0
    assert fake_client.content_fetches == []


def test_context_fetch_stops_at_file_cap(monkeypatch) -> None:
    """One content fetch per picked file — never one past the cap."""
    import autoresearch.review_cli as cli
    from autoresearch.review import MAX_CONTEXT_FILES

    class WideClient(FakeReviewClient):
        def get_pull_request_files(self, repo: str, number: int, max_pages: int = 5) -> list[dict]:
            return [{"filename": f"f{i}.py", "status": "modified"} for i in range(100)]

    fake_client = WideClient()
    monkeypatch.setattr(cli, "GitHubClient", lambda auth: fake_client)
    monkeypatch.setattr(cli, "AnthropicCompleter", lambda **kw: object())
    monkeypatch.setattr(
        cli,
        "review",
        lambda pr, c, b, today=None, explicit_request=False: __import__(
            "autoresearch.review", fromlist=["ReviewResult"]
        ).ReviewResult(findings=[], notes=""),
    )
    _cli_env(monkeypatch)
    assert cli.main() == 0
    assert len(fake_client.content_fetches) == MAX_CONTEXT_FILES


def test_fork_fallback_when_head_repo_lacks_full_name(monkeypatch) -> None:
    """A deleted fork yields head.repo without full_name; str(None) must not
    become the literal repo \"None\" (the live reviewer's catch)."""
    import autoresearch.review_cli as cli

    class DeletedFork(FakeReviewClient):
        def __init__(self) -> None:
            super().__init__()
            self.repos_fetched: list[str] = []

        def get_pull_request(self, repo: str, number: int) -> dict:
            data = super().get_pull_request(repo, number)
            data["head"]["repo"] = {"id": 1, "full_name": None}
            return data

        def get_file_content(self, repo: str, path: str, ref: str) -> str:
            self.repos_fetched.append(repo)
            return "x"

    fake_client = DeletedFork()
    monkeypatch.setattr(cli, "GitHubClient", lambda auth: fake_client)
    monkeypatch.setattr(cli, "AnthropicCompleter", lambda **kw: object())
    monkeypatch.setattr(
        cli,
        "review",
        lambda pr, c, b, today=None, explicit_request=False: __import__(
            "autoresearch.review", fromlist=["ReviewResult"]
        ).ReviewResult(findings=[], notes=""),
    )
    _cli_env(monkeypatch)
    assert cli.main() == 0
    assert fake_client.repos_fetched and set(fake_client.repos_fetched) == {"org/repo"}


def test_explicit_request_env_reaches_review_for_bot_prs(monkeypatch) -> None:
    """REVIEW_EXPLICIT_REQUEST=true must flow through main(): the bot PR
    pays the context fan-out and review() receives explicit_request=True —
    a misspelled env key or dropped argument leaves this red."""
    import autoresearch.review_cli as cli
    from autoresearch.review import ReviewResult

    fake_client = FakeReviewClient(author="Some-Bot")
    seen: dict = {}

    def fake_review(pr, completer, bot_login, today=None, explicit_request=False):
        seen["explicit"] = explicit_request
        return ReviewResult(findings=[], notes="")

    monkeypatch.setattr(cli, "GitHubClient", lambda auth: fake_client)
    monkeypatch.setattr(cli, "AnthropicCompleter", lambda **kw: object())
    monkeypatch.setattr(cli, "review", fake_review)
    _cli_env(monkeypatch)
    monkeypatch.setenv("REVIEW_EXPLICIT_REQUEST", "true")
    assert cli.main() == 0
    assert seen["explicit"] is True
    assert fake_client.content_fetches != []  # explicitly-requested: fan-out paid


def test_each_round_posts_a_new_numbered_comment(monkeypatch) -> None:
    """Rounds are first-class: every run posts a NEW comment (edits fire no
    notifications), numbered by counting prior marker comments, stamped with
    the reviewed head."""
    import autoresearch.review_cli as cli
    from autoresearch.review import ReviewResult

    fake_client = FakeReviewClient()
    monkeypatch.setattr(cli, "GitHubClient", lambda auth: fake_client)
    monkeypatch.setattr(cli, "AnthropicCompleter", lambda **kw: object())
    monkeypatch.setattr(
        cli,
        "review",
        lambda pr, c, b, today=None, explicit_request=False: ReviewResult(
            findings=[], notes="looked fine"
        ),
    )
    _cli_env(monkeypatch)
    assert cli.main() == 0
    assert cli.main() == 0
    bodies = [c["body"] for c in fake_client.posted]
    assert len(bodies) == 2  # two comments, never an edit
    assert "**Round 1**" in bodies[0] and "**Round 2**" in bodies[1]
    # the stamp carries the EXACT reviewed head from the PR payload
    assert "reviewed head `abc123`" in bodies[0]
    # same head twice -> the second round says so
    assert "(re-run on the same head)" in bodies[1]


def test_quote_replies_do_not_inflate_round_count(monkeypatch) -> None:
    """A human quoting the advisory comment copies the marker, but quoted
    lines are prefixed — counting is TEXTUAL (marker at line start) and
    deliberately identity-agnostic, so self-hosters posting with
    machine-user PATs number correctly. A verbatim unquoted paste would
    inflate the cosmetic number; accepted."""
    import autoresearch.review_cli as cli
    from autoresearch.review import MARKER, ReviewResult

    fake_client = FakeReviewClient()
    # a real quote-reply: every quoted line is prefixed, marker not at start
    fake_client.posted.append(
        {"body": f"> {MARKER}\n> old finding\n\nmy reply", "user": {"type": "User"}}
    )
    monkeypatch.setattr(cli, "GitHubClient", lambda auth: fake_client)
    monkeypatch.setattr(cli, "AnthropicCompleter", lambda **kw: object())
    monkeypatch.setattr(
        cli,
        "review",
        lambda pr, c, b, today=None, explicit_request=False: ReviewResult(findings=[], notes="n"),
    )
    _cli_env(monkeypatch)
    assert cli.main() == 0
    assert "**Round 1**" in fake_client.posted[-1]["body"]


def test_round_count_failure_never_costs_the_round(monkeypatch) -> None:
    """Numbering is cosmetic; a listing failure must not suppress the post."""
    import autoresearch.review_cli as cli
    from autoresearch.github import GitHubError
    from autoresearch.review import ReviewResult

    class ListlessClient(FakeReviewClient):
        def list_comments(self, repo, number, max_pages=20):
            raise GitHubError(500, "/comments", "transient")

    fake_client = ListlessClient()
    monkeypatch.setattr(cli, "GitHubClient", lambda auth: fake_client)
    monkeypatch.setattr(cli, "AnthropicCompleter", lambda **kw: object())
    monkeypatch.setattr(
        cli,
        "review",
        lambda pr, c, b, today=None, explicit_request=False: ReviewResult(findings=[], notes="n"),
    )
    _cli_env(monkeypatch)
    assert cli.main() == 0
    assert len(fake_client.posted) == 1
    assert "New round" in fake_client.posted[0]["body"]
