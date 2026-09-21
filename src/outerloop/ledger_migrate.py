"""Import a pinned main ledger into research-log without inventing measurements."""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import asdict, replace

from outerloop.appauth import resolve_bot_auth
from outerloop.cli import env_file_values
from outerloop.github import GitHubClient, GitHubError
from outerloop.ledger_branch import RESEARCH_LOG_BRANCH, LedgerWriteError
from outerloop.progress import (
    LEADER_FILE,
    PROGRESS_FILE,
    LedgerReadError,
    parse_leader,
    render_markdown,
)


def migrate_ledger(
    github: GitHubClient,
    target: str,
    main_sha: str,
    *,
    force: bool = False,
    dry_run: bool = False,
) -> str:
    """Copy the exact ledger at main_sha; that SHA attributes a snapshot only."""
    if not re.fullmatch(r"[0-9a-f]{40}", main_sha):
        raise ValueError("--main-sha must be a full commit SHA")
    if github.branch_sha(target, github.default_branch(target)) != main_sha:
        raise ValueError("--main-sha does not match the target's current default branch")
    source = parse_leader(github.get_file(target, LEADER_FILE, main_sha))
    entries = {
        name: replace(
            entry,
            main_commit=main_sha,
            measured_sha="",
            measurement_signature="",
            reset_commit="",
            ruler="",
        )
        for name, entry in source.items()
    }
    table = render_markdown(entries, target)
    patch = {
        LEADER_FILE: json.dumps(
            {name: asdict(entry) for name, entry in sorted(entries.items())}, indent=2
        )
        + "\n",
        PROGRESS_FILE: table,
    }
    head = github.branch_head(target, RESEARCH_LOG_BRANCH)
    if head is None:
        raise LedgerReadError("ledger branch head unavailable")
    created = False
    for _attempt in range(3):
        if head:
            tree = github.get_tree(target, head)
            if tree.get("truncated") is not False or not isinstance(tree.get("tree"), list):
                raise LedgerReadError("incomplete ledger tree")
            exists = any(item["path"] in patch for item in tree["tree"])
            if exists and not force and not (created and head == main_sha):
                raise ValueError("branch ledger already exists; use --force to replace it")
        if dry_run:
            return table
        if not head:
            try:
                github.create_ref(target, f"refs/heads/{RESEARCH_LOG_BRANCH}", main_sha)
                created = True
            except GitHubError:
                # A concurrent creator must pass the same overwrite check.
                pass
            head = github.branch_head(target, RESEARCH_LOG_BRANCH)
            if not head:
                raise LedgerWriteError("could not create ledger branch")
            continue
        if github.put_files(
            target, patch, RESEARCH_LOG_BRANCH, "Import main benchmark ledger", expected_head=head
        ):
            return table
        new_head = github.branch_head(target, RESEARCH_LOG_BRANCH)
        if not new_head or new_head == head:
            raise LedgerWriteError("ledger migration write failed")
        head = new_head
    raise LedgerWriteError("ledger moved during migration; rerun the command")


def migrate(args: argparse.Namespace) -> int:
    try:
        settings = {**env_file_values(keys=None), **os.environ}
        github = GitHubClient(
            auth=resolve_bot_auth(
                settings.get("OUTERLOOP_PAT_FILE", ""),
                settings.get("OUTERLOOP_GITHUB_APP_FILE", ""),
            )
        )
        table = migrate_ledger(
            github, args.target, args.main_sha, force=args.force, dry_run=args.dry_run
        )
    except Exception:
        # Remote errors may contain credentials.
        print(
            "Ledger migration failed: check authentication, the current main SHA, "
            "and whether a branch ledger already exists (--force replaces it)."
        )
        return 1
    print(table if args.dry_run else f"Imported {args.main_sha} into {args.target}:research-log.")
    return 0
