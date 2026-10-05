"""参照クエリが読んだ選手の成績（`ActivePlayerStats`）を、順位づけと一覧の関数が受け取る形にする。

ランキングの規則（規定・並び順）と一覧の行は、ドメインの関数と画面の行の組み立てが `Player` を受け取る。
集約（`Team`）を組み立てずにその形へ合わせるための、小さな写し。
"""

from __future__ import annotations

from ..domain.entities import Player
from ..domain.value_objects import JerseyNumber
from .dto import ActivePlayerStats


def stats_player(row: ActivePlayerStats) -> Player:
    """順位づけと一覧に渡す選手。経歴・主将歴は使わないので持たせない（主将かどうかは行が持つ）。"""
    return Player(
        id=row.player_id,
        name=row.name,
        number=JerseyNumber(row.number),
        position=row.position,
        profile=row.profile,
        batting=row.batting,
        pitching=row.pitching,
    )
