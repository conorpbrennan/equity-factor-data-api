"""Function-signature conventions: canonical parameter names.

Short but meaningful argument names, one spelling per concept. A function that
takes a date-of-record takes `as_of`; a range takes `start` and `end`
(inclusive); nobody has to remember whether this library wanted asof,
as_of_date, or business_date.
"""

from __future__ import annotations

CANONICAL_PARAMS: dict[str, str] = {
    "as_of": "single date of record (COB); core layer: datetime.date, "
             "user layer also accepts 'YYYY-MM-DD' and 'latest'",
    "start": "range start date, inclusive",
    "end": "range end date, inclusive",
    "model": "model_id string at the user layer, Model object at the core",
    "assets": "sequence of asset identifiers; core: internal int asset_id, "
              "user layer also accepts vendor ids (see sec_id_type)",
    "sec_id_type": "identifier scheme of `assets` when not internal",
    "factors": "sequence of factor_id strings; None = all factors",
    "version": "publication version; 1 = original, >1 = restatement",
}

# Spellings the toolkit should flag in review (each seen in the wild).
DISCOURAGED = {
    "asof": "as_of", "as_of_date": "as_of", "date": "as_of",
    "business_date": "as_of", "cobdate": "as_of",
    "date_from": "start", "date_to": "end",
    "from_date": "start", "to_date": "end",
    "model_name": "model", "ids": "assets", "securities": "assets",
}
