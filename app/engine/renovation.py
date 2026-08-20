"""Indicative renovation cost guidance — a "Potential renovation" section
sourced entirely from signals this codebase already extracts (EPC rating,
plus the same description/key-features keyword scan `scoring.py` already
runs for the value-add score), paired with generic, clearly-labelled UK-
wide average cost bands.

This is deliberately NOT a property-specific quote. This project has no
way to inspect a listing's actual current condition, exact floor area, or
local labour rates, so presenting a single precise renovation figure would
be misleading in the same way a fabricated comparable sale would be — the
same "never fabricate, never mislead" principle applied throughout this
codebase. What CAN be done honestly: surface which generic renovation
categories are actually *relevant* to THIS listing (based on real,
inspectable signals), and give each a defensible, always-a-range UK-wide
cost estimate, always paired with a "get local quotes" caveat.

Cost bands are broad 2025/26 UK averages for typical 2-4 bed residential
work (tradesperson-comparison-site survey data, e.g. Checkatrade/
MyBuilder-style ranges) — dated (`_ESTIMATES_AS_OF`) so staleness is
visible; review periodically, the same way the SDLT bands in financial.py
are dated and flagged for periodic review.

Keyword detection reuses a windowed-context technique already established
in this codebase (see rightmove.py's `_detect_price_qualifier` for the
original: price qualifiers/tenure are only trusted in a small window
around their actual display, not scanned across the whole page, after a
real false-positive this caused). Here the risk is different but
analogous: "recently renovated" or "already extended" describes work
that's DONE, not needed — a naive substring match on "renovat"/"extend"
would misread that as a signal that the work still needs doing, which
matters more here than in scoring.py's soft score nudge, because this
section attaches a £ estimate to the claim.

Bed-count scaling (added after live testing surfaced a real problem): EPC
rating and description keywords are BOTH commonly absent on Rightmove's
SEO-description fallback extraction path (the day-to-day path per
rightmove.py's own docstring — see README's "Data sourcing" table), which
means two completely different properties with neither signal would
otherwise get byte-identical cosmetic-refresh-only estimates, which reads
as broken even though each number is technically computed fresh. `beds`,
by contrast, IS reliably extracted on that same fallback path (also per
the README). So every cost band is scaled by a `beds`-derived multiplier
— a real, extracted, per-listing signal, not a fabricated one — since a
5-bed house genuinely costs more to redecorate/re-insulate/modernise than
a 1-bed flat. `beds is None` (rare) falls back to the neutral 1.0
multiplier, i.e. today's unscaled bands.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.extractors.rightmove import ListingData

# Words that flip a keyword's meaning from "needs doing" to "already
# done" when they appear immediately before it, e.g. "recently renovated",
# "already extended", "newly converted loft" — same false-positive class
# as the "shared ownership" glossary bug fixed in rightmove.py.
_COMPLETION_SIGNAL = re.compile(
    r"\b(recently|newly|fully|already|just|beautifully|tastefully|comprehensively|completely)\s+[\w\-]*\s*$",
    re.I,
)

# Keywords where the "needs doing" reading requires the completion-signal
# guard above — each can legitimately describe finished work instead.
_MODERNISATION_NEED_KEYWORDS = ["renovat", "refurb", "modernis", "modernize"]
_EXTENSION_NEED_KEYWORDS = ["extend", "extension", "side return"]
_LOFT_NEED_KEYWORDS = ["loft"]

# Keywords that are inherently forward-looking — "potential", "planning
# permission", "STPP" (subject to planning permission) don't have a
# "already done" reading, so no guard needed for these.
_MODERNISATION_OPPORTUNITY_KEYWORDS = ["potential", "development"]
_PLANNING_KEYWORDS = ["planning", "stpp"]

# Broad UK-wide 2025/26 averages for typical 2-4 bed residential work —
# always a (low, high) range, never a single figure.
_COSMETIC_REFRESH = (2_500, 7_000)
_ENERGY_UPGRADE = (4_000, 12_000)
_MODERNISATION = (18_000, 35_000)
_LOFT_CONVERSION = (30_000, 55_000)
_EXTENSION = (40_000, 65_000)

_ESTIMATES_AS_OF = "2025/26"

_NOTE = (
    "Indicative UK-wide averages for typical residential work — not a "
    "quote for this specific property's condition, size, or location. "
    "Get 2-3 local tradesperson quotes before budgeting."
)

# Bands above are calibrated for a typical 3-bed property; scaled by beds
# for anything else. Capped rather than linear — a 6-bed house doesn't
# cost 3x a 2-bed to redecorate, floor area grows sub-linearly with beds
# in practice.
_BED_MULTIPLIERS = {1: 0.6, 2: 0.8, 3: 1.0, 4: 1.3, 5: 1.6}
_BED_MULTIPLIER_CAP = 1.9
_BED_MULTIPLIER_STEP_ABOVE_5 = 0.25


def _bed_multiplier(beds: int | None) -> float:
    if beds is None or beds < 1:
        return 1.0
    if beds in _BED_MULTIPLIERS:
        return _BED_MULTIPLIERS[beds]
    if beds > 5:
        return min(_BED_MULTIPLIERS[5] + (beds - 5) * _BED_MULTIPLIER_STEP_ABOVE_5, _BED_MULTIPLIER_CAP)
    return 1.0  # unreachable given beds >= 1, kept as a safe fallback


def _round_to_nearest(n: float, nearest: int = 250) -> int:
    return int(round(n / nearest) * nearest)


def _scale(band: tuple[int, int], multiplier: float) -> tuple[int, int]:
    low, high = band
    return _round_to_nearest(low * multiplier), _round_to_nearest(high * multiplier)


@dataclass
class RenovationItem:
    label: str
    low: int
    high: int
    rationale: str


@dataclass
class RenovationEstimate:
    items: list[RenovationItem]
    total_low: int
    total_high: int
    as_of: str = _ESTIMATES_AS_OF
    note: str = _NOTE


def _text(listing: ListingData) -> str:
    return f"{listing.description} {' '.join(listing.key_features)}".lower()


def _has_need_signal(text: str, keywords: list[str], window_chars: int = 30) -> bool:
    """True if any keyword appears in `text` without being immediately
    preceded by a completion word (see _COMPLETION_SIGNAL) — i.e. a real
    "this still needs doing" signal, not "this has already been done"."""
    for keyword in keywords:
        start = 0
        while True:
            idx = text.find(keyword, start)
            if idx == -1:
                break
            window = text[max(0, idx - window_chars):idx]
            if not _COMPLETION_SIGNAL.search(window):
                return True
            start = idx + 1
    return False


def _has_any(text: str, keywords: list[str]) -> bool:
    return any(kw in text for kw in keywords)


def estimate_renovation(listing: ListingData) -> RenovationEstimate:
    """Never returns an empty estimate — a cosmetic-refresh baseline
    applies to essentially every resale property regardless of any other
    signal, so (unlike the other engine modules) there's no "unavailable"
    state here; what varies listing-to-listing is which ADDITIONAL
    categories apply on top of that baseline."""
    text = _text(listing)
    epc = (listing.epc_rating or "").upper()
    multiplier = _bed_multiplier(listing.beds)
    size_note = f", scaled for a {listing.beds}-bed property" if listing.beds else ""

    items: list[RenovationItem] = [
        RenovationItem(
            "Cosmetic refresh (decorating, flooring, minor repairs)",
            *_scale(_COSMETIC_REFRESH, multiplier),
            f"Baseline — applies to most resale properties regardless of other signals{size_note}",
        )
    ]

    if epc in ("D", "E", "F", "G"):
        items.append(
            RenovationItem(
                "Energy efficiency upgrade (insulation, heating, glazing)",
                *_scale(_ENERGY_UPGRADE, multiplier),
                f"EPC {epc} on this listing — real scope to improve efficiency{size_note}",
            )
        )

    if _has_need_signal(text, _MODERNISATION_NEED_KEYWORDS) or _has_any(text, _MODERNISATION_OPPORTUNITY_KEYWORDS):
        items.append(
            RenovationItem(
                "Kitchen & bathroom modernisation",
                *_scale(_MODERNISATION, multiplier),
                f"Listing description signals renovation/modernisation potential{size_note}",
            )
        )

    if _has_need_signal(text, _LOFT_NEED_KEYWORDS):
        items.append(
            RenovationItem(
                "Loft conversion",
                *_scale(_LOFT_CONVERSION, multiplier),
                f"Listing description specifically mentions loft potential{size_note}",
            )
        )

    if _has_need_signal(text, _EXTENSION_NEED_KEYWORDS) or _has_any(text, _PLANNING_KEYWORDS):
        items.append(
            RenovationItem(
                "Single-storey extension (subject to planning)",
                *_scale(_EXTENSION, multiplier),
                f"Listing description signals extension/planning potential{size_note}",
            )
        )

    total_low = sum(i.low for i in items)
    total_high = sum(i.high for i in items)
    return RenovationEstimate(items=items, total_low=total_low, total_high=total_high)
