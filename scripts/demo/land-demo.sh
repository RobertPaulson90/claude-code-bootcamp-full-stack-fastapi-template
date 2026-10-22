#!/usr/bin/env bash
# Stage fresh PRs for the /land demo in this fork.
#
#   scripts/demo/land-demo.sh onboarding [suffix]   PR from the factory's intent branch, as it was
#                                                   handed over (head 928b9d8, the post-review commit)
#   scripts/demo/land-demo.sh docs [suffix]         a small docs-only PR for the auto-merge lane
#
# Each run pushes a new branch and opens a new PR, so a rehearsal never spends
# the PR you demo with. Close rehearsal PRs with `gh pr close <n> --delete-branch`.
set -euo pipefail

REPO=$(git remote get-url origin | sed -E 's#.*github\.com[:/]([^/]+/[^/.]+)(\.git)?$#\1#')
UPSTREAM=GrandjeanIo/claude-code-bootcamp-full-stack-fastapi-template
SOURCE_HEAD=928b9d89e0877151c262c726ce0923d991eebd0c
kind=${1:-}
suffix=${2:-$(date +%H%M%S)}

case "$kind" in
  onboarding)
    branch="intent/001-new-user-onboarding-$suffix"
    git fetch -q upstream "$SOURCE_HEAD" 2>/dev/null || git fetch -q origin intent/001-new-user-onboarding
    git push -q origin "$SOURCE_HEAD:refs/heads/$branch"
    body=$(mktemp)
    gh pr view 3 -R "$UPSTREAM" --json body --jq .body > "$body"
    printf '\n\n---\n<sub>Staged for the /land demo from %s#3 (head %s).</sub>\n' "$UPSTREAM" "${SOURCE_HEAD:0:7}" >> "$body"
    gh pr create -R "$REPO" --base master --head "$branch" --title "New-user onboarding" --body-file "$body"
    rm -f "$body"
    ;;
  docs)
    branch="docs/dev-prerequisites-$suffix"
    wt=$(mktemp -d)
    git fetch -q origin master
    git worktree add -q --detach "$wt" origin/master
    (
      cd "$wt"
      python3 - <<'EOF'
from pathlib import Path
p = Path("development.md")
text = p.read_text()
note = (
    "## Prerequisites\n\n"
    "- [uv](https://docs.astral.sh/uv/) for the backend and the repo's hooks.\n"
    "- [Bun](https://bun.sh/) for the frontend, the generated client and the email templates.\n"
    "- Docker for PostgreSQL and Mailpit. Without it, the backend tests need another PostgreSQL on `localhost:5432`.\n\n"
)
if "## Prerequisites" not in text:
    first = text.index("\n## ") + 1
    p.write_text(text[:first] + note + text[first:])
EOF
      git add development.md
      git commit -q -m "docs: list development prerequisites"
      git push -q origin "HEAD:refs/heads/$branch"
    )
    git worktree remove --force "$wt"
    gh pr create -R "$REPO" --base master --head "$branch" --title "docs: list development prerequisites" \
      --body "Adds a short prerequisites section to development.md: uv, Bun and Docker, and what the backend tests need without Docker."
    ;;
  *)
    echo "usage: $0 onboarding|docs [suffix]" >&2
    exit 2
    ;;
esac
