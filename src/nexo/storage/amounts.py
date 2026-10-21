"""Amounts that the extractor named as entities.

The extraction prompt says dates, amounts and quantities are never entities, but
a model that wants to record "rated 4.8 stars" may still make "4.8 stars" a node.
`AMOUNT_ENTITIES` decides what ingestion does with such a triple:

  convert  the quantity becomes an attribute of the entity at the other end
  drop     the triple is discarded
  keep     no check (the behavior before this setting)

Only names that are *entirely* a quantity count: "4.8 stars", "$35", "28%",
"60 days", "2024-03-01". "$5 Lunch Special" and "3 Musketeers" are names.
"""

from __future__ import annotations

import re

_UNITS = (
    r"%|percent|stars?|reviews?|ratings?|votes?|mi|miles?|km|kms|ft|feet|m2|sq\s?ft|"
    r"kg|g|mg|oz|ounces?|lbs?|pounds?|ml|l|liters?|litres?|"
    r"secs?|seconds?|mins?|minutes?|hrs?|hours?|days?|weeks?|months?|years?|yrs?|"
    r"usd|eur|gbp|dollars?|cents?|x|times|units?|items?|orders?|users?|customers?|"
    r"people|visits?|views?|clicks?|downloads?|installs?"
)
# A k/M/bn multiplier counts only with a currency or a unit: "$3M", "1.6k reviews", not "3M".
_NUMBER = r"\d[\d,]*(?:\.\d+)?(?:[kKmMbB]n?)?"
_BARE_MULTIPLIED = re.compile(r"^\d[\d,]*(?:\.\d+)?[kKmMbB]n?$")
_AMOUNT = re.compile(
    rf"^(?:~|≈|about|approx\.?|approximately|around|over|under|up to|at least|"
    rf"at most|less than|more than)?\s*[-+]?[$€£¥]?\s*{_NUMBER}\s*(?:{_UNITS})?"
    rf"(?:\s*(?:-|to|–)\s*[$€£¥]?\s*{_NUMBER}\s*(?:{_UNITS})?)?$",
    re.IGNORECASE,
)
_DATE = re.compile(
    r"^(?:\d{4}-\d{2}(?:-\d{2})?|\d{1,2}/\d{1,2}/\d{2,4}|q[1-4]\s*\d{4}|\d{4})$",
    re.IGNORECASE,
)
_PREFIX = re.compile(r"^(?:HAS|IS|WITH|HAVING)_")


def is_amount(name: str) -> bool:
    """Whether an entity name is only a quantity, price, percentage or date."""
    text = name.strip()
    if not text or _BARE_MULTIPLIED.match(text):
        return False
    return bool(_AMOUNT.match(text) or _DATE.match(text))


def attribute_name(relation: str) -> str:
    """The attribute a relation names: HAS_STAR_RATING -> 'star rating'."""
    return _PREFIX.sub("", relation).replace("_", " ").lower() or relation.lower()
