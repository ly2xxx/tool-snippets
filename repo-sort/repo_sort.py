#!/usr/bin/env python3
"""repo-sort: tag GitHub repositories by category and archive the dead ones, from a plan file.

  python repo_sort.py snapshot OWNER [-o repos.json]
      Save OWNER's repositories (topics, archived state, description) as JSON
      for the Repo Sort page to import.

  python repo_sort.py apply repo-plan.txt [--yes] [--only REPO ...]
      Compare the plan with GitHub and print what would change.
      Nothing changes until you add --yes.

Needs the GitHub CLI (https://cli.github.com), signed in with `gh auth login`.
Standard library only.

PLAN FORMAT (the Repo Sort page writes this for you)

  categories: portfolio homelab learning      topics this tool manages
  retire: old-name                            former topics to strip everywhere
  ly2xxx/aidev    portfolio   keep            repository, topic, action
  ly2xxx/kata     learning    archive
  ly2xxx/gco      -           keep            "-" = no category topic

Actions: keep (leave archived state alone), archive, unarchive.
A repository ends up with exactly one managed topic: the one on its line.
Managed topics it no longer belongs to are removed; topics this tool does
not manage are never touched. Re-running is safe: repositories already in
the planned state are skipped.
"""
import argparse
import datetime
import json
import re
import shutil
import subprocess
import sys
import time

TOPIC_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,49}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
ACTIONS = ("keep", "archive", "unarchive")
STATE_FIELDS = "nameWithOwner,isArchived,repositoryTopics"
SNAPSHOT_FIELDS = "nameWithOwner,visibility,isFork,isArchived,pushedAt,repositoryTopics,description"


class PlanError(Exception):
    """The plan file has problems. Raised before anything touches GitHub."""


class GhError(Exception):
    """A gh command failed."""


# ---------------------------------------------------------------- plan file

class Entry:
    def __init__(self, repo, topic, action, line):
        self.repo, self.topic, self.action, self.line = repo, topic, action, line

    @property
    def key(self):
        return self.repo.lower()


class Plan:
    def __init__(self, categories, retired, entries, warnings):
        self.categories, self.retired = categories, retired
        self.entries, self.warnings = entries, warnings

    @property
    def managed(self):
        """Every topic this plan is allowed to add or remove."""
        return set(self.categories) | set(self.retired) | {e.topic for e in self.entries if e.topic}


def _words(value):
    return [w for w in re.split(r"[\s,]+", value.strip()) if w]


def parse_plan(text):
    """Parse plan text. Collects every problem before raising, so one run shows them all."""
    categories, retired, entries, errors, warnings = [], [], [], [], []
    seen = {}
    for num, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        directive = re.match(r"^(categories|retire)\s*:(.*)$", line, re.IGNORECASE)
        if directive:
            target = categories if directive.group(1).lower() == "categories" else retired
            for topic in _words(directive.group(2)):
                if TOPIC_RE.match(topic):
                    target.append(topic)
                else:
                    errors.append(f"line {num}: '{topic}' is not a valid GitHub topic "
                                  "(lowercase letters, digits and hyphens, up to 50 characters)")
            continue
        fields = line.split()
        if len(fields) not in (2, 3):
            errors.append(f"line {num}: expected 'owner/repo topic [action]', got '{line}'")
            continue
        repo, topic = fields[0], fields[1]
        action = fields[2].lower() if len(fields) == 3 else "keep"
        if not REPO_RE.match(repo):
            errors.append(f"line {num}: '{repo}' is not an owner/repo name")
        if topic != "-" and not TOPIC_RE.match(topic):
            errors.append(f"line {num}: '{topic}' is not a valid GitHub topic "
                          "(lowercase letters, digits and hyphens, up to 50 characters)")
        if action not in ACTIONS:
            errors.append(f"line {num}: action must be keep, archive or unarchive, not '{fields[2]}'")
        if repo.lower() in seen:
            errors.append(f"line {num}: {repo} is already planned on line {seen[repo.lower()]}")
        seen[repo.lower()] = num
        entries.append(Entry(repo, None if topic == "-" else topic, action, num))

    for topic in sorted(set(categories) & set(retired)):
        warnings.append(f"'{topic}' is both a category and retired; it will be kept as a category")
    retired = [t for t in retired if t not in categories]
    for e in entries:
        if e.topic and e.topic not in categories:
            warnings.append(f"line {e.line}: '{e.topic}' is not on the categories line; managing it anyway")
    if not entries and not errors:
        errors.append("the plan has no repository lines")
    if errors:
        raise PlanError("\n".join(errors))
    return Plan(categories, retired, entries, warnings)


# ---------------------------------------------------------------- gh wrapper

def run_gh(args):
    """Run gh and return stdout. Kept in one place so tests can swap it out."""
    try:
        out = subprocess.run(["gh", *args], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=120)
    except FileNotFoundError:
        raise GhError("the GitHub CLI 'gh' is not installed. Get it from https://cli.github.com "
                      "and run 'gh auth login'.")
    except subprocess.TimeoutExpired:
        raise GhError(f"'gh {' '.join(args[:3])}' timed out")
    if out.returncode != 0:
        raise GhError((out.stderr or out.stdout).strip() or f"gh exited with code {out.returncode}")
    return out.stdout


def preflight():
    if not shutil.which("gh"):
        raise GhError("the GitHub CLI 'gh' is not installed. Get it from https://cli.github.com "
                      "and run 'gh auth login'.")
    try:
        run_gh(["auth", "status"])
    except GhError:
        raise GhError("gh is not signed in. Run 'gh auth login' first.")


def topic_names(value):
    """gh has reported topics in a few shapes over the years; accept all of them."""
    if not value:
        return []
    if isinstance(value, dict):
        value = value.get("nodes") or []
    names = []
    for t in value:
        if isinstance(t, str):
            names.append(t)
        elif isinstance(t, dict):
            name = t.get("name") or (t.get("topic") or {}).get("name")
            if name:
                names.append(name)
    return names


class RepoState:
    def __init__(self, name, topics, archived):
        self.name, self.topics, self.archived = name, set(topics), bool(archived)


def fetch_states(entries):
    """Current topics and archived state for every planned repo: one listing per owner."""
    states = {}
    for owner in sorted({e.repo.split("/")[0] for e in entries}, key=str.lower):
        for r in json.loads(run_gh(["repo", "list", owner, "--limit", "1000", "--json", STATE_FIELDS]) or "[]"):
            states[r["nameWithOwner"].lower()] = RepoState(
                r["nameWithOwner"], topic_names(r.get("repositoryTopics")), r.get("isArchived"))
    for e in entries:  # anything the owner listing missed, e.g. a repo in an org you only belong to
        if e.key not in states:
            try:
                r = json.loads(run_gh(["repo", "view", e.repo, "--json", STATE_FIELDS]))
                states[e.key] = RepoState(r["nameWithOwner"], topic_names(r.get("repositoryTopics")),
                                          r.get("isArchived"))
            except (GhError, ValueError, KeyError):
                pass
    return states


# ---------------------------------------------------------------- planning

def plan_steps(entry, state, managed):
    """Ordered gh steps that bring one repo to its planned state. Empty list = already there.

    Archived repos are read-only, topics included, so retagging one means
    unarchiving first and archiving again afterwards when it should stay archived.
    """
    add = [entry.topic] if entry.topic and entry.topic not in state.topics else []
    remove = sorted((managed & state.topics) - ({entry.topic} if entry.topic else set()))
    final_archived = {"archive": True, "unarchive": False, "keep": state.archived}[entry.action]
    retag = bool(add or remove)

    steps, archived_now = [], state.archived
    if state.archived and (retag or not final_archived):
        steps.append(("unarchive",))
        archived_now = False
    if retag:
        steps.append(("edit", add, remove))
    if final_archived and not archived_now:
        steps.append(("archive",))
    return steps


def describe(steps, state):
    parts = []
    for step in steps:
        if step[0] == "edit":
            parts += [f"+{t}" for t in step[1]] + [f"-{t}" for t in step[2]]
    kinds = [s[0] for s in steps]
    if "archive" in kinds and "unarchive" in kinds:
        parts.append("(unarchive, retag, re-archive)")
    elif "archive" in kinds:
        parts.append("archive")
    elif "unarchive" in kinds:
        parts.append("unarchive")
    return " ".join(parts)


def gh_args(step, repo):
    if step[0] == "edit":
        args = ["repo", "edit", repo]
        for t in step[1]:
            args += ["--add-topic", t]
        for t in step[2]:
            args += ["--remove-topic", t]
        return args
    return ["repo", step[0], repo, "--yes"]


# ---------------------------------------------------------------- commands

def cmd_snapshot(args):
    preflight()
    raw = json.loads(run_gh(["repo", "list", args.owner, "--limit", "1000", "--json", SNAPSHOT_FIELDS]) or "[]")
    repos = [{
        "name": r["nameWithOwner"],
        "visibility": (r.get("visibility") or "").lower() or "unknown",
        "fork": bool(r.get("isFork")),
        "archived": bool(r.get("isArchived")),
        "pushed": (r.get("pushedAt") or "")[:10],
        "topics": topic_names(r.get("repositoryTopics")),
        "description": r.get("description") or "",
    } for r in raw]
    repos.sort(key=lambda r: r["name"].lower())
    doc = {"owner": args.owner,
           "generated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "repos": repos}
    text = json.dumps(doc, indent=1, ensure_ascii=False)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(f"Saved {len(repos)} repositories to {args.output}. Import it in the Repo Sort page.")
    else:
        print(text)
    return 0


def cmd_apply(args):
    try:
        with open(args.plan, encoding="utf-8-sig") as fh:
            plan = parse_plan(fh.read())
    except OSError as e:
        print(f"Can't read {args.plan}: {e.strerror}", file=sys.stderr)
        return 2
    except PlanError as e:
        print(f"{args.plan} has problems, so nothing was run:\n{e}", file=sys.stderr)
        return 2

    entries = plan.entries
    if args.only:
        wanted = {w.lower() for w in _words(",".join(args.only))}
        entries = [e for e in entries if e.key in wanted or e.key.split("/")[1] in wanted]
        if not entries:
            print("None of the --only repositories are in the plan.", file=sys.stderr)
            return 2

    preflight()
    for w in plan.warnings:
        print(f"note: {w}")
    states = fetch_states(entries)
    managed = plan.managed
    mode = "applying" if args.yes else "dry run"
    print(f"repo-sort | {mode} | {len(entries)} repositories | managed topics: {' '.join(sorted(managed)) or '-'}\n")

    width = max(len(e.repo) for e in entries)
    work, missing, unchanged = [], [], 0
    for e in entries:
        state = states.get(e.key)
        if not state:
            missing.append(e.repo)
            print(f"  {e.repo:<{width}}  not found, or you can't see it")
            continue
        steps = plan_steps(e, state, managed)
        if not steps:
            unchanged += 1
            continue
        work.append((e, state, steps))
        print(f"  {e.repo:<{width}}  {describe(steps, state)}")

    counts = {"tag": 0, "retag": 0, "archive": 0, "unarchive": 0}
    for _, state, steps in work:
        for s in steps:
            if s[0] == "edit":
                counts["retag" if s[2] else "tag"] += 1
        kinds = [s[0] for s in steps]
        if "archive" in kinds and "unarchive" not in kinds:
            counts["archive"] += 1
        if "unarchive" in kinds and "archive" not in kinds:
            counts["unarchive"] += 1
    print(f"\n{len(work)} to change: {counts['tag']} to tag, {counts['retag']} to retag, "
          f"{counts['archive']} to archive, {counts['unarchive']} to unarchive. "
          f"{unchanged} already match{'es' if unchanged == 1 else ''} the plan.")
    if missing:
        print(f"{len(missing)} not found: {', '.join(missing)}")

    if not args.yes:
        print("\nDry run: nothing was changed. Re-run with --yes to apply." if work else "\nNothing to do.")
        return 1 if missing else 0

    failures = []
    print()
    for e, state, steps in work:
        try:
            for step in steps:
                run_gh(gh_args(step, state.name))
                if args.pause:
                    time.sleep(args.pause)  # stay well under GitHub's limit on write requests
            print(f"  ok    {state.name}")
        except GhError as err:
            failures.append(state.name)
            print(f"  FAIL  {state.name}: {err}")
    done = len(work) - len(failures)
    print(f"\nDone: {done} changed, {len(failures)} failed.")
    if failures:
        print("Re-run the same command to retry; repositories that already match are skipped.")
    return 1 if failures or missing else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    snap = sub.add_parser("snapshot", help="save an owner's repositories as JSON for the page")
    snap.add_argument("owner", help="GitHub user or organization, e.g. ly2xxx")
    snap.add_argument("-o", "--output", help="file to write (default: print to the terminal)")
    snap.set_defaults(func=cmd_snapshot)

    app = sub.add_parser("apply", help="tag and archive repositories from a plan file")
    app.add_argument("plan", help="plan file, e.g. repo-plan.txt")
    app.add_argument("--yes", action="store_true", help="make the changes (default is a dry run)")
    app.add_argument("--only", action="append", metavar="REPO",
                     help="limit to these repositories (name or owner/name; repeat or comma-separate)")
    app.add_argument("--pause", type=float, default=1.0,
                     help="seconds to wait after each change (default 1.0)")
    app.set_defaults(func=cmd_apply)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except GhError as e:
        print(f"Stopped: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nStopped. Re-run the same command to continue; finished repositories are skipped.",
              file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
