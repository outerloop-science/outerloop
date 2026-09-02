"""The kernel's identity is a SET across a flip: the login it posts as plus its
former logins. Recognition stays keyed on login, never on public markers."""

from __future__ import annotations

from autoresearch.github import bot_aliases_from_env, is_own_login


def test_aliases_parse_and_own_login_matches_any_identity(monkeypatch) -> None:
    monkeypatch.delenv("AUTORESEARCH_BOT_ALIASES", raising=False)
    assert bot_aliases_from_env() == ()
    assert is_own_login("outerloop-autoresearch[bot]", "outerloop-autoresearch[bot]")
    assert not is_own_login("agentic-learning-bot", "outerloop-autoresearch[bot]")
    monkeypatch.setenv(
        "AUTORESEARCH_BOT_ALIASES", " agentic-learning-bot, old-bot ,agentic-learning-bot"
    )
    assert bot_aliases_from_env() == ("agentic-learning-bot", "old-bot")
    assert is_own_login("Agentic-Learning-Bot", "outerloop-autoresearch[bot]")
    assert is_own_login("old-bot", "outerloop-autoresearch[bot]")
    assert not is_own_login("renmengye", "outerloop-autoresearch[bot]")
    # blank identities fail closed, as before
    assert not is_own_login("", "outerloop-autoresearch[bot]")
    assert not is_own_login("someone", "")


def test_the_kernels_old_research_log_issue_is_never_an_order(monkeypatch) -> None:
    """Live 2026-09-02: after the App flip the intake lane claimed issue #1
    (the research log, authored by the PAT account) as a research request."""
    from autoresearch.intake import qualifying_issue

    issue = {
        "number": 1,
        "title": "Research log",
        "body": "<!-- autoresearch:research-log -->\none comment per finished run",
        "user": {"login": "agentic-learning-bot"},
        "author_association": "MEMBER",
    }
    monkeypatch.delenv("AUTORESEARCH_BOT_ALIASES", raising=False)
    assert qualifying_issue(issue, "outerloop-autoresearch[bot]")  # the hole
    monkeypatch.setenv("AUTORESEARCH_BOT_ALIASES", "agentic-learning-bot")
    assert not qualifying_issue(issue, "outerloop-autoresearch[bot]")
    # a maintainer's issue still qualifies
    theirs = {**issue, "user": {"login": "renmengye"}, "body": "try a wider model"}
    assert qualifying_issue(theirs, "outerloop-autoresearch[bot]")


def test_old_claims_alarms_and_comments_are_still_ours(monkeypatch) -> None:
    from autoresearch.followup import qualifying_comments
    from autoresearch.tick import CONTRACT_ALARM_MARKER, _find_alarm_issue

    monkeypatch.setenv("AUTORESEARCH_BOT_ALIASES", "agentic-learning-bot")
    bot = "outerloop-autoresearch[bot]"
    # a comment the old account posted is the kernel's, not a maintainer's
    old = {
        "id": 5,
        "body": "**negative-result** ...",
        "user": {"login": "agentic-learning-bot"},
        "author_association": "MEMBER",
    }
    theirs = {
        "id": 6,
        "body": "please retry",
        "user": {"login": "renmengye"},
        "author_association": "MEMBER",
    }
    assert [c[0] for c in qualifying_comments([old, theirs], bot, 0)] == [6]

    class G:
        def list_open_issues(self, target, max_pages=10):
            return [
                {
                    "number": 7,
                    "body": f"{CONTRACT_ALARM_MARKER}\nbroken",
                    "user": {"login": "agentic-learning-bot"},
                },
                {
                    "number": 8,
                    "body": f"{CONTRACT_ALARM_MARKER}\nforged",
                    "user": {"login": "stranger"},
                },
            ]

    assert _find_alarm_issue(G(), "org/repo", bot) == 7
    monkeypatch.delenv("AUTORESEARCH_BOT_ALIASES")
    assert _find_alarm_issue(G(), "org/repo", bot) == 0  # without the alias: not ours
