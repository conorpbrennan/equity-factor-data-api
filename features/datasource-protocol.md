# Datasource Protocol

**Requirement**: Invert the core→store dependency: core owns a DataSource protocol (semantic, model-agnostic reads); Store implements it; facade stays the composition root — so Chris's curated model store can drop in as a second implementation without core changes.

**Started**: 2026-07-15
**Last updated**: 2026-07-15
**Branch**: datasource-protocol

## Files involved



- .claude/skills/factor-data/SKILL.md
- python_src/examples/exposures_today.py
- python_src/examples/morning_workflow.py
- python_src/modelfacade/README.md
- python_src/modelfacade/__init__.py
- python_src/modelfacade/cache.py
- python_src/modelfacade/core.py
- python_src/modelfacade/datasource.py
- python_src/modelfacade/facade.py
- python_src/modelfacade/selftest.py
- python_src/modelfacade/store.py
- python_src/run_analytics.py
- python_src/usage_example.py
- python_src/warm_cache.py

## History

- 2026-07-15 `774a325` — facade: caching off by default, opt in with cache=UserCache()
  - .claude/skills/factor-data/SKILL.md
  - python_src/examples/morning_workflow.py
  - python_src/modelfacade/README.md
  - python_src/modelfacade/__init__.py
  - python_src/modelfacade/cache.py
  - python_src/modelfacade/facade.py
  - python_src/modelfacade/selftest.py
  - python_src/run_analytics.py
  - python_src/usage_example.py
  - python_src/warm_cache.py

- 2026-07-15 `6e46aa6` — core: invert the store dependency behind a core-owned DataSource protocol
  - python_src/examples/exposures_today.py
  - python_src/modelfacade/__init__.py
  - python_src/modelfacade/core.py
  - python_src/modelfacade/datasource.py
  - python_src/modelfacade/facade.py
  - python_src/modelfacade/selftest.py
  - python_src/modelfacade/store.py
  - python_src/usage_example.py
