"""Tests for repo_sort.py. Run from the repo-sort folder:  python -m unittest -v

The end-to-end tests put a fake `gh` on PATH that keeps repository state in a
JSON file, so they exercise the real command line without touching GitHub.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import repo_sort  # noqa: E402

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
state_path, log_path = os.environ["FAKE_GH_STATE"], os.environ["FAKE_GH_LOG"]
state = json.load(open(state_path))
args = sys.argv[1:]
with open(log_path, "a") as fh:
    fh.write(" ".join(args) + "\n")

def find(name):
    for r in state:
        if r["nameWithOwner"].lower() == name.lower():
            return r
    sys.stderr.write("GraphQL: Could not resolve to a Repository with the name '%s'.\n" % name)
    sys.exit(1)

def save():
    json.dump(state, open(state_path, "w"))

def shape(r):
    return {**r, "repositoryTopics": [{"name": t} for t in r["topics"]]}

if args[:2] == ["auth", "status"]:
    sys.exit(0)
if args[:2] == ["repo", "list"]:
    print(json.dumps([shape(r) for r in state if r["nameWithOwner"].split("/")[0].lower() == args[2].lower()]))
elif args[:2] == ["repo", "view"]:
    print(json.dumps(shape(find(args[2]))))
elif args[:2] == ["repo", "edit"]:
    r = find(args[2])
    if r["isArchived"]:
        sys.stderr.write("HTTP 403: Repository was archived so is read-only.\n")
        sys.exit(1)
    rest = args[3:]
    for flag, topic in zip(rest[::2], rest[1::2]):
        if flag == "--add-topic" and topic not in r["topics"]:
            r["topics"].append(topic)
        if flag == "--remove-topic" and topic in r["topics"]:
            r["topics"].remove(topic)
    save()
elif args[:2] in (["repo", "archive"], ["repo", "unarchive"]):
    find(args[2])["isArchived"] = args[1] == "archive"
    save()
else:
    sys.stderr.write("fake gh: unsupported %s\n" % args)
    sys.exit(1)
'''


def repo(name, topics=(), archived=False, **extra):
    return {"nameWithOwner": name, "topics": list(topics), "isArchived": archived,
            "visibility": "PUBLIC", "isFork": False, "pushedAt": "2026-09-01T10:00:00Z",
            "description": "", **extra}


class ParsePlanTests(unittest.TestCase):
    def test_reads_directives_comments_and_default_action(self):
        plan = repo_sort.parse_plan(textwrap.dedent("""
            # a comment
            categories: portfolio, homelab learning
            retire: old-lab
            me/aidev   portfolio            # trailing comment
            me/kata    learning   archive
            me/gco     -          keep  # unsorted
        """))
        self.assertEqual(plan.categories, ["portfolio", "homelab", "learning"])
        self.assertEqual(plan.retired, ["old-lab"])
        self.assertEqual([(e.repo, e.topic, e.action) for e in plan.entries],
                         [("me/aidev", "portfolio", "keep"), ("me/kata", "learning", "archive"),
                          ("me/gco", None, "keep")])
        self.assertEqual(plan.managed, {"portfolio", "homelab", "learning", "old-lab"})

    def test_collects_every_problem_before_failing(self):
        with self.assertRaises(repo_sort.PlanError) as ctx:
            repo_sort.parse_plan(textwrap.dedent("""
                categories: Portfolio
                me/a   home_lab
                me/b   learning  delete
                not-a-repo  learning
                me/a   learning
                me/c
            """))
        msg = str(ctx.exception)
        for fragment in ("'Portfolio' is not a valid", "'home_lab' is not a valid", "not 'delete'",
                         "'not-a-repo' is not an owner/repo", "already planned on line 3",
                         "expected 'owner/repo topic [action]'"):
            self.assertIn(fragment, msg)

    def test_topic_outside_categories_is_managed_with_a_warning(self):
        plan = repo_sort.parse_plan("categories: a\nme/x b\n")
        self.assertIn("b", plan.managed)
        self.assertTrue(any("'b' is not on the categories line" in w for w in plan.warnings))

    def test_empty_plan_is_an_error(self):
        with self.assertRaises(repo_sort.PlanError):
            repo_sort.parse_plan("categories: a\n# nothing else\n")


class PlanStepsTests(unittest.TestCase):
    def steps(self, topic, action, topics=(), archived=False, managed=("a", "b")):
        entry = repo_sort.Entry("me/x", topic, action, 1)
        return repo_sort.plan_steps(entry, repo_sort.RepoState("me/x", topics, archived), set(managed))

    def test_adds_topic(self):
        self.assertEqual(self.steps("a", "keep"), [("edit", ["a"], [])])

    def test_moves_between_categories_and_leaves_unmanaged_topics(self):
        self.assertEqual(self.steps("b", "keep", topics=("a", "python")), [("edit", ["b"], ["a"])])

    def test_no_category_strips_managed_topics_only(self):
        self.assertEqual(self.steps(None, "keep", topics=("a", "python")), [("edit", [], ["a"])])

    def test_already_matching_is_empty(self):
        self.assertEqual(self.steps("a", "keep", topics=("a",)), [])
        self.assertEqual(self.steps("a", "archive", topics=("a",), archived=True), [])

    def test_tags_before_archiving(self):
        self.assertEqual(self.steps("a", "archive"), [("edit", ["a"], []), ("archive",)])

    def test_retagging_an_archived_repo_unarchives_then_rearchives(self):
        self.assertEqual(self.steps("b", "keep", topics=("a",), archived=True),
                         [("unarchive",), ("edit", ["b"], ["a"]), ("archive",)])

    def test_unarchive(self):
        self.assertEqual(self.steps("a", "unarchive", topics=("a",), archived=True), [("unarchive",)])
        self.assertEqual(self.steps("a", "unarchive", topics=("a",)), [])

    def test_topic_shapes(self):
        self.assertEqual(repo_sort.topic_names([{"name": "a"}, {"topic": {"name": "b"}}, "c"]), ["a", "b", "c"])
        self.assertEqual(repo_sort.topic_names({"nodes": [{"topic": {"name": "d"}}]}), ["d"])
        self.assertEqual(repo_sort.topic_names(None), [])


@unittest.skipUnless(os.name == "posix", "the fake gh is a POSIX executable script")
class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.gh = os.path.join(d, "gh")
        with open(self.gh, "w") as fh:
            fh.write(FAKE_GH.replace("#!/usr/bin/env python3", "#!" + sys.executable, 1))
        os.chmod(self.gh, 0o755)
        self.state_path, self.log_path = os.path.join(d, "state.json"), os.path.join(d, "calls.log")
        self.plan_path = os.path.join(d, "repo-plan.txt")
        self.env = {"PATH": d + os.pathsep + os.environ.get("PATH", ""),
                    "FAKE_GH_STATE": self.state_path, "FAKE_GH_LOG": self.log_path}
        self._old_env = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        self.write_state([
            repo("me/aidev"),
            repo("me/kata", topics=["python"]),
            repo("me/oldlab", topics=["homelab"], archived=True),
            repo("me/gco", topics=["homelab"]),
        ])

    def tearDown(self):
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def write_state(self, rows):
        with open(self.state_path, "w") as fh:
            json.dump(rows, fh)
        open(self.log_path, "w").close()

    def state(self):
        with open(self.state_path) as fh:
            return {r["nameWithOwner"]: r for r in json.load(fh)}

    def calls(self):
        with open(self.log_path) as fh:
            return [line.split() for line in fh.read().splitlines()]

    def run_cli(self, *args, plan=None):
        if plan is not None:
            with open(self.plan_path, "w") as fh:
                fh.write(textwrap.dedent(plan))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = repo_sort.main(list(args))
        return code, out.getvalue(), err.getvalue()

    PLAN = """
        categories: portfolio learning homelab
        me/aidev   portfolio  keep
        me/kata    learning   archive
        me/oldlab  learning   keep
        me/gco     -          keep   # unsorted
    """

    def test_dry_run_changes_nothing(self):
        code, out, _ = self.run_cli("apply", self.plan_path, "--pause", "0", plan=self.PLAN)
        self.assertEqual(code, 0)
        self.assertIn("Dry run: nothing was changed", out)
        self.assertIn("+learning archive", out)
        mutating = [c for c in self.calls() if c[1] in ("edit", "archive", "unarchive")]
        self.assertEqual(mutating, [])

    def test_apply_then_rerun_is_a_no_op(self):
        code, out, _ = self.run_cli("apply", self.plan_path, "--yes", "--pause", "0", plan=self.PLAN)
        self.assertEqual(code, 0, out)
        s = self.state()
        self.assertEqual(s["me/aidev"]["topics"], ["portfolio"])
        self.assertEqual(sorted(s["me/kata"]["topics"]), ["learning", "python"])  # unmanaged kept
        self.assertTrue(s["me/kata"]["isArchived"])
        self.assertEqual(s["me/oldlab"]["topics"], ["learning"])
        self.assertTrue(s["me/oldlab"]["isArchived"])  # keep = stays archived after retag
        self.assertEqual(s["me/gco"]["topics"], [])  # "-" strips the managed topic
        open(self.log_path, "w").close()
        code, out, _ = self.run_cli("apply", self.plan_path, "--yes", "--pause", "0")
        self.assertEqual(code, 0)
        self.assertIn("4 already match the plan", out)
        self.assertEqual([c for c in self.calls() if c[1] in ("edit", "archive", "unarchive")], [])

    def test_only_limits_the_run(self):
        self.run_cli("apply", self.plan_path, "--yes", "--pause", "0", "--only", "aidev", plan=self.PLAN)
        s = self.state()
        self.assertEqual(s["me/aidev"]["topics"], ["portfolio"])
        self.assertEqual(s["me/kata"]["topics"], ["python"])

    def test_bad_plan_stops_before_calling_gh(self):
        code, _, err = self.run_cli("apply", self.plan_path, plan="me/aidev Portfolio\n")
        self.assertEqual(code, 2)
        self.assertIn("nothing was run", err)
        self.assertEqual(self.calls(), [])

    def test_missing_repo_is_reported_and_others_still_apply(self):
        code, out, _ = self.run_cli("apply", self.plan_path, "--yes", "--pause", "0",
                                    plan="me/aidev portfolio\nme/ghost portfolio\n")
        self.assertEqual(code, 1)
        self.assertIn("me/ghost", out)
        self.assertIn("not found", out)
        self.assertEqual(self.state()["me/aidev"]["topics"], ["portfolio"])

    def test_snapshot_normalises_gh_output(self):
        out_path = os.path.join(self.tmp.name, "repos.json")
        code, out, _ = self.run_cli("snapshot", "me", "-o", out_path)
        self.assertEqual(code, 0)
        with open(out_path) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["owner"], "me")
        kata = next(r for r in doc["repos"] if r["name"] == "me/kata")
        self.assertEqual(kata, {"name": "me/kata", "visibility": "public", "fork": False, "archived": False,
                                "pushed": "2026-09-01", "topics": ["python"], "description": ""})


if __name__ == "__main__":
    unittest.main()
