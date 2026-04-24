---
quick_id: 260424-wtc
slug: remove-orphaned-agent-worktrees
description: Remove orphaned agent worktrees
date: "2026-04-24"
status: planned
---

# Quick Task 260424-wtc: Remove Orphaned Agent Worktrees

## Goal

Remove two stale agent worktree directories left over from previous Claude Code agent runs.

## Tasks

### Task 1: Remove orphaned worktrees and branches

**Files:** `.claude/worktrees/agent-aa257c7e`, `.claude/worktrees/agent-ac5a4ae1`

**Action:**
```bash
git worktree remove --force .claude/worktrees/agent-aa257c7e
git worktree remove --force .claude/worktrees/agent-ac5a4ae1
git branch -D worktree-agent-aa257c7e
git branch -D worktree-agent-ac5a4ae1
git worktree prune
```

**Verify:** `git worktree list` shows only the main worktree.

**Done:** No stale worktrees remain.
