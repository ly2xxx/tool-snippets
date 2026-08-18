# 1. Abort the broken rebase
git rebase --abort

# 2. Create an orphan branch with no history
git checkout --orphan clean-root

# 3. Stage the current files (sanitized versions)
git add -A

# 4. Commit them as the new root
git commit -m "Initial public release"

# 5. Delete the old main branch and rename clean-root to main
git branch -D main
git branch -m main

# 6. Force-push (the old history is gone forever, but no one has the old clone yet since it's not public)
git push --force-with-lease origin main