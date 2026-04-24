---
quick_id: 260424-wtc
slug: remove-orphaned-agent-worktrees
status: complete
date: "2026-04-24"
---

# Quick Task 260424-wtc: Remove Orphaned Agent Worktrees

## Result

Removed two orphaned agent worktrees left over from previous Claude Code agent runs:

- `git worktree remove --force .claude/worktrees/agent-aa257c7e`
- `git worktree remove --force .claude/worktrees/agent-ac5a4ae1`
- Deleted branches: `worktree-agent-aa257c7e`, `worktree-agent-ac5a4ae1`
- `git worktree prune` run to clean up any stale refs

`git worktree list` now shows only the main worktree on `autodev`.
