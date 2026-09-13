from dataclasses import replace
from typing import cast

import pytest

from outerloop import cli, inbox, init
from outerloop.appauth import AppInstallationTokenProvider
from outerloop.appmanifest import DEFAULT_PERMISSIONS
from outerloop.github import GitHubClient, GitHubError
from outerloop.runstate import PARKED, RunRecord


@pytest.fixture
def app():
    permissions = dict(DEFAULT_PERMISSIONS)
    del permissions["checks"]
    del permissions["actions"]
    bodies = {
        "/app": {
            "slug": "my-app",
            "owner": {"login": "maker", "type": "Organization"},
            "permissions": dict(permissions),
        },
        "/repos/org/repo/installation": {
            "id": 42,
            "account": {"login": "adopter", "type": "Organization"},
            "permissions": permissions,
        },
    }
    calls = []

    def transport(req):
        assert req.get_method() == "GET"
        assert req.get_header("Authorization").startswith("Bearer ")
        path = req.full_url.removeprefix("https://api.github.com")
        calls.append(path)
        body = bodies[path]
        if isinstance(body, Exception):
            raise body
        return body

    provider = AppInstallationTokenProvider(1, 42, lambda data: b"sig", transport=transport)
    return provider, bodies, calls


@pytest.mark.parametrize("owner_type", ["Organization", "User"])
@pytest.mark.parametrize("account_type", ["Organization", "User"])
def test_permission_pages(app, owner_type, account_type):
    provider, bodies, calls = app
    bodies["/app"]["owner"]["type"] = owner_type
    bodies["/repos/org/repo/installation"]["account"]["type"] = account_type
    gaps = init.app_permission_gaps(provider, "org/repo")
    edit_prefix = "/organizations/maker" if owner_type == "Organization" else ""
    accept_prefix = "/organizations/adopter" if account_type == "Organization" else ""
    assert gaps.edit_url == f"https://github.com{edit_prefix}/settings/apps/my-app/permissions"
    assert gaps.accept_url == f"https://github.com{accept_prefix}/settings/installations/42"
    assert gaps.missing == ("actions", "checks")
    assert "actions: read" in gaps.problem and "checks: read" in gaps.problem
    assert gaps.problem.index(gaps.edit_url) < gaps.problem.index(gaps.accept_url)
    assert calls == ["/repos/org/repo/installation", "/app"]


@pytest.mark.parametrize("level", ["read", "write"])
def test_complete_permissions(app, level):
    provider, bodies, _ = app
    permissions = bodies["/repos/org/repo/installation"]["permissions"]
    permissions.update(checks=level, actions=level)
    gaps = init.app_permission_gaps(provider, "org/repo")
    assert gaps.missing == () and gaps.problem == ""


def test_failed_app_lookup_keeps_permission_names(app):
    provider, bodies, _ = app
    bodies["/app"] = ValueError("offline")
    gaps = init.app_permission_gaps(provider, "org/repo")
    assert gaps.missing == ("actions", "checks")
    assert "actions: read" in gaps.problem and "checks: read" in gaps.problem
    assert gaps.edit_url == gaps.accept_url == ""
    assert "https://" not in gaps.problem


def test_access_uses_one_installation_lookup(app):
    provider, _, calls = app
    assert "checks: read" in init.app_permission_gaps(provider, "org/repo").problem
    assert calls.count("/repos/org/repo/installation") == 1


@pytest.mark.parametrize("fail", [False, True])
def test_upgrade_guidance_is_best_effort(app, monkeypatch, capsys, fail):
    """The check runs as a fresh `outerloop permissions` process (the upgrade
    itself still runs the pre-upgrade code); its exit code decides whether
    the next command is named, and a failed check never fails the upgrade."""
    provider, _, _ = app
    gaps = init.app_permission_gaps(provider, "org/repo")
    ran: list[list[str]] = []

    def run(cmd, check=False):
        ran.append(list(cmd))
        if cmd[-1] == "permissions":
            print("could not check the App permissions on GitHub." if fail else gaps.problem)
            return type("Proc", (), {"returncode": 1})()
        return type("Proc", (), {"returncode": 0})()

    monkeypatch.setattr(cli.subprocess, "run", run)
    versions = iter(["old", "new"])
    monkeypatch.setattr(cli, "_installed_version", lambda *a: next(versions))
    assert cli.main(["upgrade"]) == 0
    captured = capsys.readouterr()
    assert ran[-1][-3:] == ["-m", "outerloop", "permissions"]
    if not fail:
        assert gaps.problem in captured.out
        assert captured.out.index(gaps.problem) < captured.out.index("Restart")
    else:
        assert "could not check" in captured.out
    assert captured.out.rstrip().endswith("outerloop permissions --open")


@pytest.mark.parametrize("lookup_fails", [False, True])
def test_sweep_warning_caches_guidance(app, tmp_path, monkeypatch, caplog, lookup_fails):
    provider, _, _ = app
    gaps = init.app_permission_gaps(provider, "org/repo")
    if lookup_fails:
        gaps = replace(gaps, edit_url="", accept_url="", problem="lookup failed")
    calls = []

    def check(*args):
        calls.append(args)
        return gaps

    monkeypatch.setattr(init, "app_permission_gaps", check)
    monkeypatch.setattr(inbox, "_APP_PERMISSION_WARNINGS", {})

    class GitHub:
        auth = provider

        def list_comments(self, *args):
            return []

        list_pr_reviews = list_comments
        list_pr_review_comments = list_comments

        def list_check_runs(self, *args):
            raise GitHubError(403, "/checks", "forbidden")

    record = RunRecord(
        "run", "org/repo", "task", PARKED, pr_url="https://github.com/org/repo/pull/9"
    )
    for now in (1, 2):
        inbox.gather_github_messages(
            tmp_path, record, cast(GitHubClient, GitHub()), "bot", now, {"head": {"sha": "abc"}}
        )
    assert len(calls) == 1
    if lookup_fails:
        assert "App needs checks: read permission" in caplog.text
    else:
        assert gaps.problem in caplog.text


@pytest.fixture
def app_env(app, monkeypatch):
    provider, _, _ = app
    for key in cli.START_KEYS + cli.TICK_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        cli,
        "env_file_values",
        lambda *a: {"OUTERLOOP_GITHUB_APP_FILE": "/app.json", "OUTERLOOP_TARGET": "org/repo"},
    )
    monkeypatch.setattr("outerloop.appauth.app_provider_from_file", lambda path: provider)
    return app


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("open_page", [False, True])
def test_permissions_status_and_next_page(app_env, monkeypatch, capsys, configured, open_page):
    provider, bodies, _ = app_env
    if configured:
        bodies["/app"]["permissions"] = dict(DEFAULT_PERMISSIONS)
    gaps = init.app_permission_gaps(provider, "org/repo")
    assert gaps.configured_missing == (() if configured else ("actions", "checks"))
    opened = []

    def open_url(url):
        opened.append(url)
        return True

    monkeypatch.setattr(cli.webbrowser, "open", open_url)
    assert cli.main(["permissions", *(["--open"] if open_page else [])]) == 1
    out = capsys.readouterr().out
    lines = out.splitlines()
    columns = []
    for name, level in DEFAULT_PERMISSIONS.items():
        line = next(line for line in lines if line.startswith(f"{name}:"))
        status = "missing" if name in ("actions", "checks") else "ok"
        assert line.split() == [f"{name}:", level, status]
        columns.append(line.index(status))
    assert len(set(columns)) == 1
    next_page = gaps.accept_url if configured else gaps.edit_url
    if open_page:
        assert opened == [next_page]
        assert next_page in out
        assert (gaps.edit_url if configured else gaps.accept_url) not in out
    else:
        assert opened == []
        assert out.index(gaps.edit_url) < out.index(gaps.accept_url)


def test_permissions_complete_does_not_open(app_env, monkeypatch, capsys):
    _, bodies, _ = app_env
    bodies["/repos/org/repo/installation"]["permissions"] = dict(DEFAULT_PERMISSIONS)
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: pytest.fail("unexpected browser"))
    assert cli.main(["permissions", "--open"]) == 0
    out = capsys.readouterr().out
    assert "missing" not in out
    assert out.count("  ok") == len(DEFAULT_PERMISSIONS)


@pytest.mark.parametrize("pat", [False, True])
def test_permissions_requires_app_or_skips_pat(monkeypatch, capsys, pat):
    for key in cli.APP_PERMISSION_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        cli, "env_file_values", lambda *a: {"OUTERLOOP_PAT_FILE": "/pat"} if pat else {}
    )
    assert cli.main(["permissions"]) == (0 if pat else 1)
    assert (
        "A PAT needs no App permissions" if pat else "OUTERLOOP_GITHUB_APP_FILE"
    ) in capsys.readouterr().out


@pytest.mark.parametrize("failure", ["lookup", "file", "env", "browser"])
def test_permissions_failures_never_raise(app_env, monkeypatch, capsys, failure):
    _, bodies, _ = app_env
    opened = []

    def fail(*args):
        raise ValueError("offline")

    monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.append(url))
    if failure == "lookup":
        bodies["/app"] = ValueError("offline")
    elif failure == "file":
        monkeypatch.setattr("outerloop.appauth.app_provider_from_file", fail)
    elif failure == "env":
        monkeypatch.setattr(cli, "env_file_values", fail)
    else:
        monkeypatch.setattr(cli.webbrowser, "open", fail)
    assert cli.main(["permissions", "--open"]) == 1
    assert "could not check" in capsys.readouterr().out
    assert opened == []


@pytest.mark.parametrize("failure", [False, True])
def test_start_permission_warning_continues(app_env, tmp_path, monkeypatch, capsys, failure):
    provider, _, calls = app_env
    problem = init.app_permission_gaps(provider, "org/repo").problem
    calls.clear()
    if failure:

        def fail(*args):
            raise ValueError("offline")

        monkeypatch.setattr(init, "app_permission_gaps", fail)
    monkeypatch.setattr(cli, "find_uv", lambda: ("/bin/uv", ""))
    launched = []

    def launch(cmd, env):
        launched.append(cmd)
        return 0

    monkeypatch.setattr(cli, "_exec", launch)
    assert cli.main(["start", "--local", "--root", str(tmp_path)]) == 0
    assert len(launched) == 1
    assert ("could not check" if failure else problem) in capsys.readouterr().err
    if not failure:
        assert calls == ["/repos/org/repo/installation", "/app"]


def test_sweep_success_invalidates_warning(app_env, tmp_path, monkeypatch):
    provider, _, calls = app_env
    monkeypatch.setattr(inbox, "_APP_PERMISSION_WARNINGS", {})

    class GitHub:
        auth = provider
        fails = True

        def list_comments(self, *args):
            return []

        list_pr_reviews = list_comments
        list_pr_review_comments = list_comments

        def list_check_runs(self, *args):
            if self.fails:
                raise GitHubError(403, "/checks", "forbidden")
            return []

    github = GitHub()
    record = RunRecord(
        "run", "org/repo", "task", PARKED, pr_url="https://github.com/org/repo/pull/9"
    )
    for fails in (True, False, True):
        github.fails = fails
        inbox.gather_github_messages(
            tmp_path, record, cast(GitHubClient, github), "bot", 1, {"head": {"sha": "abc"}}
        )
        assert (record.target in inbox._APP_PERMISSION_WARNINGS) == fails
    assert calls.count("/app") == 2


def test_configured_write_satisfies_required_read(app):
    provider, bodies, _ = app
    bodies["/app"]["permissions"] = {name: "write" for name in DEFAULT_PERMISSIONS}
    gaps = init.app_permission_gaps(provider, "org/repo")
    assert gaps.configured_missing == ()
    assert "already configured" in gaps.problem


def test_permissions_process_env_overrides_file(app_env, monkeypatch, capsys):
    monkeypatch.setenv("OUTERLOOP_GITHUB_APP_FILE", "")
    monkeypatch.setenv("OUTERLOOP_PAT_FILE", "/pat")
    assert cli.main(["permissions", "--open"]) == 0
    assert "A PAT needs no App permissions" in capsys.readouterr().out
    assert app_env[2] == []
