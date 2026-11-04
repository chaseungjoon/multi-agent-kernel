# 10. Git integration

> [Contributing guide](../../CONTRIBUTING.md) · ← [9. Planner](09-planner.md) · [11. Session lifecycle](11-session-lifecycle.md) →
>
> **Code:** `mak/git_integration/` · **Tests:** `tests/git_integration/`

`mak/git_integration/git.py` treats Git as an **audit log**. Lock discipline
already prevents conflicting writes, so commits go directly to the working branch.

- **`commit_task(task_id, files, description, agent_type, session_id)`** commits
  exactly `files` with a `[MAK-<task_id>]` subject and a Files/Status/Agent/Session
  body, or returns `None` if they match HEAD. The commit is built in a **private
  index** (`GIT_INDEX_FILE` = `.git/mak-index-<uuid>`, seeded from HEAD, or empty
  on a repo with no HEAD), so the user's staged changes are never swept in and
  the user's index is never touched. Afterwards `git update-index --add` re-stats
  only the committed paths so `git status` stays clean.
- **`validate_clean_state()`** — enforced when `git.require_clean_tree` is on
  (off by default).
- **`ensure_initialized()`** — when `auto_commit` is on, guarantees the work dir
  is its **own** repo (running `git init` if it is nested in an outer repo or in
  none), setting a local identity only if git has none.
- **`get_session_commits(session_id)`** and **`push(branch, remote)`**.

Every operation raises `GitIntegrationError` with stderr on failure.

---

[Contributing guide](../../CONTRIBUTING.md) · ← [9. Planner](09-planner.md) · [11. Session lifecycle](11-session-lifecycle.md) →
