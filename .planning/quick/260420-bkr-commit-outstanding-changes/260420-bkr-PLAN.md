---
phase: quick
plan: 260420-bkr
type: execute
wave: 1
depends_on: []
files_modified:
  - .planning/config.json
  - .claude/
  - .planning/debug/scanner-memory-overflow.md
  - .planning/phases/16-when-a-scanner-advertised-auto-mode-it-should-be-configurable-by-the-user-if-that-means-flatbed-mode-or-adf-mode/.gitkeep
  - .planning/phases/16-when-a-scanner-advertised-auto-mode-it-should-be-configurable-by-the-user-if-that-means-flatbed-mode-or-adf-mode/16-CONTEXT.md
  - .planning/phases/17-fix-paperless-upload-error-datetime-format-and-title-type-mismatch-in-api-payload/.gitkeep
  - .planning/phases/18-automatic-scanned-page-size-detection-or-user-specified-paper-size-to-avoid-capturing-the-full-scanner-bed/.gitkeep
  - .planning/phases/19-write-user-facing-docs-including-a-full-getting-started-section-that-walks-a-new-user-through-setup-and-first-scan-using-the-di-taxis-model/.gitkeep
autonomous: true
requirements: [quick-task]
must_haves:
  truths:
    - "All outstanding changes are committed with appropriate grouping"
    - "No sensitive files (credentials, env) are committed"
  artifacts:
    - path: ".planning/config.json"
      provides: "Updated project config"
    - path: ".planning/phases/16-*/16-CONTEXT.md"
      provides: "Phase 16 context file"
  key_links: []
---

<objective>
Commit all outstanding changes in three logical groups: planning infrastructure
updates (.claude/ memory + config.json), debug artifacts, and new phase scaffold
directories (phases 16-19).

Purpose: Clean working tree so future work starts from a known committed state.
Output: Three commits grouping related changes.
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
@$HOME/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@.planning/STATE.md
</context>

<tasks>

<task type="auto">
  <name>Task 1: Commit planning config and memory files</name>
  <files>.planning/config.json, .claude/</files>
  <action>
Stage and commit two groups of infrastructure files:

Commit 1 — planning config update:
```
git add .planning/config.json
git commit -m "chore(planning): update config.json"
```

Commit 2 — Claude memory directory (auto-generated project memory):
```
git add .claude/
git commit -m "chore(memory): add Claude project memory files"
```

Do NOT commit any file that contains API keys, tokens, or credentials. Inspect
.claude/ contents first with `ls -la .claude/` and `ls -la .claude/projects/` to
confirm only memory/MEMORY.md and similar markdown files are present before staging.
  </action>
  <verify>
    <automated>git log --oneline -3 | grep -E "(planning|memory)"</automated>
  </verify>
  <done>config.json and .claude/ memory files committed in two separate commits, no credentials included</done>
</task>

<task type="auto">
  <name>Task 2: Commit debug artifact and phase scaffold directories</name>
  <files>.planning/debug/scanner-memory-overflow.md, .planning/phases/16-*/, .planning/phases/17-*/, .planning/phases/18-*/, .planning/phases/19-*/</files>
  <action>
Commit 3 — debug artifact:
```
git add .planning/debug/scanner-memory-overflow.md
git commit -m "docs(debug): add scanner memory overflow debug notes"
```

Commit 4 — phase scaffold directories (phases 16-19 with .gitkeep and CONTEXT.md):
```
git add ".planning/phases/16-when-a-scanner-advertised-auto-mode-it-should-be-configurable-by-the-user-if-that-means-flatbed-mode-or-adf-mode/"
git add ".planning/phases/17-fix-paperless-upload-error-datetime-format-and-title-type-mismatch-in-api-payload/"
git add ".planning/phases/18-automatic-scanned-page-size-detection-or-user-specified-paper-size-to-avoid-capturing-the-full-scanner-bed/"
git add ".planning/phases/19-write-user-facing-docs-including-a-full-getting-started-section-that-walks-a-new-user-through-setup-and-first-scan-using-the-di-taxis-model/"
git commit -m "docs(planning): scaffold phase directories 16-19 with context files"
```
  </action>
  <verify>
    <automated>git status --short | wc -l</automated>
  </verify>
  <done>Working tree is clean (git status shows no untracked or modified files). All four commits visible in git log.</done>
</task>

</tasks>

<verification>
After both tasks complete:
- `git status` shows clean working tree
- `git log --oneline -5` shows the four new commits
- No credentials or .env files appear in any commit (`git show --stat HEAD~3` through `git show --stat HEAD`)
</verification>

<success_criteria>
All outstanding changes committed. Working tree clean. Four commits with logical groupings: config update, memory files, debug artifact, phase scaffolds.
</success_criteria>

<output>
No SUMMARY.md needed for quick tasks. Update STATE.md Quick Tasks Completed table with this entry:
| 260420-bkr | Commit outstanding changes (config, memory, debug, phases 16-19) | 2026-04-20 | [commit hash] | [260420-bkr-commit-outstanding-changes](./quick/260420-bkr-commit-outstanding-changes/) |
</output>
