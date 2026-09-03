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
    # blank identities fail closed, as before — a blank CURRENT login disables
    # the aliases too (no identity of our own means no identity at all)
    assert not is_own_login("", "outerloop-autoresearch[bot]")
    assert not is_own_login("someone", "")
    assert not is_own_login("old-bot", "")
    assert not is_own_login("agentic-learning-bot", "   ")


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


def test_the_reusable_workflows_carry_the_aliases() -> None:
    """The reviewer and verifier run in GitHub Actions with only what the
    workflow exports: the alias input must reach the process env everywhere
    REVIEW_BOT_LOGIN does, and the verifier's author gate must accept it."""
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    for name in ("advisory-review-agent.yml", "advisory-review-summarize.yml", "verify-agent.yml"):
        text = (root / name).read_text()
        doc = yaml.safe_load(text)
        inputs = (
            doc[True]["workflow_call"]["inputs"]
            if True in doc
            else doc["on"]["workflow_call"]["inputs"]
        )
        assert "bot_aliases" in inputs and inputs["bot_aliases"]["required"] is False, name
        for job in doc["jobs"].values():
            for step in job.get("steps", []):
                env = step.get("env") or {}
                if "REVIEW_BOT_LOGIN" in env:
                    assert env.get("AUTORESEARCH_BOT_ALIASES") == "${{ inputs.bot_aliases }}", (
                        name,
                        step.get("name"),
                    )
    verify = yaml.safe_load((root / "verify-agent.yml").read_text())
    gate = verify["jobs"]["gate"]
    # one normalizing gate (bash trims each alias), consumed by both jobs
    run = gate["steps"][0]["run"]
    assert "tr -d '[:space:]'" in run and "BOT_ALIASES" in run and "bot_authored=" in run
    assert gate["steps"][0]["env"]["BOT_ALIASES"] == "${{ inputs.bot_aliases }}"
    for job in ("verify", "post"):
        assert "gate" in (
            [verify["jobs"][job]["needs"]]
            if isinstance(verify["jobs"][job]["needs"], str)
            else verify["jobs"][job]["needs"]
        )
        assert "needs.gate.outputs.bot_authored == 'true'" in verify["jobs"][job]["if"]
        assert "inputs.bot_aliases" not in verify["jobs"][job]["if"]
