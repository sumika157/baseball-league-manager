"""世界の中の、ひとりの選手のある年の能力。

能力は選手 × 年で1組。野手は `BatterRatings`、投手は `PitcherRatings`（成長型はどちらも持つ）。
翌年の能力は新しい組として足す（オフの処理）ので、年ごとの推移が残る。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..simulation.ratings import BatterRatings, PitcherRatings
from ..value_objects import Season


@dataclass(frozen=True)
class PlayerRatings:
    """選手 × 年の能力。"""

    player_id: int
    year: int
    ratings: BatterRatings | PitcherRatings

    def __post_init__(self) -> None:
        # 年として成り立たない値はここで弾く
        object.__setattr__(self, "year", Season(self.year).year)

    @property
    def is_pitcher(self) -> bool:
        return isinstance(self.ratings, PitcherRatings)
