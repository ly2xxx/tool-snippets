# repo-sort

Sort a pile of GitHub repositories into categories, then apply that to GitHub in one run: each category becomes a **topic** on its repositories, and anything you mark is **archived**. Nothing is moved, renamed or deleted, so links keep working.

Two parts:

- **`repo-sort.html`** — the page where you do the sorting. Tap repositories to select them and move them between categories (or drag them on a desktop), mark dead ones for archiving, and copy the plan it writes.
- **`repo_sort.py`** — reads that plan and makes the changes with the GitHub CLI. Dry run by default.

Needs Python 3.8+ and the [GitHub CLI](https://cli.github.com) signed in with `gh auth login`. Standard library only.

## Quick start

```powershell
cd repo-sort

# 1. Export your repositories (names, descriptions, current topics)
python repo_sort.py snapshot ly2xxx -o repos.json

# 2. Open repo-sort.html, choose Import, pick repos.json, and sort.
#    When Unsorted is empty, Copy plan and save it here as repo-plan.txt.

# 3. See what would change. Nothing is touched.
python repo_sort.py apply repo-plan.txt

# 4. Try one repository, then all of them
python repo_sort.py apply repo-plan.txt --yes --only aidev
python repo_sort.py apply repo-plan.txt --yes
```

Then browse a category on GitHub: `https://github.com/search?q=user:ly2xxx+topic:homelab&type=repositories`

## The page

Open `repo-sort.html` straight from disk and it saves your sorting in that browser. Published as a Claude artifact, it saves to the artifact's own store instead, so you can sort on your phone and copy the plan on your laptop.

- **Import** takes the `snapshot` JSON, the JSON Claude's `list_repos` tool returns, or a plan you copied earlier. Re-importing a snapshot keeps the categories you already chose and adds any new repositories to Unsorted.
- **Categories** lets you rename them, change their topic, add and delete. Renaming or deleting a topic adds it to a `retire:` line so the script removes it from GitHub too.
- **Unsorted** and **No tag** both add no topic. The difference is that Unsorted means "not decided yet", so the progress bar only fills when it is empty.

## The plan file

```text
categories: portfolio homelab learning      # topics this tool manages
retire: old-lab                             # former topics, removed wherever found
ly2xxx/aidev    portfolio   keep            # repository, topic, action
ly2xxx/kata     learning    archive
ly2xxx/gco      -           keep            # "-" = no category topic
```

Actions are `keep` (leave archived state as it is), `archive` and `unarchive`. Each repository ends up with exactly one managed topic, the one on its line. Topics the plan doesn't manage (say, `python` or `streamlit`) are never touched. You can write or edit a plan by hand; the script checks the whole file and lists every problem before it changes anything.

## Safety

- `apply` is a dry run unless you pass `--yes`.
- Re-running is safe. Repositories already in the planned state are skipped, so after an interruption or a failure you just run the same command again.
- Archived repositories are read-only on GitHub, topics included. To retag one that should stay archived, the script unarchives it, changes the topics and archives it again, and says so in the dry run.
- It pauses one second after each change (`--pause`) to stay well under GitHub's limit on write requests.
- `repos.json` and `repo-plan.txt` list your private repositories by name, so this folder's `.gitignore` keeps them out of git.

## Tests

```bash
python -m unittest discover -s tests -v
```

The end-to-end tests put a fake `gh` on the path, so they never touch GitHub. They run on macOS and Linux and skip on Windows.
