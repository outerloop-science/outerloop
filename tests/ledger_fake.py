"""In-memory GitHub branch/CAS surface shared by writer integration tests."""

from dataclasses import dataclass, field

from outerloop.github import GitHubError


@dataclass
class LedgerGitHub:
    dry_run: bool = False
    ledger_head: str = "ledger-0"
    ledger_files: dict[str, str] = field(default_factory=dict)
    ledger_snapshots: dict[str, dict[str, str]] = field(default_factory=dict)
    ledger_writes: list[dict[str, str]] = field(default_factory=list)
    ledger_fail: bool = False
    trees: dict[str, str] = field(default_factory=dict)
    ancestry: list[str] = field(default_factory=list)
    pull_requests: dict[int, dict] = field(default_factory=dict)

    def default_branch(self, repo):
        return "main"

    def branch_sha(self, repo, branch):
        return "main-pin"

    def branch_head(self, repo, branch):
        assert branch == "research-log"
        return self.ledger_head

    def create_ref(self, repo, ref, sha):
        assert ref == "refs/heads/research-log" and sha == "main-pin"
        self.ledger_head = sha

    def get_tree(self, repo, sha, recursive=True):
        if self.ledger_fail:
            raise GitHubError(503, "tree", "unavailable")
        if not recursive:
            return {"sha": self.trees[sha]}
        files = self.ledger_snapshots.get(sha, self.ledger_files)
        return {"truncated": False, "tree": [{"path": p} for p in files]}

    def get_file(self, repo, path, ref):
        files = self.ledger_snapshots.get(ref, self.ledger_files)
        if path not in files:
            raise GitHubError(404, "file", "missing")
        return files[path]

    def put_files(self, repo, files, branch, message, *, expected_head):
        assert branch == "research-log"
        if self.ledger_fail or expected_head != self.ledger_head:
            return False
        self.ledger_snapshots[self.ledger_head] = dict(self.ledger_files)
        self.ledger_files.update(files)
        self.ledger_writes.append(files)
        self.ledger_head = f"ledger-{len(self.ledger_writes)}"
        return True

    def head_contains(self, repo, base, head):
        return self.compare(repo, base, head)["status"] in ("ahead", "identical")

    def compare(self, repo, base, head):
        a, b = self.ancestry.index(base), self.ancestry.index(head)
        return {"status": "identical" if a == b else "ahead" if a < b else "behind"}

    def get_pull_request(self, repo, number):
        return self.pull_requests[number]
