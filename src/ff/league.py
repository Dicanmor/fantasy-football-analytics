"""League format used by the value model and the power rankings."""
from __future__ import annotations

from dataclasses import dataclass, field

POSITIONS = ("QB", "RB", "WR", "TE")


@dataclass(frozen=True)
class League:
    """PPR, 1 QB / 2 RB / 2 WR / 1 TE (+ K, DST) and 5 bench spots, no flex.

    K and DST are left out of the model on purpose: they are streamable, so their value over replacement is
    close to zero and they would not change trades or rankings meaningfully.
    """

    teams: int = 12
    starters: dict[str, int] = field(default_factory=lambda: {"QB": 1, "RB": 2, "WR": 2, "TE": 1})
    bench: int = 5
    # Rough number of bench spots per team that end up holding each position (the rest go to K/DST/other).
    # These are assumptions, not estimates: they only move the replacement level and are easy to change.
    bench_share: dict[str, float] = field(
        default_factory=lambda: {"QB": 0.4, "RB": 1.8, "WR": 1.8, "TE": 0.5}
    )

    def replacement_rank(self, position: str) -> int:
        """How deep in the position's ranking a typical league rosters players (= replacement level)."""
        return round(self.teams * (self.starters[position] + self.bench_share[position]))
