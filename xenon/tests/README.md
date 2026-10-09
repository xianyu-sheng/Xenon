# tests tree merged

The package-local test tree was merged into the repository root `tests/`
(commit "merge dual test trees"). This directory exists only so that the
legacy CI command `pytest tests xenon/tests` keeps working until the CI
workflow config is updated.

No test files live here.
