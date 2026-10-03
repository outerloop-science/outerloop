"""Run the pinned Hermes CLI unchanged, retaining its reported session evidence.

This file runs with Hermes's Python, without an outerloop installation.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


def main() -> None:
    repo = sys.argv.pop(1)
    sys.path.insert(0, repo)
    import run_agent
    from agent.legacy_cli import main as cli_main

    original = run_agent.AIAgent.run_conversation
    root_agent: Any = None

    def conversation(agent: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal root_agent
        if root_agent is None:
            root_agent = agent
        result = original(agent, *args, **kwargs)
        if agent is not root_agent:
            return result
        try:
            session_id = os.environ["OUTERLOOP_CAPTURE_ID"]
            counters = {
                "input_tokens": "session_prompt_tokens",
                "output_tokens": "session_completion_tokens",
                "cached_input_tokens": "session_cache_read_tokens",
                "cache_creation_input_tokens": "session_cache_write_tokens",
            }
            usage = {
                key: getattr(agent, attr) for key, attr in counters.items() if hasattr(agent, attr)
            }
            if not getattr(agent, "_last_turn_usage", None):
                usage = {}
            sample = {
                "session_id": session_id,
                "native_session_id": getattr(agent, "session_id", ""),
                "model": agent.model,
                "usage": usage,
                "messages": result.get("messages", []),
                "completed": result.get("completed"),
            }
            path = Path.home() / f"evidence-{session_id}.json"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(sample, ensure_ascii=False))
            print("OUTERLOOP_USAGE " + json.dumps({"usage": usage, "session_id": session_id}))
        except Exception:
            # Telemetry must never affect the author's result.
            pass
        return result

    run_agent.AIAgent.run_conversation = conversation
    raise SystemExit(cli_main(run=run_agent.main))


if __name__ == "__main__":
    main()
