"""Pinned imports preserve numbers, refuse replacement, and never write main."""

import json
from dataclasses import asdict
from typing import cast

import pytest

from ledger_fake import LedgerGitHub
from outerloop.cli import main
from outerloop.github import GitHubClient, GitHubError
from outerloop.ledger_branch import LedgerWriteError
from outerloop.ledger_migrate import migrate_ledger
from outerloop.progress import LEADER_FILE, LeaderEntry, LedgerReadError, parse_leader

PIN = "a" * 40
ENTRY = LeaderEntry("bench", "loss", "min", 3.123456789, 2.123456789, "old", "2026-09-20", 731)
SOURCE = json.dumps({"bench": asdict(ENTRY)})


class MigrationGitHub(LedgerGitHub):
    def branch_sha(self, repo, branch):
        return PIN

    def create_ref(self, repo, ref, sha):
        assert ref == "refs/heads/research-log" and sha == PIN
        self.ledger_head = sha
        self.ledger_files = dict(self.ledger_snapshots[sha])


def fake(*, absent=False):
    return MigrationGitHub(
        ledger_head="" if absent else "ledger-0",
        ledger_files={"reports/keep.md": "keep"},
        ledger_snapshots={PIN: {LEADER_FILE: SOURCE, "reports/keep.md": "keep"}},
    )


def migrate(gh, **kwargs):
    return migrate_ledger(cast(GitHubClient, gh), "org/repo", PIN, **kwargs)


@pytest.mark.parametrize("absent", [False, True])
def test_import_and_refuse_repeat(absent):
    gh = fake(absent=absent)
    table = migrate(gh)
    entry = parse_leader(gh.ledger_files[LEADER_FILE])["bench"]
    assert (entry.baseline, entry.best, entry.run_seed) == (ENTRY.baseline, ENTRY.best, 731)
    assert entry.main_commit == PIN
    assert not entry.measured_sha
    assert "imported; provenance unknown" in table
    assert gh.ledger_files["reports/keep.md"] == "keep"
    assert gh.ledger_snapshots[PIN][LEADER_FILE] == SOURCE
    with pytest.raises(ValueError, match="already exists"):
        migrate(gh)
    assert len(gh.ledger_writes) == 1
    assert migrate(gh, force=True) == table


@pytest.mark.parametrize("absent", [False, True])
def test_dry_run_never_creates_or_writes(absent):
    gh = fake(absent=absent)
    head = gh.ledger_head
    assert "| bench |" in migrate(gh, dry_run=True)
    assert gh.ledger_head == head and not gh.ledger_writes


def test_refuses_invalid_source_or_pin():
    gh = fake()
    with pytest.raises(ValueError, match="current default"):
        migrate_ledger(cast(GitHubClient, gh), "org/repo", "b" * 40)
    gh.ledger_snapshots[PIN][LEADER_FILE] = "{broken"
    with pytest.raises(LedgerReadError):
        migrate(gh)
    assert not gh.ledger_writes


def test_force_can_replace_corrupt_destination():
    gh = fake()
    gh.ledger_files[LEADER_FILE] = "{broken"
    with pytest.raises(ValueError, match="already exists"):
        migrate(gh)
    migrate(gh, force=True)
    assert parse_leader(gh.ledger_files[LEADER_FILE])["bench"].best == ENTRY.best


def test_concurrent_ledger_creation_is_not_overwritten():
    class RacingGitHub(MigrationGitHub):
        def put_files(self, *args, **kwargs):
            self.ledger_head = "raced"
            self.ledger_files[LEADER_FILE] = "{}"
            return False

    gh = RacingGitHub(ledger_snapshots={PIN: {LEADER_FILE: SOURCE}})
    with pytest.raises(ValueError, match="already exists"):
        migrate(gh)
    assert gh.ledger_files[LEADER_FILE] == "{}"


def test_failed_write_keeps_destination():
    class FailingGitHub(MigrationGitHub):
        def put_files(self, *args, **kwargs):
            return False

    gh = FailingGitHub(ledger_snapshots={PIN: {LEADER_FILE: SOURCE}})
    with pytest.raises(LedgerWriteError):
        migrate(gh)
    assert not gh.ledger_writes


def test_cli_dispatch_and_redaction(monkeypatch, capsys):
    gh = fake()
    monkeypatch.setattr("outerloop.ledger_migrate.env_file_values", lambda **kw: {})
    monkeypatch.setattr("outerloop.ledger_migrate.resolve_bot_auth", lambda *a: None)
    monkeypatch.setattr("outerloop.ledger_migrate.GitHubClient", lambda **kw: gh)
    argv = ["migrate-ledger", "--target", "org/repo", "--main-sha", PIN, "--dry-run"]
    assert main(argv) == 0
    assert "provenance unknown" in capsys.readouterr().out

    def fail(*args):
        raise GitHubError(500, "secret-token", "secret-token")

    monkeypatch.setattr(gh, "get_file", fail)
    assert main(argv) == 1
    assert "secret-token" not in capsys.readouterr().out


@pytest.mark.parametrize("error", [ValueError, LedgerReadError, LedgerWriteError])
def test_cli_reports_local_errors(monkeypatch, capsys, error):
    monkeypatch.setattr("outerloop.ledger_migrate.env_file_values", lambda **kw: {})
    monkeypatch.setattr("outerloop.ledger_migrate.resolve_bot_auth", lambda *a: None)
    monkeypatch.setattr("outerloop.ledger_migrate.GitHubClient", lambda **kw: fake())

    def fail(*args, **kwargs):
        raise error("local migration failure")

    monkeypatch.setattr("outerloop.ledger_migrate.migrate_ledger", fail)
    assert main(["migrate-ledger", "--target", "org/repo", "--main-sha", PIN]) == 1
    assert capsys.readouterr().out == "local migration failure\n"
