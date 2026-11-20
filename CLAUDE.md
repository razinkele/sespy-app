# SESPy app — agent notes

This repo is the Shiny research UI (`sespy_app/`, `app.py`). The analysis
library is the separate `sespy` package (repo razinkele/SESPy), pinned by tag
in `pyproject.toml`. Library modules are imported as `sespy.<x>`; UI modules
as `sespy_app.<x>`. Never put the old monolith SESPy checkout on the path.

## Shiny for Python reference

Shiny ≥ 1.7.0 ships its own Agent Skills. Before writing or changing Shiny
UI, reactivity, modules, extended tasks, data frames or Playwright tests,
read the matching reference under the directory printed by
`micromamba run -n shiny shiny skills path shiny-for-python`
(`references/reactivity.md`, `modules-core.md`, `extended-tasks.md`,
`data-frames.md`, `testing.md`, `debugging.md` are the ones this app uses).
Prefer it over recollection; it tracks the installed version.
