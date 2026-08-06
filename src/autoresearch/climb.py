"""One live climb, end to end: clone → climb_once → commit/push/PR → report.

This is the glue `orchestrator.climb_once` deliberately does not own: the git
side (bot-auth clone, veto-checked commit, push, PR) and the run's durable
record. One invocation = one run = at most one PR.

Credential separation holds throughout: the bot PAT is read orchestrator-side
and used only by Workspace network calls and the PR client, after the session
has ended; the session sees only its own capped API key inside its container.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from autoresearch.contract import load_contract
from autoresearch.github import (
    FileTokenProvider,
    GitHubClient,
    Workspace,
)
from autoresearch.harness import ClaudeCodeHarness, Harness, redact
from autoresearch.orchestrator import (
    ClimbConfig,
    Evaluator,
    SubprocessEvaluator,
    climb_once,
    out_of_scope,
    pr_body,
)
from autoresearch.runstate import (
    ABORTED,
    ENDED,
    IN_REVIEW,
    NEGATIVE_RESULT,
    RunRecord,
    save_record,
)

log = logging.getLogger(__name__)

RULER = (
    "The metric is computed by the contract's eval command over a frozen "
    "instance pool. Your claim is verified by the orchestrator re-running "
    "that exact command on your tree — and again by CI after the PR opens. "
    "Only changes inside the contract's allowed paths are ever measured."
)

_ENDINGS_BY_OUTCOME = {
    "no-improvement": NEGATIVE_RESULT,
    "session-error": ABORTED,
    "eval-error": ABORTED,
    "scope-violation": ABORTED,
}


@dataclass(frozen=True)
class LiveClimbOutcome:
    run_id: str
    outcome: str
    pr_url: str = ""
    report_path: str = ""


def live_climb(
    config: ClimbConfig,
    run_root: Path,
    run_id: str,
    harness: Harness,
    evaluator: Evaluator,
    github: GitHubClient,
    bot_auth: FileTokenProvider,
    now: float,
    created: str,
    secrets: tuple[str, ...] = (),
    base_branch: str = "main",
) -> LiveClimbOutcome:
    """Run one climb against the real target repo."""
    run_dir = run_root / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    workspace = run_dir / "ws"

    ws = Workspace.clone(f"https://github.com/{config.target}.git", workspace, auth=bot_auth)
    contract_path = workspace / ".autoresearch.yaml"
    contract_text = contract_path.read_text()
    contract = load_contract(contract_text, config.target)

    def changed_paths() -> list[str]:
        ws.git("add", "-A")
        paths = ws.staged_paths()
        ws.git("reset")
        return paths

    record = RunRecord(
        run_id=run_id,
        target=config.target,
        task_title=f"improve {config.benchmark}",
        state="implementing",
        agent_id=config.agent_id,
        deadline=now + 24 * 3600,
    )
    save_record(run_root, record, now)

    result = climb_once(
        config,
        contract_text,
        workspace,
        harness,
        evaluator,
        ruler=RULER,
        changed_paths=changed_paths,
        created=created,
    )

    report = result.report(config, redact_secrets=secrets)
    report_path = run_dir / "report.md"
    report_path.write_text(report)

    pr_url = ""
    if result.outcome == "improved":
        branch = result.branch
        ws.branch(branch)
        # The commit veto re-checks FULL scope (allowed + forbidden) as
        # defense in depth behind climb_once's pre-eval check.
        ws.commit_all(
            f"agent: improve {config.benchmark} ({result.baseline} -> {result.candidate})",
            author=config.agent_id,
            forbidden=lambda p: bool(out_of_scope([p], contract)),
        )
        ws.push(branch)
        pr_url = github.create_pull(
            config.target,
            title=f"[agent] {config.benchmark}: {result.baseline} -> {result.candidate}",
            head=branch,
            base=base_branch,
            body=pr_body(result, config, redact_secrets=secrets),
        )
        final = RunRecord(**{**record.__dict__, "state": IN_REVIEW, "ending_note": pr_url})
    else:
        final = RunRecord(
            **{
                **record.__dict__,
                "state": ENDED,
                "ending": _ENDINGS_BY_OUTCOME[result.outcome],
                "ending_note": redact(result.note, secrets),
            }
        )
    save_record(run_root, final, now)
    log.info("run %s: %s %s", run_id, result.outcome, pr_url)
    return LiveClimbOutcome(
        run_id=run_id,
        outcome=result.outcome,
        pr_url=pr_url,
        report_path=str(report_path),
    )


def main() -> int:
    import argparse
    import os
    import time
    from datetime import UTC, datetime

    parser = argparse.ArgumentParser(description="One live climb on one benchmark.")
    parser.add_argument("--target", required=True)
    parser.add_argument("--benchmark", required=True)
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--image", default="", help="apptainer image for session+eval")
    parser.add_argument("--claude-bin", default=os.path.expanduser("~/.local/bin/claude"))
    parser.add_argument("--model", default="claude-opus-5")
    parser.add_argument("--max-turns", type=int, default=60)
    parser.add_argument("--pat-file", default=os.path.expanduser("~/.config/autoresearch/bot_pat"))
    parser.add_argument(
        "--key-file", default=os.path.expanduser("~/.config/autoresearch/harness_key")
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    api_key = Path(args.key_file).read_text().strip()
    bot_auth = FileTokenProvider(Path(args.pat_file))
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    run_id = f"{args.benchmark}-{stamp}"

    outcome = live_climb(
        config=ClimbConfig(target=args.target, benchmark=args.benchmark),
        run_root=args.run_root,
        run_id=run_id,
        harness=ClaudeCodeHarness(
            api_key=api_key,
            binary=args.claude_bin,
            model=args.model,
            max_turns=args.max_turns,
            container_image=args.image,
        ),
        evaluator=SubprocessEvaluator(container_image=args.image),
        github=GitHubClient(auth=bot_auth),
        bot_auth=bot_auth,
        now=time.time(),
        created=datetime.now(UTC).isoformat(),
        secrets=(api_key,),
    )
    print(f"outcome={outcome.outcome} pr={outcome.pr_url or '-'} report={outcome.report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
