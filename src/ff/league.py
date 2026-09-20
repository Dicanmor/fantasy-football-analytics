"""League format used by the value model, the power rankings and the trade tools.

The website reads the real lineup from the Sleeper league (``roster_positions``); this class holds the
same information for Python and the default (12 teams, 1 QB / 2 RB / 2 WR / 1 TE / 1 FLEX / K / DEF, 5 bench).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

POSITIONS = ("QB", "RB", "WR", "TE")

# Which positions each slot accepts. Slot names follow Sleeper's ``roster_positions``.
SLOT_ELIGIBLE: dict[str, tuple[str, ...]] = {
    "QB": ("QB",), "RB": ("RB",), "WR": ("WR",), "TE": ("TE",), "K": ("K",), "DEF": ("DEF",),
    "FLEX": ("RB", "WR", "TE"),
    "SUPER_FLEX": ("QB", "RB", "WR", "TE"),
    "REC_FLEX": ("WR", "TE"),
    "WRRB_FLEX": ("WR", "RB"),
}

# How the players who fill a flex slot are spread over positions (assumption used for replacement levels).
FLEX_SHARE: dict[str, dict[str, float]] = {
    "FLEX": {"RB": 0.45, "WR": 0.45, "TE": 0.10},
    "SUPER_FLEX": {"QB": 0.70, "RB": 0.10, "WR": 0.15, "TE": 0.05},
    "REC_FLEX": {"WR": 0.60, "TE": 0.40},
    "WRRB_FLEX": {"RB": 0.50, "WR": 0.50},
}

# Rough number of bench spots per team that end up holding each position (assumption, easy to change).
BENCH_SHARE: dict[str, float] = {"QB": 0.4, "RB": 1.8, "WR": 1.8, "TE": 0.5}
KD_DEPTH = 0.5  # K and DEF are streamed: replacement is ~1.5 x teams deep


def _round_half_up(x: float) -> int:
    """Same rounding as JavaScript's Math.round for positive numbers (Python's round() is half-to-even)."""
    return math.floor(x + 0.5 + 1e-9)


def slot_positions(slot: str) -> tuple[str, ...]:
    return SLOT_ELIGIBLE[slot]


@dataclass(frozen=True)
class League:
    """PPR league. ``slots`` is the starting lineup (no bench)."""

    teams: int = 12
    slots: tuple[str, ...] = ("QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "K", "DEF")
    bench: int = 5
    ir: int = 3
    bench_share: dict[str, float] = field(default_factory=lambda: dict(BENCH_SHARE))

    @property
    def starters(self) -> dict[str, int]:
        """Positional starters only (flex handled separately), e.g. {'QB': 1, 'RB': 2, ...}."""
        out: dict[str, int] = {}
        for s in self.slots:
            if s in ("QB", "RB", "WR", "TE", "K", "DEF"):
                out[s] = out.get(s, 0) + 1
        return out

    def replacement_rank(self, position: str) -> int:
        """How deep in the position's ranking a typical league rosters players (= replacement level)."""
        if position in ("K", "DEF"):
            return _round_half_up(self.teams * (self.starters.get(position, 1) + KD_DEPTH))
        n = self.starters.get(position, 0)
        for s in self.slots:
            n += FLEX_SHARE.get(s, {}).get(position, 0.0)
        return _round_half_up(self.teams * (n + self.bench_share.get(position, 0.0)))
