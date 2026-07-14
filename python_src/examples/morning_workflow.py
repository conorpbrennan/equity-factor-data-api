"""Q: How does a session start hot from the morning job's working set?

The producer/consumer pattern end to end: if the scheduled job
(warm_cache.py) has persisted a working set for this model, adopt it and
serve the day's questions from memory; if it hasn't, warm one now and
persist it for the next session. Either way the queries afterwards are
identical — the cache only changes where answers come from.

    python examples/morning_workflow.py [--aws | --root DIR] [--model ID]
"""

from _bootstrap import setup

from analytics import Portfolio, exposures
from modelfacade import ModelFacade


def main() -> None:
    root, model_id = setup(__doc__)
    fac = ModelFacade.load(model_id, root)
    positions = [1, 2, 3]

    try:
        fac.load_cache()                       # the morning job ran
        print("adopted persisted working set:", fac.cache.stats)
    except FileNotFoundError:                  # it didn't — be the job
        fac.warm(positions)
        saved_to = fac.save_cache()
        print("no persisted set found; warmed and saved to", saved_to)

    # the day's questions — covered ones served from memory
    today = fac.core.dates()[1]
    fac.get_factor_loadings(today, assets=[1, 2])
    fac.get_specific_risk(today, assets=[3])
    book = Portfolio.from_holdings("book", today, {1: 10.0, 2: 20.0})
    exposures(fac, book)                       # analytics ride the core

    print("after a morning of queries:", fac.cache.stats)


if __name__ == "__main__":
    main()
