# Risk Profile And Panels

**Requirement**: Close the three highest-value gaps from Chris's 2026-07-15 deck review: a RiskProfile scope (PAS requirement — factor exposures + specific-risk positions, analyzable like a portfolio), get_security_panel (loadings + specific risk + returns in one joined call), and date-range loadings on the facade.

**Started**: 2026-07-15
**Last updated**: 2026-07-15
**Branch**: risk-profile-and-panels

## Files involved


- .claude/skills/factor-data/SKILL.md
- python_src/analytics/README.md
- python_src/analytics/__init__.py
- python_src/analytics/functions.py
- python_src/analytics/riskprofile.py
- python_src/analytics/selftest.py
- python_src/modelfacade/core.py
- python_src/modelfacade/facade.py
- python_src/modelfacade/selftest.py

## History

- 2026-07-15 `51c62f2` — analytics + facade: RiskProfile, volatility, security panel, date-range loadings
  - .claude/skills/factor-data/SKILL.md
    - python_src/analytics/README.md
  - python_src/analytics/__init__.py
  - python_src/analytics/functions.py
  - python_src/analytics/riskprofile.py
  - python_src/analytics/selftest.py
  - python_src/modelfacade/core.py
  - python_src/modelfacade/facade.py
  - python_src/modelfacade/selftest.py
        