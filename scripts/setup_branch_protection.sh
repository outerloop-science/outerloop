#!/usr/bin/env bash
# Apply branch protection to main. Requires GitHub Team (or public repo) and admin rights.
#
# Usage:
#   bash scripts/setup_branch_protection.sh [owner/repo] [approvals] [enforce_admins]
#   bash scripts/setup_branch_protection.sh                    # solo phase: 0 approvals, admins enforced
#   bash scripts/setup_branch_protection.sh outerloop-science/outerloop 1
#                                                              # once a second code owner joins
#
# Solo-phase default is approvals=0 (a sole code owner cannot approve their own PRs);
# checks are still required and admins are still enforced. Raise to 1 with a second owner.
#
# Notes:
# - required_linear_history stays FALSE: merge commits only (squash/rebase disabled
#   at the repo level).
# - The checks list must match the job names in .github/workflows/ci.yml.
set -euo pipefail

REPO="${1:-outerloop-science/outerloop}"
APPROVALS="${2:-0}"
ENFORCE_ADMINS="${3:-true}"

if [ "${APPROVALS}" -gt 0 ]; then CODE_OWNER=true; else CODE_OWNER=false; fi

gh api --method PUT -H "Accept: application/vnd.github+json" \
  "repos/${REPO}/branches/main/protection" --input - <<EOF
{
  "required_status_checks": {
    "strict": false,
    "checks": [
      {"context": "ci"}
    ]
  },
  "enforce_admins": ${ENFORCE_ADMINS},
  "required_pull_request_reviews": {
    "required_approving_review_count": ${APPROVALS},
    "require_code_owner_reviews": ${CODE_OWNER},
    "dismiss_stale_reviews": false
  },
  "restrictions": null,
  "required_linear_history": false,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_conversation_resolution": true
}
EOF

echo "Branch protection applied to ${REPO}:main (approvals: ${APPROVALS}, code_owner: ${CODE_OWNER}, enforce_admins: ${ENFORCE_ADMINS})."
