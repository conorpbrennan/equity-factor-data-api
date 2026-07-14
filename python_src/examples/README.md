# Examples — one risk question per script

Each script answers one question a user actually asks, end to end, and is
self-contained (bootstraps its own imports, builds the demo micro store on
first run). Run from anywhere:

    python examples/exposures_today.py            # demo store
    python examples/exposures_today.py --aws       # the project S3 store
    python examples/exposures_today.py --root DIR # any other v2 store

| script | the question it answers |
|---|---|
| `exposures_today.py` | What are my factor exposures right now? (positions keyed by vendor ids) |
| `active_vs_benchmark.py` | What am I actually betting on, relative to my benchmark? |
| `flash_pnl.py` | What's my factor PnL *tonight*, before the official numbers land? |
| `explain_change.py` | My exposure moved — which factor, and which asset drove it? |
| `morning_workflow.py` | Start a session hot from the working set the morning job persisted |

The Claude skill (`.claude/skills/factor-data/SKILL.md`) uses the same
recipes — these scripts are the human-readable versions. To drive the same
scenarios conversationally, paste the prompts in
[claude_cli_prompts.md](claude_cli_prompts.md) into a Claude Code session
launched from the repo root.
