<!--
Thanks for contributing to KubeIntellect! 💙
Please fill this out so reviewers can move fast. See CONTRIBUTING.md for the full guide.
-->

## What & why

<!-- What does this PR change, and why? Link the issue it addresses. -->

Closes #

## Type of change

- [ ] 🐛 Bug fix
- [ ] ✨ New feature
- [ ] 📖 Docs
- [ ] 🧹 Refactor / chore
- [ ] ⚡ Performance
- [ ] 🧪 Tests

## Scope

- Version directory touched: <!-- e.g. v4/ -->
- [ ] This change is scoped to a version whose contributions are open (`v4/`, or docs/typos in older versions).

## Checklist

- [ ] New behavior has both a **happy-path** and an **error-path** test
- [ ] **Every mutating/write operation keeps its dry-run + diff + human-approval (HITL) gate** (safety invariant)
- [ ] Secret values are never logged or returned (key names only)
- [ ] `uv run pytest` passes locally
- [ ] `uv run ruff check .` passes locally
- [ ] `uv run mypy src` passes locally
- [ ] Docs updated if behavior/CLI/flags changed
- [ ] Commits are signed off (`git commit -s` — DCO)

## Notes for reviewers

<!-- Anything reviewers should focus on, known trade-offs, follow-ups, screenshots/asciinema, etc. -->
