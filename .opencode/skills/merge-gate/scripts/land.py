#!/usr/bin/env python3
"""The deterministic half of /land.

Facts, evidence, the verdict and the merge come from here; the model only adds
validated findings. Run from the repository root:

    land.py facts <pr>             PR facts, risk tier, review baseline, worktree at head
    land.py evidence <pr>          SETUP, VERIFY and tests in .land/pr-<n>
    land.py record-findings <pr>   validated findings as JSON on stdin
    land.py decide <pr>            verdict from docs/agents/merge-policy.md
    land.py packet <pr> [--html]   the review packet (markdown, or HTML on stdout)
    land.py brief <pr>             the fixer's prompt for a FIX verdict
    land.py review <pr>            post the packet as one GitHub review, with a marker
    land.py merge <pr>             merge, only when the verdict is AUTO-MERGE at the current head

Everything is written to .land/runs/pr-<n>/<head12>/. Standard library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

POLICY_PATH = "docs/agents/merge-policy.md"
PROJECT_PATH = "docs/agents/project.md"
MARKER_RE = re.compile(
    r"<!-- merge-steward head=([0-9a-f]{40}) verdict=([A-Z-]+) round=(\d+) -->"
)
TIER_ORDER = {"generated": -1, "low": 0, "normal": 1, "high": 2}
SEVERITIES = ("P0", "P1", "P2", "P3")
DISPOSITIONS = ("hand-back", "human", "note")
SCRIPT_DIR = Path(__file__).resolve().parent


class LandError(Exception):
    pass


# --------------------------------------------------------------------------- shell


def run(cmd, cwd=None, check=True, input_text=None, env=None):
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        input=input_text,
        capture_output=True,
        text=True,
        env=env,
        shell=isinstance(cmd, str),
    )
    if check and proc.returncode != 0:
        shown = cmd if isinstance(cmd, str) else " ".join(cmd)
        raise LandError(f"`{shown}` failed ({proc.returncode}): {proc.stderr.strip()[-800:]}")
    return proc


def git(*args, cwd=None, check=True):
    return run(["git", *args], cwd=cwd or ROOT, check=check).stdout.strip()


def gh_json(*args, input_text=None):
    out = run(["gh", *args], cwd=ROOT, input_text=input_text).stdout
    return json.loads(out) if out.strip() else None


def repo_root() -> Path:
    try:
        return Path(run(["git", "rev-parse", "--show-toplevel"]).stdout.strip())
    except LandError:
        sys.exit("land.py: run from inside the repository")


ROOT = repo_root()
LAND = ROOT / ".land"


def repo_slug() -> str:
    if os.environ.get("LAND_REPO"):
        return os.environ["LAND_REPO"]
    url = git("remote", "get-url", "origin")
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", url)
    if not m:
        raise LandError(f"origin is not a GitHub remote: {url}")
    return m.group(1)


# --------------------------------------------------------------------------- policy


def split_row(line: str) -> list[str]:
    cells = re.split(r"(?<!\\)\|", line.strip().strip("|"))
    return [c.strip().replace("\\|", "|") for c in cells]


def parse_tables(text: str) -> dict[str, list[list[str]]]:
    """Map each heading to the body rows of the first table under it."""
    tables: dict[str, list[list[str]]] = {}
    heading = ""
    in_comment = False
    for line in text.splitlines():
        if "<!--" in line:
            in_comment = True
        if in_comment:
            if "-->" in line:
                in_comment = False
            continue
        if line.startswith("#"):
            heading = line.lstrip("#").strip().lower()
            continue
        if line.lstrip().startswith("|"):
            row = split_row(line)
            if all(re.fullmatch(r":?-{3,}:?", c) for c in row if c):
                continue
            tables.setdefault(heading, []).append(row)
    # the first row of each table is its header
    return {h: rows[1:] for h, rows in tables.items() if rows}


def code_spans(cell: str) -> list[str]:
    spans = re.findall(r"`([^`]*)`", cell)
    return spans if spans else ([cell.strip()] if cell.strip() not in ("", "—", "-") else [])


def first_span(cell: str) -> str:
    spans = code_spans(cell)
    return spans[0] if spans else ""


def load_policy() -> dict:
    path = ROOT / POLICY_PATH
    if not path.exists():
        raise LandError(f"{POLICY_PATH} is missing")
    tables = parse_tables(path.read_text())
    keys = {first_span(r[0]): code_spans(r[1]) for r in tables.get("merge policy", [])}
    project_tables = parse_tables((ROOT / PROJECT_PATH).read_text())
    # a value with one code span is a command; anything longer is prose, kept whole
    project = {
        first_span(r[0]): first_span(r[1]) if len(re.findall(r"`[^`]*`", r[1])) == 1 else r[1].replace("`", "")
        for r in project_tables.get("project config", [])
    }
    return {
        "merge_method": (keys.get("MERGE_METHOD") or ["merge"])[0],
        "max_fix_rounds": int((keys.get("MAX_FIX_ROUNDS") or ["2"])[0]),
        "auto_merge_tiers": keys.get("AUTO_MERGE_TIERS") or ["low"],
        "test_paths": keys.get("TEST_PATHS") or [],
        "not_verified": " ".join(keys.get("NOT_VERIFIED") or []) or project.get("UNTESTED", ""),
        "path_rules": [
            {"pattern": first_span(r[0]), "tier": r[1].strip(), "why": r[2].strip()}
            for r in tables.get("risk rules (paths)", [])
        ],
        "content_rules": [
            {"regex": first_span(r[0]), "tier": r[1].strip(), "why": r[2].strip()}
            for r in tables.get("risk rules (content)", [])
        ],
        "evidence": [
            {
                "step": first_span(r[0]),
                "when": code_spans(r[1]),
                "after": first_span(r[2]) or None,
                "needs": r[3].strip() if r[3].strip() not in ("—", "-", "") else None,
                "workdir": first_span(r[4]) or ".",
                "command": first_span(r[5]),
            }
            for r in tables.get("evidence", [])
        ],
        "reviewers": {r[0].strip(): first_span(r[1]) for r in tables.get("reviewers", [])},
        "project": project,
    }


def glob_match(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, p) for p in patterns)


# --------------------------------------------------------------------------- runs


def run_dir(pr: int, head: str) -> Path:
    d = LAND / "runs" / f"pr-{pr}" / head[:12]
    d.mkdir(parents=True, exist_ok=True)
    return d


def current_head(pr: int, repo: str) -> str:
    return gh_json("pr", "view", str(pr), "-R", repo, "--json", "headRefOid")["headRefOid"]


def load_run(pr: int, need=("facts",)) -> tuple[Path, dict]:
    """The run for the PR's current head, with the named files loaded."""
    repo = repo_slug()
    head = current_head(pr, repo)
    d = LAND / "runs" / f"pr-{pr}" / head[:12]
    data = {}
    for name in ("facts", "evidence", "findings", "verdict"):
        f = d / f"{name}.json"
        if f.exists():
            data[name] = json.loads(f.read_text())
        elif name in need:
            step = {"facts": "facts", "evidence": "evidence", "findings": "record-findings",
                    "verdict": "decide"}[name]
            raise LandError(
                f"no {name}.json for PR #{pr} at {head[:12]} (the head may have moved): "
                f"run `land.py {step} {pr}` first"
            )
    return d, data


def write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2) + "\n")


# --------------------------------------------------------------------------- facts


def family_of(trailers: str) -> str:
    t = trailers.lower()
    if "claude" in t or "anthropic" in t:
        return "claude"
    if re.search(r"\bgpt|openai|codex", t):
        return "gpt"
    return "unknown"


def classify(path: str, policy: dict) -> tuple[str, str]:
    for rule in policy["path_rules"]:
        if fnmatch.fnmatchcase(path, rule["pattern"]):
            return rule["tier"], f"{rule['why']} (`{rule['pattern']}`)"
    return "normal", "code"


def review_list_items(body: str) -> list[str]:
    m = re.search(r"^## Review these first\s*$(.*?)(?=^## |\Z)", body or "", re.M | re.S)
    if not m:
        return []
    return [i.strip() for i in re.findall(r"^\d+\.\s+(.*)$", m.group(1), re.M)]


def ensure_worktree(pr: int, head: str) -> Path:
    wt = LAND / f"pr-{pr}"
    exclude = ROOT / ".git" / "info" / "exclude"
    if exclude.exists() and ".land/" not in exclude.read_text():
        with exclude.open("a") as fh:
            fh.write("\n.land/\n")
    if not (wt / ".git").exists():
        git("worktree", "add", "--detach", str(wt), head)
    else:
        git("reset", "--hard", "-q", cwd=wt)
        git("clean", "-fdq", cwd=wt)
        git("checkout", "--detach", "-q", head, cwd=wt)
    return wt


def cmd_facts(args) -> None:
    policy = load_policy()
    repo = repo_slug()
    pr = args.pr
    me = gh_json("api", "user")["login"]
    p = gh_json(
        "pr", "view", str(pr), "-R", repo, "--json",
        "number,title,url,state,isDraft,baseRefName,baseRefOid,headRefName,headRefOid,"
        "mergeable,mergeStateStatus,author,body,statusCheckRollup,createdAt",
    )
    head, base_ref = p["headRefOid"], p["baseRefName"]
    git("fetch", "-q", "origin", f"+refs/pull/{pr}/head:refs/land/pr-{pr}",
        f"+refs/heads/{base_ref}:refs/remotes/origin/{base_ref}")
    if git("rev-parse", f"refs/land/pr-{pr}") != head:
        raise LandError("fetched head does not match the PR head; retry")
    merge_base = git("merge-base", f"origin/{base_ref}", head)

    # files and tiers
    numstat = {}
    for line in git("diff", "--numstat", "--no-renames", merge_base, head).splitlines():
        add, dele, path = line.split("\t", 2)
        numstat[path] = (int(add) if add.isdigit() else 0, int(dele) if dele.isdigit() else 0)
    files = []
    for path, (add, dele) in sorted(numstat.items()):
        tier, why = classify(path, policy)
        reasons = [why] if tier in ("high", "low") else []
        is_test = glob_match(path, policy["test_paths"])
        if tier != "generated" and not is_test:
            added = "\n".join(
                l[1:] for l in git("diff", "-U0", merge_base, head, "--", path).splitlines()
                if l.startswith("+") and not l.startswith("+++")
            )
            for rule in policy["content_rules"]:
                if not re.search(rule["regex"], added):
                    continue
                if TIER_ORDER[rule["tier"]] >= TIER_ORDER[tier]:
                    tier = rule["tier"]
                    reasons.append(f"{rule['why']} (adds a line matching `{rule['regex']}`)")
        files.append({
            "path": path, "additions": add, "deletions": dele, "tier": tier,
            "reasons": reasons, "test": is_test,
        })
    gated = [f for f in files if f["tier"] != "generated"]
    tier = max((f["tier"] for f in gated), key=TIER_ORDER.get, default="low")
    tier_reasons = sorted({
        f"{f['path']}: {r}" for f in gated if f["tier"] == tier for r in f["reasons"]
    }) if tier == "high" else []

    # commits
    team = policy["project"].get("LINEAR_TEAM", "")
    fmt = "%H%x02%P%x02%an%x02%cI%x02%s%x02%(trailers:key=Co-Authored-By,valueonly,separator=; )%x03"
    commits = []
    for rec in git("log", f"--format={fmt}", f"{merge_base}..{head}").split("\x03"):
        rec = rec.strip("\n")
        if not rec:
            continue
        sha, parents, author, date, subject, trailers = rec.split("\x02")
        is_merge = len(parents.split()) > 1
        commits.append({
            "sha": sha, "subject": subject, "author": author, "date": date,
            "merge": is_merge, "co_authors": trailers.strip(),
            "family": None if is_merge else family_of(trailers),
            "convention_ok": is_merge or not team or bool(re.match(rf"^{team}-\d+:", subject)),
        })
    factory_branch = p["headRefName"].startswith("intent/")
    violations = [c for c in commits if factory_branch and not c["convention_ok"]]

    # review baseline: our own last review, else the factory's last merge, else the merge base
    reviews = gh_json("api", f"repos/{repo}/pulls/{pr}/reviews", "--paginate") or []
    markers = []
    for r in reviews:
        if (r.get("user") or {}).get("login") != me:
            continue
        m = MARKER_RE.search(r.get("body") or "")
        if m:
            markers.append({"head": m.group(1), "verdict": m.group(2), "round": int(m.group(3)),
                            "at": r.get("submitted_at"), "url": r.get("html_url")})
    ancestors = [
        mk for mk in markers
        if run(["git", "merge-base", "--is-ancestor", mk["head"], head], cwd=ROOT, check=False).returncode == 0
    ]
    fix_rounds = sum(1 for mk in markers if mk["verdict"] == "FIX")
    baseline, baseline_source, prior = merge_base, "none: no earlier review, the whole PR is unreviewed", None
    if ancestors:
        prior = max(ancestors, key=lambda mk: mk["at"] or "")
        baseline = prior["head"]
        baseline_source = f"steward review at {prior['head'][:12]} ({prior['verdict']}, {prior['at']})"
    elif factory_branch:
        first_parent = git("log", "--first-parent", "--format=%H%x02%s", f"{merge_base}..{head}").splitlines()
        for line in first_parent:  # newest first
            sha, subject = line.split("\x02", 1)
            if re.match(rf"^Merge (review round \d+|{team}-\d+)", subject):
                baseline = sha
                baseline_source = f"feature-factory review, last factory merge {sha[:12]} \"{subject}\""
                break

    after_baseline = set(git("rev-list", f"{baseline}..{head}").splitlines()) if baseline != head else set()
    delta_commits = [c for c in commits if not c["merge"] and c["sha"] in after_baseline]
    delta_files = git("diff", "--name-only", "--no-renames", baseline, head).splitlines() if baseline != head else []
    tiers_by_path = {x["path"]: x["tier"] for x in files}
    delta_code = [f for f in delta_files if tiers_by_path.get(f) != "generated"]

    def reviewer_for(fams: set) -> str:
        r = policy["reviewers"]
        if "claude" in fams:
            return r.get("claude", "review-sol")
        if "gpt" in fams:
            return r.get("gpt", "review-opus")
        return r.get("unknown", "review-opus")

    reviews_required = []
    if baseline != merge_base and delta_commits:
        reviews_required.append({
            "kind": "delta", "base": baseline, "head": head, "paths": delta_code,
            "reviewer": reviewer_for({c["family"] for c in delta_commits}),
            "why": f"commits after the last review ({baseline_source})",
        })
    if not prior and (tier != "low" or baseline == merge_base):
        reviews_required.append({
            "kind": "full", "base": merge_base, "head": head,
            "paths": [f["path"] for f in gated],
            "reviewer": reviewer_for({c["family"] for c in commits if not c["merge"]}),
            "why": "no independent review of this PR yet",
        })

    checks = []
    for c in p.get("statusCheckRollup") or []:
        checks.append({"name": c.get("name") or c.get("context"),
                       "result": (c.get("conclusion") or c.get("state") or c.get("status") or "").upper()})
    if not checks:
        ci = "none"
    elif any(c["result"] in ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED") for c in checks):
        ci = "failing"
    elif all(c["result"] in ("SUCCESS", "NEUTRAL", "SKIPPED") for c in checks):
        ci = "passing"
    else:
        ci = "pending"

    steps = [
        s["step"] for s in policy["evidence"]
        if any(glob_match(f["path"], s["when"]) for f in files)
    ]

    previous_findings = []
    if prior:
        f = LAND / "runs" / f"pr-{pr}" / prior["head"][:12] / "findings.json"
        if f.exists():
            previous_findings = [
                x for x in json.loads(f.read_text()).get("findings", [])
                if x.get("disposition") == "hand-back"
            ]

    wt = ensure_worktree(pr, head)
    facts = {
        "repo": repo, "me": me, "generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "pr": {k: p[k] for k in ("number", "title", "url", "state", "isDraft", "baseRefName",
                                  "headRefName", "mergeable", "mergeStateStatus", "createdAt")},
        "author": (p.get("author") or {}).get("login"),
        "head": head, "merge_base": merge_base, "worktree": str(wt.relative_to(ROOT)),
        "factory_branch": factory_branch,
        "tier": tier, "tier_reasons": tier_reasons,
        "files": files, "commits": commits,
        "convention_violations": [
            {"sha": c["sha"], "subject": c["subject"],
             "rule": f"docs/agents/issue-tracker.md: commits start with `{team}-<n>:`"}
            for c in violations
        ],
        "baseline": baseline, "baseline_source": baseline_source,
        "delta": {
            "commits": [{k: c[k] for k in ("sha", "subject", "author", "date", "co_authors", "family")}
                        for c in delta_commits],
            "files": delta_files,
            "touches_tests": any(glob_match(f, policy["test_paths"]) for f in delta_files),
        },
        "fix_rounds_done": fix_rounds, "steward_markers": markers,
        "previous_findings": previous_findings,
        "reviews_required": reviews_required,
        "evidence_steps": steps,
        "ci": ci, "checks": checks,
        "untested": policy["not_verified"],
        "author_review_list": review_list_items(p.get("body") or ""),
    }
    d = run_dir(pr, head)
    write_json(d / "facts.json", facts)
    print_facts(facts, d)


def print_facts(f: dict, d: Path) -> None:
    p = f["pr"]
    print(f"PR #{p['number']} {p['title']!r}  {p['url']}")
    print(f"head {f['head'][:12]}  base {p['baseRefName']} (merge base {f['merge_base'][:12]})  "
          f"state {p['state']}{' draft' if p['isDraft'] else ''}  mergeable {p['mergeable']}")
    gated = [x for x in f["files"] if x["tier"] != "generated"]
    gen = [x for x in f["files"] if x["tier"] == "generated"]
    print(f"files: {len(gated)} reviewed + {len(gen)} generated, "
          f"+{sum(x['additions'] for x in f['files'])}/-{sum(x['deletions'] for x in f['files'])}")
    print(f"tier: {f['tier']}")
    for r in f["tier_reasons"]:
        print(f"  - {r}")
    print(f"CI: {f['ci']}" + (f" ({len(f['checks'])} checks)" if f["checks"] else " (no checks reported)"))
    print(f"baseline: {f['baseline'][:12]} - {f['baseline_source']}")
    dl = f["delta"]
    if f["baseline"] != f["merge_base"]:
        print(f"since baseline: {len(dl['commits'])} commit(s), {len(dl['files'])} file(s), "
              f"tests touched: {'yes' if dl['touches_tests'] else 'NO'}")
        for c in dl["commits"]:
            print(f"  {c['sha'][:12]} {c['subject']!r}  by {c['co_authors'] or c['author']} [{c['family']}]")
        for path in dl["files"]:
            print(f"    {path}")
    for v in f["convention_violations"]:
        print(f"convention: {v['sha'][:12]} {v['subject']!r} breaks {v['rule']}")
    print(f"fix rounds so far: {f['fix_rounds_done']}")
    if f["previous_findings"]:
        print(f"previous hand-back findings to re-check: {len(f['previous_findings'])}")
        for x in f["previous_findings"]:
            print(f"  - {x['id']} {x['severity']} {x['title']} ({x['path']}:{x.get('line', '?')})")
    print("reviews required:")
    for r in f["reviews_required"]:
        print(f"  - {r['kind']}: {r['base'][:12]}..{r['head'][:12]} with {r['reviewer']} "
              f"({len(r['paths'])} files) - {r['why']}")
    print(f"evidence steps: {', '.join(f['evidence_steps'])}")
    print(f"worktree at head: {f['worktree']}")
    print(f"run dir: {d.relative_to(ROOT)}")


# --------------------------------------------------------------------------- evidence


def docker_usable() -> bool:
    if not shutil.which("docker"):
        return False
    return run(["docker", "image", "ls", "-q"], check=False).returncode == 0


def start_postgres(wt: Path, project: str, log):
    if docker_usable():
        up = run(["docker", "compose", "-p", project, "up", "-d", "--wait", "db", "mailpit"],
                 cwd=wt, check=False)
        log.write(up.stdout + up.stderr)
        if up.returncode == 0:
            return "docker compose (db, mailpit)", lambda: run(
                ["docker", "compose", "-p", project, "down", "-v", "--remove-orphans"], cwd=wt, check=False)
    pg = SCRIPT_DIR / "pg-local.sh"
    out = run(["bash", str(pg), "start"], check=False)
    log.write(out.stdout + out.stderr)
    if out.returncode != 0:
        raise LandError("could not start postgres (Docker unusable and pg-local.sh failed)")
    return "local postgres (pg-local.sh; Docker unusable)", lambda: run(["bash", str(pg), "stop"], check=False)


def cmd_evidence(args) -> None:
    policy = load_policy()
    d, data = load_run(args.pr)
    facts = data["facts"]
    wt = ensure_worktree(args.pr, facts["head"])
    logs = d / "logs"
    logs.mkdir(exist_ok=True)
    project = policy["project"]
    base_ref = facts["pr"]["baseRefName"]
    verify = project.get("VERIFY", "").replace(f"origin/{base_ref}", facts["merge_base"])
    results: dict[str, dict] = {}
    only = set(args.only.split(",")) if args.only else None
    for spec in policy["evidence"]:
        name = spec["step"]
        if name not in facts["evidence_steps"]:
            continue
        if only and name not in only:
            prev = (data.get("evidence") or {}).get("steps", {}).get(name)
            if prev:
                results[name] = prev
            continue
        if spec["after"] and results.get(spec["after"], {}).get("status") != "pass":
            results[name] = {"status": "skipped", "why": f"{spec['after']} did not pass"}
            print(f"[skip] {name}: {spec['after']} did not pass")
            continue
        cmd = spec["command"].replace("{SETUP}", project.get("SETUP", "true")).replace("{VERIFY}", verify)
        workdir = wt / spec["workdir"]
        log_path = logs / f"{name}.log"
        print(f"[run ] {name}: {cmd}  (in {workdir.relative_to(ROOT)})", flush=True)
        started = time.time()
        stop = None
        note = None
        with log_path.open("w") as log:
            try:
                if spec["needs"] == "postgres":
                    note, stop = start_postgres(wt, f"land-pr-{args.pr}", log)
                    log.write(f"\n--- postgres: {note}\n")
                    log.flush()
                # the steward's own virtualenv must not leak into the worktree's uv runs
                env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
                proc = subprocess.run(cmd, shell=True, cwd=workdir, stdout=log, stderr=subprocess.STDOUT,
                                      text=True, env=env, executable="/bin/bash")
                code = proc.returncode
            except LandError as e:
                log.write(f"\n{e}\n")
                code = 125
            finally:
                if stop:
                    stop()
        tail = log_path.read_text(errors="replace").splitlines()[-25:]
        status = "pass" if code == 0 else "fail"
        results[name] = {
            "status": status, "exit": code, "seconds": round(time.time() - started, 1),
            "command": cmd, "workdir": spec["workdir"], "log": str(log_path.relative_to(ROOT)),
            "tail": tail, **({"note": note} if note else {}),
        }
        print(f"[{status}] {name} in {results[name]['seconds']}s (exit {code}, log {results[name]['log']})")
    dirty = git("status", "--porcelain", cwd=wt)
    evidence = {"head": facts["head"], "steps": results,
                "worktree_dirty_after": dirty.splitlines()[:20]}
    write_json(d / "evidence.json", evidence)
    if dirty:
        print("note: the steps modified tracked files in the worktree (a hook auto-fixed something):")
        print("\n".join("  " + l for l in dirty.splitlines()[:20]))
    for name, r in results.items():
        if r["status"] == "fail":
            print(f"\n--- {name} (last lines) ---")
            print("\n".join(r["tail"]))


# --------------------------------------------------------------------------- findings


def cmd_record_findings(args) -> None:
    d, data = load_run(args.pr)
    facts = data["facts"]
    try:
        payload = json.loads(sys.stdin.read())
    except json.JSONDecodeError as e:
        raise LandError(f"findings are not valid JSON: {e}")
    reviews = payload.get("reviews", [])
    findings = payload.get("findings", [])
    errors = []
    for r in reviews:
        for k in ("kind", "reviewer"):
            if k not in r:
                errors.append(f"review missing {k}: {r}")
        r.setdefault("head", facts["head"])
    for i, f in enumerate(findings, 1):
        f.setdefault("id", f"F{i}")
        if f.get("severity") not in SEVERITIES:
            errors.append(f"{f['id']}: severity must be one of {SEVERITIES}")
        if f.get("disposition") not in DISPOSITIONS:
            errors.append(f"{f['id']}: disposition must be one of {DISPOSITIONS}")
        for k in ("title", "path", "body"):
            if not f.get(k):
                errors.append(f"{f['id']}: missing {k}")
        f["blocking"] = f.get("disposition") == "hand-back" and (
            f.get("severity") in ("P0", "P1") or bool(f.get("blocking")))
    if errors:
        raise LandError("findings rejected:\n  " + "\n  ".join(errors))
    required = {r["kind"] for r in facts["reviews_required"]}
    done = {r["kind"] for r in reviews if r.get("head") == facts["head"]}
    write_json(d / "findings.json", {"head": facts["head"], "reviews": reviews, "findings": findings})
    print(f"recorded {len(findings)} finding(s) from {len(reviews)} review(s) at {facts['head'][:12]}")
    if required - done:
        print(f"still missing reviews: {', '.join(sorted(required - done))}")


# --------------------------------------------------------------------------- decide


def decide(facts: dict, evidence: dict | None, findings: dict | None, policy: dict) -> dict:
    head = facts["head"]
    pr = facts["pr"]
    reasons, missing, failed = [], [], []
    v = None
    if pr["state"] != "OPEN":
        v, reasons = "NOT-READY", [f"PR is {pr['state'].lower()}"]
    elif pr["isDraft"]:
        v, reasons = "NOT-READY", ["PR is a draft"]
    elif pr["mergeable"] == "CONFLICTING":
        v, reasons = "NOT-READY", ["PR has merge conflicts with its base"]
    if v:
        return {"verdict": v, "head": head, "tier": facts["tier"], "reasons": reasons}

    steps = (evidence or {}).get("steps", {}) if (evidence or {}).get("head") == head else {}
    for s in facts["evidence_steps"]:
        r = steps.get(s)
        if not r:
            missing.append(f"evidence step `{s}` has not run at {head[:12]}")
        elif r["status"] == "fail":
            failed.append(f"`{s}` failed (exit {r['exit']}): see {r['log']}")
    fx = findings if (findings or {}).get("head") == head else None
    done = {r["kind"] for r in (fx or {}).get("reviews", [])}
    for r in facts["reviews_required"]:
        if r["kind"] not in done:
            missing.append(f"{r['kind']} review by {r['reviewer']} has not run at {head[:12]}")
    items = (fx or {}).get("findings", [])
    blocking = [f for f in items if f.get("blocking")]
    human = [f for f in items if f.get("disposition") == "human"]

    rounds = facts["fix_rounds_done"]
    max_rounds = policy["max_fix_rounds"]
    tier = facts["tier"]
    base = {"head": head, "tier": tier, "tier_reasons": facts["tier_reasons"], "round": rounds,
            "blocking": [f"{f['id']} {f['severity']} {f['title']}" for f in blocking],
            "failed": failed, "missing": missing}
    if missing:
        return {**base, "verdict": "COLLECT", "reasons": missing}
    if failed or blocking or facts["ci"] == "failing":
        why = failed + [f"{f['id']} {f['severity']}: {f['title']} ({f['path']}:{f.get('line', '?')})"
                        for f in blocking]
        if facts["ci"] == "failing":
            why.append("CI checks are failing")
        if rounds >= max_rounds:
            return {**base, "verdict": "STOP",
                    "reasons": [f"still blocked after {rounds} fix round(s) (max {max_rounds})"] + why}
        return {**base, "verdict": "FIX", "reasons": why}
    if facts["ci"] == "pending":
        return {**base, "verdict": "COLLECT", "reasons": ["CI checks are still running"]}
    merge_cmd = (f"gh pr merge {pr['number']} -R {facts['repo']} --{policy['merge_method']} "
                 f"--match-head-commit {head}")
    if tier not in policy["auto_merge_tiers"] or human:
        why = []
        if tier not in policy["auto_merge_tiers"]:
            why.append(f"tier `{tier}` is not auto-mergeable")
            why += facts["tier_reasons"]
        why += [f"{f['id']} for a person: {f['title']}" for f in human]
        return {**base, "verdict": "SIGN-OFF", "reasons": why, "merge_command": merge_cmd}
    return {**base, "verdict": "AUTO-MERGE",
            "reasons": [f"tier `{tier}`, every required step passed, no blocking finding"],
            "merge_command": merge_cmd}


def cmd_decide(args) -> None:
    policy = load_policy()
    d, data = load_run(args.pr)
    verdict = decide(data["facts"], data.get("evidence"), data.get("findings"), policy)
    verdict["decided_at"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    write_json(d / "verdict.json", verdict)
    print(f"VERDICT {verdict['verdict']} at {verdict['head'][:12]}")
    for r in verdict["reasons"]:
        print(f"  - {r}")
    if verdict.get("merge_command"):
        print(f"merge command: {verdict['merge_command']}")


# --------------------------------------------------------------------------- packet


VERDICT_LINE = {
    "AUTO-MERGE": "Nothing blocks and the risk tier allows it: the steward merges.",
    "SIGN-OFF": "Nothing blocks. The risk needs a person: approve the merge prompt to merge, reject it to stop.",
    "FIX": "Blocked. The steward hands the items below to a fixer and lands again.",
    "STOP": "Blocked after the last fix round. The line is stopped: a person takes it from here.",
    "COLLECT": "Evidence is incomplete; no decision yet.",
    "NOT-READY": "The PR can't be landed as it is.",
}


def packet_parts(data: dict) -> dict:
    facts, ev, fx, v = data["facts"], data.get("evidence") or {}, data.get("findings") or {}, data.get("verdict") or {}
    steps = []
    for name in facts["evidence_steps"]:
        r = ev.get("steps", {}).get(name) if ev.get("head") == facts["head"] else None
        if not r:
            steps.append((name, "not run", ""))
        elif r["status"] == "skipped":
            steps.append((name, "skipped", r.get("why", "")))
        else:
            steps.append((name, r["status"], f"{r['seconds']}s" + (f", {r['note']}" if r.get("note") else "")))
    reviews = [(r["kind"], r["reviewer"], r.get("summary", "")) for r in fx.get("reviews", [])]
    return {"facts": facts, "steps": steps, "reviews": reviews, "findings": fx.get("findings", []), "verdict": v}


def packet_md(data: dict) -> str:
    p = packet_parts(data)
    f, v = p["facts"], p["verdict"]
    out = [
        f"## merge-steward: {v.get('verdict', 'NO VERDICT')} at `{f['head'][:12]}`",
        "",
        VERDICT_LINE.get(v.get("verdict"), ""),
        "",
    ]
    if v.get("reasons"):
        out += ["**Why**", ""] + [f"- {r}" for r in v["reasons"]] + [""]
    out += [f"**Risk tier:** `{f['tier']}`", ""]
    out += [f"**Reviewed since:** `{f['baseline'][:12]}`, {f['baseline_source']}", ""]
    if f["baseline"] != f["merge_base"] and f["delta"]["commits"]:
        out += ["**Commits after that review**", ""]
        out += [f"- `{c['sha'][:12]}` {c['subject']} ({c['co_authors'] or c['author']})" for c in f["delta"]["commits"]]
        out += [f"- tests changed with them: {'yes' if f['delta']['touches_tests'] else '**no**'}", ""]
    out += ["**Evidence at this head**", "", "| Step | Result | |", "| --- | --- | --- |"]
    out += [f"| `{n}` | {s} | {d} |" for n, s, d in p["steps"]]
    out += [f"| CI | {f['ci']} | {len(f['checks'])} check(s) |"]
    out += [f"| review `{k}` | {r} | {s} |" for k, r, s in p["reviews"]]
    out += [""]
    by = {"hand-back": [], "human": [], "note": []}
    for x in p["findings"]:
        by[x["disposition"]].append(x)
    titles = {"hand-back": "Back to the fixer", "human": "For you to decide", "note": "Notes (non-blocking)"}
    for k in ("hand-back", "human", "note"):
        if by[k]:
            out += [f"**{titles[k]}**", ""]
            for x in by[k]:
                loc = f"`{x['path']}:{x['line']}`" if x.get("line") else f"`{x['path']}`"
                out += [f"- **{x['id']} {x['severity']}** {x['title']} - {loc}", f"  {x['body']}"]
            out += [""]
    if f["convention_violations"]:
        out += ["**Outside the factory's process**", ""]
        out += [f"- `{c['sha'][:12]}` \"{c['subject']}\" breaks {c['rule']}" for c in f["convention_violations"]]
        out += [""]
    if v.get("verdict") in ("SIGN-OFF", "STOP") and f["author_review_list"]:
        out += ["**The author's own review list (claims, not verified by the steward)**", ""]
        out += [f"{i}. {item[:400]}" for i, item in enumerate(f["author_review_list"], 1)] + [""]
    if f["untested"]:
        out += [f"**Not verified here:** {f['untested']}", ""]
    return "\n".join(out)


def packet_html(data: dict) -> str:
    p = packet_parts(data)
    f, v = p["facts"], p["verdict"]
    e = html.escape
    verdict = v.get("verdict", "NO VERDICT")
    tone = {"AUTO-MERGE": "--success", "SIGN-OFF": "--warning", "FIX": "--destructive",
            "STOP": "--destructive", "COLLECT": "--info", "NOT-READY": "--muted-foreground"}.get(verdict, "--info")

    def badge(s):
        c = {"pass": "--success", "fail": "--destructive", "skipped": "--muted-foreground",
             "not run": "--warning", "none": "--warning", "passing": "--success",
             "failing": "--destructive", "pending": "--info"}.get(s, "--muted-foreground")
        return f'<span class="b" style="--c:var({c})">{e(s)}</span>'

    rows = "".join(f"<tr><td><code>{e(n)}</code></td><td>{badge(s)}</td><td>{e(d)}</td></tr>" for n, s, d in p["steps"])
    rows += f"<tr><td>CI</td><td>{badge(f['ci'])}</td><td>{len(f['checks'])} check(s)</td></tr>"
    rows += "".join(f"<tr><td>review <code>{e(k)}</code></td><td>{badge('done')}</td><td>{e(r)}: {e(s)}</td></tr>"
                    for k, r, s in p["reviews"])
    delta = ""
    if f["baseline"] != f["merge_base"] and f["delta"]["commits"]:
        items = "".join(f"<li><code>{e(c['sha'][:12])}</code> {e(c['subject'])} <span class='m'>{e(c['co_authors'] or c['author'])}</span></li>"
                        for c in f["delta"]["commits"])
        delta = (f"<div class='card'><h3>After the last review</h3><p class='m'>{e(f['baseline_source'])}</p>"
                 f"<ul>{items}</ul><p>Tests changed with them: <b>{'yes' if f['delta']['touches_tests'] else 'no'}</b></p></div>")
    groups = ""
    titles = {"hand-back": ("Back to the fixer", "--destructive"), "human": ("For you to decide", "--warning"),
              "note": ("Notes", "--muted-foreground")}
    for k, (t, c) in titles.items():
        xs = [x for x in p["findings"] if x["disposition"] == k]
        if not xs:
            continue
        lis = "".join(
            f"<li><span class='b' style='--c:var({c})'>{e(x['severity'])}</span> <b>{e(x['title'])}</b>"
            f"<div class='m'><code>{e(x['path'])}{':' + str(x['line']) if x.get('line') else ''}</code></div>"
            f"<div>{e(x['body'])}</div></li>" for x in xs)
        groups += f"<div class='card'><h3>{e(t)}</h3><ul class='f'>{lis}</ul></div>"
    reasons = "".join(f"<li>{e(r)}</li>" for r in v.get("reasons", []))
    untested = f"<p class='m'><b>Not verified here:</b> {e(f['untested'])}</p>" if f["untested"] else ""
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
.w{{font-family:var(--font-sans);display:flex;flex-direction:column;gap:12px}}
.hd{{display:flex;align-items:center;gap:12px;flex-wrap:wrap}}
.v{{font-weight:700;font-size:20px;padding:6px 14px;border-radius:var(--radius);color:var(--background);background:var({tone})}}
.card{{border:1px solid var(--border);border-radius:var(--radius);padding:16px;background:var(--card);color:var(--card-foreground)}}
h3{{margin:0 0 8px;font-size:14px}} p{{margin:4px 0}} ul{{margin:4px 0;padding-left:18px}}
.f{{list-style:none;padding:0;display:flex;flex-direction:column;gap:10px}}
.m{{color:var(--muted-foreground);font-size:12px}} code{{font-family:var(--font-mono);font-size:12px}}
table{{border-collapse:collapse;width:100%;font-size:13px}} td{{padding:4px 8px;border-top:1px solid var(--border)}}
.b{{display:inline-block;font-size:11px;font-weight:600;padding:1px 8px;border-radius:999px;border:1px solid var(--c);color:var(--c)}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px}}
</style></head><body><div class="w">
<div class="hd"><span class="v">{e(verdict)}</span><div><b>PR #{f['pr']['number']}</b> {e(f['pr']['title'])}
<div class="m">head <code>{f['head'][:12]}</code> - tier <b>{e(f['tier'])}</b> - fix rounds {f['fix_rounds_done']}</div></div></div>
<p>{e(VERDICT_LINE.get(verdict, ''))}</p>
<div class="card"><h3>Why</h3><ul>{reasons}</ul></div>
<div class="grid">{delta}<div class="card"><h3>Evidence at this head</h3><table>{rows}</table></div></div>
{groups}{untested}
</div></body></html>"""


def cmd_packet(args) -> None:
    d, data = load_run(args.pr, need=("facts", "verdict"))
    md = packet_md(data)
    (d / "packet.md").write_text(md + "\n")
    h = packet_html(data)
    (d / "packet.html").write_text(h)
    if args.html:
        print(h)
    else:
        print(md)
        print(f"\n(written to {(d / 'packet.md').relative_to(ROOT)} and packet.html)")


# --------------------------------------------------------------------------- brief


def cmd_brief(args) -> None:
    d, data = load_run(args.pr, need=("facts", "verdict"))
    facts, v = data["facts"], data["verdict"]
    if v["verdict"] != "FIX":
        raise LandError(f"verdict is {v['verdict']}, not FIX")
    ev = data.get("evidence") or {}
    fx = data.get("findings") or {}
    policy = load_policy()
    rnd = facts["fix_rounds_done"] + 1
    wt = ROOT / facts["worktree"]
    verify = policy["project"].get("VERIFY", "").replace("origin/" + facts["pr"]["baseRefName"], facts["merge_base"])
    lines = [
        f"Fix PR #{facts['pr']['number']} ({facts['repo']}), round {rnd} of {policy['max_fix_rounds']}.",
        "",
        f"Work in `{wt}`: a git worktree detached at the PR head `{facts['head']}`, with dependencies "
        f"installed. Run every command there. Do not touch `{ROOT}` itself.",
        "",
        "## Fix exactly these",
        "",
    ]
    n = 0
    for f in fx.get("findings", []):
        if f.get("disposition") == "hand-back":
            n += 1
            loc = f"{f['path']}:{f['line']}" if f.get("line") else f["path"]
            lines += [f"{n}. **{f['id']} {f['severity']} {f['title']}** at `{loc}`", f"   {f['body']}", ""]
    for name, r in ev.get("steps", {}).items():
        if r.get("status") == "fail":
            n += 1
            lines += [f"{n}. **Evidence step `{name}` failed** (exit {r['exit']}): `{r['command']}` "
                      f"in `{r['workdir']}`. Full log: `{ROOT / r['log']}`. Last lines:", "", "```"]
            lines += r["tail"][-15:] + ["```", ""]
    lines += [
        "## Rules",
        "",
        "- Each fix comes with a test that fails if the fix is reverted. Run it both ways.",
        "- Change nothing the list above doesn't need: no refactors, no extra validation or options.",
        "- If an item is wrong or needs a product decision, don't guess: leave it and say so.",
        f"- Before committing, run the repo gate: `{verify}`.",
        "- Backend tests need postgres on localhost:5432: "
        f"`bash {SCRIPT_DIR / 'pg-local.sh'} start` (and `stop` after), then in `backend/`: "
        "`uv run bash scripts/prestart.sh && uv run bash scripts/tests-start.sh fix && "
        "uv run coverage report --fail-under=90`.",
        "- One commit per item, subject `fix(land): <what>`, ending with a `Co-Authored-By:` trailer "
        "naming the model you run on.",
        f"- Push with `git push origin HEAD:refs/heads/{facts['pr']['headRefName']}` (a fast-forward; never force).",
        "- Never merge, approve, review or comment on the PR, and never edit its description.",
        "",
        "Reply with the commits you pushed (sha and subject), each item's status (fixed, or left and why), "
        "and the commands you ran with their results.",
    ]
    text = "\n".join(lines)
    (d / f"brief-round-{rnd}.md").write_text(text + "\n")
    print(text)


# --------------------------------------------------------------------------- review


def right_lines(patch: str) -> set[int]:
    lines, new = set(), 0
    for l in (patch or "").splitlines():
        m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", l)
        if m:
            new = int(m.group(1))
            continue
        if l.startswith("-"):
            continue
        if l.startswith("+"):
            lines.add(new)
        new += 1
    return lines


def cmd_review(args) -> None:
    d, data = load_run(args.pr, need=("facts", "verdict"))
    facts, v = data["facts"], data["verdict"]
    repo, pr = facts["repo"], facts["pr"]["number"]
    if current_head(pr, repo) != facts["head"]:
        raise LandError("the PR head moved since these facts; run facts again")
    md = packet_md(data)
    marker = f"<!-- merge-steward head={facts['head']} verdict={v['verdict']} round={facts['fix_rounds_done']} -->"
    files = gh_json("api", f"repos/{repo}/pulls/{pr}/files", "--paginate") or []
    anchors = {f["filename"]: sorted(right_lines(f.get("patch", ""))) for f in files}
    comments = []
    for x in (data.get("findings") or {}).get("findings", []):
        if x["disposition"] == "note" or not anchors.get(x["path"]):
            continue
        want = int(x.get("line") or 0)
        line = min(anchors[x["path"]], key=lambda n: abs(n - want))
        prefix = "" if line == want else f"(anchored to the nearest changed line; the issue is at line {want}) "
        comments.append({"path": x["path"], "line": line, "side": "RIGHT",
                         "body": f"**{x['id']} {x['severity']}** {x['title']}\n\n{prefix}{x['body']}"})
    own = facts["author"] == facts["me"]
    event = "REQUEST_CHANGES" if not own and v["verdict"] in ("FIX", "STOP") else "COMMENT"
    payload = {"commit_id": facts["head"], "event": event,
               "body": md + "\n\n<sub>Posted by merge-steward (`/land`).</sub>\n" + marker,
               "comments": comments}
    res = gh_json("api", "-X", "POST", f"repos/{repo}/pulls/{pr}/reviews", "--input", "-",
                  input_text=json.dumps(payload))
    print(f"posted {event} review {res.get('html_url')} with {len(comments)} inline comment(s)")


# --------------------------------------------------------------------------- merge


def cmd_merge(args) -> None:
    policy = load_policy()
    d, data = load_run(args.pr, need=("facts", "evidence", "findings", "verdict"))
    facts = data["facts"]
    fresh = decide(facts, data.get("evidence"), data.get("findings"), policy)
    if fresh["verdict"] != "AUTO-MERGE":
        hint = (" A person merges this one: run the merge command yourself so they can approve it."
                if fresh["verdict"] == "SIGN-OFF" else "")
        raise LandError(f"refusing: the verdict at {facts['head'][:12]} is {fresh['verdict']}.{hint}")
    if current_head(args.pr, facts["repo"]) != facts["head"]:
        raise LandError("refusing: the PR head moved since the verdict")
    out = run(fresh["merge_command"].split(), cwd=ROOT)
    print(out.stdout.strip() or f"merged PR #{args.pr} at {facts['head'][:12]}")


# --------------------------------------------------------------------------- main


def main() -> None:
    ap = argparse.ArgumentParser(prog="land.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in [("facts", cmd_facts), ("evidence", cmd_evidence), ("record-findings", cmd_record_findings),
                     ("decide", cmd_decide), ("packet", cmd_packet), ("brief", cmd_brief),
                     ("review", cmd_review), ("merge", cmd_merge)]:
        sp = sub.add_parser(name)
        sp.add_argument("pr", type=int)
        sp.set_defaults(fn=fn)
        if name == "packet":
            sp.add_argument("--html", action="store_true", help="print the HTML packet")
        if name == "evidence":
            sp.add_argument("--only", help="comma-separated steps to (re)run; others keep earlier results")
    args = ap.parse_args()
    try:
        args.fn(args)
    except LandError as e:
        sys.exit(f"land.py: {e}")


if __name__ == "__main__":
    main()
