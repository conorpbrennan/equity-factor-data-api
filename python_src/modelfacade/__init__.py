"""Two-layer model data access (roadmap project 2): strict core, lenient user.

Model (core.py) is the systems-facing layer: datetime.date only, internal integer
asset ids, raw vendor units, fail-fast — always right or fails fast.
ModelFacade (facade.py) wraps a core Model and adds what end users need:
string dates and 'latest', vendor security ids, canonical units, wide
dataframes, discoverability, and a pre-warmable user cache. The two layers
convert both ways (facade.core / ModelFacade(model)) and stay separate so
user-cache leniency can never leak into core computations.

    from modelfacade import ModelFacade
    model = ModelFacade.load("AX_WW4_MH")            # store from $FACTOR_STORE_ROOT
    df = model.get_factor_loadings(as_of="latest")   # wide, one line

Runs against any store produced by genv2 (normalized layout; uses the
transforms_b generic-slot tables as a fast path when present).
"""

from . import inventory
from .core import Model
from .facade import ModelFacade
from .store import Store, list_models

__all__ = ["Model", "ModelFacade", "Store", "inventory", "list_models"]
