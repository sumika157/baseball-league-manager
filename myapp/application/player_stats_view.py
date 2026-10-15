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


def played_counts(rows: list[ActivePlayerStats], *, pitchers: bool) -> dict[int, int]:
    """選手 id → 出場数（野手は出場試合数、投手は登板数）。一覧の列と並べ替えに使う。

    `list_roster(with_games=True)` で読んだ行だけを受け取る。数を読んでいない行が来たら、
    「0試合」と黙って並べずに落とす（読み忘れを画面の値で隠さない）。
    """
    counts: dict[int, int] = {}
    for row in rows:
        count = row.appearances if pitchers else row.games
        if count is None:
            raise ValueError(
                f"出場数を読んでいない行です（選手 id={row.player_id}）。with_games=True で読んでください。"
            )
        counts[row.player_id] = count
    return counts
