# Development Rules

## 项目设计标准

设计方案与实际改动均须遵循 [AGENT.md](AGENT.md)，尤其是其中的“项目定位与优化目标”和“核心能力设计标准”。长期产品与能力标准集中维护在该文件；具体方案记录本次范围和取舍，不把现有实现当作不可替换的前提。

## Git Workflow

- Before editing files, run `git status --short` and `git branch --show-current`.
- Do not overwrite or delete existing user changes.
- Work on a task branch, not directly on `main` or `master`.
- Keep changes small and focused.
- After changes, review `git diff` before reporting completion.
- Before committing, run relevant tests or explain why tests were not run.
- Before an approved `git push`, review the commits being pushed and update `CHANGELOG.md` with their main user-visible changes. Features, API/Schema, permission, deployment, runtime, and compatibility changes must be recorded; formatting-only changes, test-only refactors, and generated static hashes do not need separate entries.
- Include the `CHANGELOG.md` update in the same push as the related code. Do not push while the changelog omits a main change from the outgoing commits.
- Only reference commits, PRs, and tags that actually exist. Never record credentials, tokens, private tenant data, or sensitive runtime artifacts in the changelog.
- Do not run `git push`, `git reset --hard`, `git clean -fd`, `git rebase`, or `git commit --amend` unless explicitly approved.

## Code Changes

- Make the minimal change needed to solve the task.
- Do not mix unrelated refactors with feature or bug fixes.
- Follow the existing project style and naming conventions.
- Update tests or docs when behavior changes.
- Do not add new dependencies without approval.

## Code Comments

- Use Chinese for new or updated code comments and explanatory docstrings by default.
- Comments should explain intent, edge cases, or complex logic instead of restating the code.
- Keep technical terms, protocol names, API names, error codes, and third-party library names in English when translation would reduce clarity.

## Final Response

Always report:

- Current branch
- Files changed
- Tests run and results
- Whether a commit was created
- Remaining risks or TODOs