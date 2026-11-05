"""First-launch, user-chosen local LLM: catalog, hardware-based
recommendation, and download management.

No model ships bundled (removed after a real GitHub Releases 2 GB-per-
asset failure made bundling a model at build time a real constraint -
see CLAUDE.md). Instead the app requires the user to pick and download
one on first launch, which also means the size ceiling that previously
forced small, compromise choices (0.5B when the budget was briefly
understood as 2 GB, then Q8_0 1.5B when the GitHub asset limit forced a
revert) no longer applies - a model download at runtime is not part of
any release artifact GitHub has to accept.
"""
