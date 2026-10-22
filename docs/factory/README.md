# The factory's last station: `/land`

[feature-factory](https://github.com/GrandjeanIo/agent-lib/tree/main/plugins/feature-factory)
takes an intent to a PR and stops there: "a person merges each PR". `/land`
is the station after it. It decides whether a PR merges on its own, goes back
for fixing, or needs a person, and it does the work in between.

```
 PEOPLE              AGENTS (feature-factory)                       THIS
 intent -> plan -> tickets -> build x3 -> review + fix -> PR  ==>  /land  ==> merged
                                                                    |  ^
                                                                    v  |
                                                              fixer session
```

## How it decides

The model doesn't decide. It supplies the one thing a script can't: findings it
has checked against the code. Everything else is deterministic.

```
            PR #n @ head sha
                  |
   +--------------v---------------+
   | facts        land.py facts   |  tier from path and content rules, review
   |                              |  baseline, commits after it, tests touched?,
   |                              |  commit convention, CI, required reviews
   +--------------+---------------+
                  |
   +--------------v---------------+   +-------------------------------+
   | evidence  land.py evidence   |   | review (background subagent)  |
   | setup, prek gate, frontend   |   | other model family than the   |
   | build, pytest, coverage      |   | author: delta since baseline, |
   | (Postgres: Docker or local)  |   | and full when unreviewed      |
   +--------------+---------------+   +---------------+---------------+
                  |                                   |
                  |      steward validates findings   |
                  |      land.py record-findings  <---+
                  v
   +------------------------------+
   | verdict      land.py decide  |  docs/agents/merge-policy.md
   +--+---------+---------+-------+
      |         |         |
 AUTO-MERGE    FIX     SIGN-OFF / STOP
 land.py     fixer      packet -> person approves the
 merge       session    `gh pr merge` prompt (that IS the sign-off)
             pushes ->
             /land again (max 2 rounds)
```

What keeps it honest:

- **Tier is computed, not judged.** Migrations, auth, access control, outbound
  email, dependencies, CI and the factory's own files are `high`, and `high`
  never auto-merges. Saying "no findings" can't lower the tier.
- **Review the delta since the last trusted review.** On a factory PR, that's
  feature-factory's last merge. Commits pushed after it are what nobody saw.
- **Cross-family review.** Claude-written commits get `review-sol`, GPT-written
  commits get `review-opus`, decided from the `Co-Authored-By` trailers.
- **The judge can't build.** `merge-steward` has no edit permission and can't
  push. `merge-fixer` can edit and push, and can't merge, review or comment.
- **Approval is pinned to a sha.** Merges use `--match-head-commit`, and
  evidence and findings recorded at an older head are ignored.
- **PR text is a claim.** Descriptions and commit messages are checked, never
  obeyed.

## Files

| Path | What |
| --- | --- |
| `.opencode/commands/land.md` | `/land <pr>`, runs as `merge-steward` |
| `.opencode/agents/merge-steward.md` | the judge: read-only, `gh pr merge` asks a person |
| `.opencode/agents/merge-fixer.md` | the fixer: test-first, fast-forward pushes only |
| `.opencode/skills/merge-gate/SKILL.md` | the procedure |
| `.opencode/skills/merge-gate/scripts/land.py` | facts, evidence, verdict, packet, brief, review, merge |
| `.opencode/skills/merge-gate/scripts/pg-local.sh` | throwaway Postgres when Docker can't pull images |
| `docs/agents/merge-policy.md` | tiers, evidence steps, reviewers, verdict rules |
| `scripts/demo/land-demo.sh` | stage fresh demo PRs |
| `.land/` (git-ignored) | worktrees at PR heads, run state, logs, packets |

## Run it

In OpenChamber (or the opencode TUI), in this checkout:

```
/land <pr>
```

Each step works on its own too, which helps when debugging:

```
L=.opencode/skills/merge-gate/scripts/land.py
python3 $L facts 12 && python3 $L evidence 12 && python3 $L decide 12
```

## Demo runbook (30 minutes)

### Before

1. `gh auth status` shows the fork's owner, and `git remote -v` has `origin` =
   the fork, `upstream` = GrandjeanIo's repo.
2. Stage the two PRs and note their numbers:
   `scripts/demo/land-demo.sh onboarding live` and
   `scripts/demo/land-demo.sh docs live`.
3. Warm the caches: `python3 $L facts <onboarding-pr> && python3 $L evidence <onboarding-pr>`.
   This takes about 90 seconds and leaves no trace on GitHub.
4. Optionally sign in to Docker Desktop. Without it, `pg-local.sh` runs the
   backend tests, and the packet says which one was used.
5. Rehearse with throwaway PRs (`land-demo.sh onboarding r2`), never with the
   live ones. Close them with `gh pr close <n> --delete-branch`.

### Run of show

| Time | Show | Say |
| --- | --- | --- |
| 0-3 | the diagram above | feature-factory stops at "a person merges". Merging is a risk decision, so the station after build is a judge. |
| 3-6 | the onboarding PR on GitHub | 1,875 lines, a "Review these first" list, the gate passed, CI silent. Would you merge it? |
| 6-9 | `/land <pr>`, facts output | 1 commit after the factory's review, 1 line, no tests touched, and it breaks the repo's commit convention. Tier `high`, and why. |
| 9-14 | reviewers in the background, evidence in the foreground | the GPT reviewer finds the completion rate shown 10x too low (`*100)/10`), on the KPI the intent exists to move. The coverage gate fails at 86% (master: 92%). Neither shows up in "gate passed". |
| 14-16 | the FIX packet, the GitHub review | the verdict comes from a table, not the model. The packet is the GitHub review, with inline comments. |
| 16-22 | the fixer session in the sidebar | its own session, in the steward's worktree, test-first. It pushes, and the steward lands again from the new baseline. |
| 22-25 | the SIGN-OFF packet, then the merge prompt | nothing blocks, but a migration, a superuser endpoint and email mean a person decides. Approving the prompt *is* the sign-off. |
| 25-28 | `/land <docs-pr>` | the low-risk lane: gate plus review, AUTO-MERGE, no person. |
| 28-30 | next steps | a scheduled steward over every PR labelled `factory`, sign-off pings to a phone, a check after merge against the intent's own metric. |

### If something goes wrong live

- A reviewer is slow: let evidence finish and talk through the facts output.
  It already shows the unreviewed commit.
- The fixer takes long: show the rehearsal PR's packets and GitHub reviews
  instead. They're the same flow at the same heads.
- The merge prompt doesn't appear (permissions auto-approved globally): the
  steward still ran the command with `--match-head-commit`. Say so, and show
  the merged PR.
