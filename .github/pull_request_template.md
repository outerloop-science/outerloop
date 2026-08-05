## Summary

<!-- What does this PR do, and why? -->

## Test plan

<!-- How was this verified? -->

## Checklist

- [ ] No secrets: bot PATs and LLM API keys are env vars on the orchestrator host only
- [ ] No agent transcripts or run artifacts (they contain target-repo code)
- [ ] No binaries or files >500 KB
- [ ] `uv.lock` regenerated if dependencies changed
- [ ] CHANGELOG.md updated under [Unreleased] for user-visible changes
