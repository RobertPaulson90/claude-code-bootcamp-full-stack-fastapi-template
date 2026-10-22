# Merge policy

<!-- Read by the merge-gate skill (`/land <pr>`) after docs/agents/project.md.
     The script `.opencode/skills/merge-gate/scripts/land.py` parses the tables
     below, so keep their columns. Patterns are globs on repo-relative paths
     (`*` also matches `/`). Escape a literal pipe inside a cell as `\|`.
     A change to this file is itself a high-risk change: the steward never
     auto-merges edits to the rules it is judged by. -->

The steward collects evidence, a person or this table decides. The model's
only input to the verdict is the list of findings it validated; tiers, required
checks and the merge itself come from here.

| Key | Value | Notes |
| --- | --- | --- |
| `MERGE_METHOD` | `merge` | feature-factory merges every ticket with `--no-ff`; a merge commit keeps those ticket commits. |
| `MAX_FIX_ROUNDS` | `2` | Fix rounds before the steward stops the line and hands the PR to a person. |
| `AUTO_MERGE_TIERS` | `low`, `normal` | Tiers the steward may merge on its own when nothing blocks. `high` always needs a person. |
| `TEST_PATHS` | `backend/tests/*`, `frontend/tests/*`, `*test_*.py`, `*.spec.ts`, `*.test.ts` | What counts as a test when checking whether a change came with one. |

## Risk rules (paths)

First matching row wins for each file. Files no row matches are `normal`.
`generated` files are gated but left out of the risk tier and the review.

| Pattern | Tier | Why |
| --- | --- | --- |
| `.opencode/*` | high | the factory itself: agents, commands, skills |
| `.claude/*` | high | agent configuration |
| `.agents/*` | high | agent configuration |
| `docs/agents/*` | high | factory configuration, including this policy |
| `docs/intent/*` | high | intents and plans are the plan of record |
| `.github/*` | high | CI and repository automation |
| `backend/app/alembic/versions/*` | high | data migration |
| `backend/app/core/security.py` | high | authentication |
| `backend/app/api/deps.py` | high | authentication and access control |
| `backend/app/api/routes/login.py` | high | authentication |
| `uv.lock` | high | dependencies |
| `bun.lock` | high | dependencies |
| `*pyproject.toml` | high | dependencies and tool configuration |
| `*package.json` | high | dependencies |
| `compose*.yml` | high | runtime and deployment |
| `*Dockerfile*` | high | runtime and deployment |
| `frontend/src/client/*` | generated | regenerated API client |
| `frontend/src/routeTree.gen.ts` | generated | regenerated route tree |
| `*.md` | low | documentation |
| `img/*` | low | documentation images |

## Risk rules (content)

A regex matched against the lines a file adds. A match raises that file to the
tier given. Generated files and `TEST_PATHS` are not matched.

| Regex | Tier | Why |
| --- | --- | --- |
| `get_current_active_superuser\|is_superuser` | high | access control |
| `send_email\|smtplib\|emails\.` | high | outbound email |
| `(?i)(secret\|password\|token)\s*=` | high | credential handling |

## Evidence

Run in a worktree at the PR head (`.land/pr-<n>`), in this order. A step runs
when any changed file matches `When`. A step whose `After` step did not pass is
skipped. Every step that runs is required: a failure blocks the merge.
`{SETUP}` and `{VERIFY}` come from docs/agents/project.md; in `{VERIFY}`,
`origin/<base>` is replaced by the PR's merge base. `Needs: postgres` starts a
fresh database on localhost:5432 (Docker when it can pull images, otherwise
`scripts/pg-local.sh`).

| Step | When | After | Needs | Workdir | Command |
| --- | --- | --- | --- | --- | --- |
| `setup` | `*` | — | — | `.` | `mkdir -p backend/app/frontend && {SETUP}` |
| `gate` | `*` | `setup` | — | `.` | `{VERIFY}` |
| `frontend-build` | `frontend/*` | `setup` | — | `frontend` | `bun run build` |
| `backend-tests` | `backend/*` | `setup` | postgres | `backend` | `uv run bash scripts/prestart.sh && uv run bash scripts/tests-start.sh land` |
| `backend-coverage` | `backend/*` | `backend-tests` | — | `backend` | `uv run coverage report --fail-under=90` |

## Reviewers

The reviewer comes from a different model family than the code's author, read
from the commits' `Co-Authored-By` trailers.

| Author family | Reviewer |
| --- | --- |
| claude | `review-sol` |
| gpt | `review-opus` |
| unknown | `review-opus` |

## Verdicts

`land.py decide` applies these in order; the first that holds is the verdict.

| Verdict | When | The steward then |
| --- | --- | --- |
| `NOT-READY` | PR is closed, draft, or has merge conflicts | reports and stops |
| `COLLECT` | a required evidence step or review has not run at this head | runs what is missing |
| `FIX` | a step failed or a validated finding blocks, and fewer than `MAX_FIX_ROUNDS` rounds ran | dispatches the fixer with the brief, then lands again |
| `STOP` | still blocked after `MAX_FIX_ROUNDS` rounds | pulls the cord: hands the PR to a person |
| `SIGN-OFF` | nothing blocks, but the tier is not in `AUTO_MERGE_TIERS` or a finding is for a person | shows the packet and asks a person to approve the merge |
| `AUTO-MERGE` | nothing blocks, the tier is in `AUTO_MERGE_TIERS`, no failing CI | merges through `land.py merge` |
