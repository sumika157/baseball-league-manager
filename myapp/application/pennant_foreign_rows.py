"""外国人の獲得の、オフ（#169 F2）と期中（#169 F3）が共有する組み立て。

「外国人」タブの行（今の外国人・候補）と、獲得した外国人の背番号・背ネームの材料。オフのサービス
（`pennant_offseason.PennantOffseasonService`）・期中の GM のサービス
（`pennant_foreign_signing.ForeignSigningService`）・判定日の AI（`pennant_midseason_review.MidseasonReview`）が、
互いのモジュールに依存せずに使う。
"""

from __future__ import annotations

from collections import Counter

from ..domain.entities import Team
from ..domain.pennant.foreign_market import ForeignCandidate, candidate_outlook_grade, scouting_report
from .dto import DevelopmentRoster, ForeignCandidateRow, ForeignHeldRow, SimulationTeam
from .pennant_outlook import grade_outlook_cell
from .pennant_ratings import overall_cell, rating_cells


def held_foreign_rows(team: SimulationTeam, development: DevelopmentRoster) -> list[ForeignHeldRow]:
    """球団にいま在籍する外国人（支配下、続けて育成）。**保有の人数の出典**（`len` が `ensure_signable` の
    `held_foreign`）。枠の数え方は呼び手が入れる（ここでは数える扱い）。

    `team` は参照クエリ `SimulationContextQuery.teams()` の球団（支配下だけ）、`development` は
    `DevelopmentRosterQuery.for_team` の育成選手。
    """
    registered = [
        ForeignHeldRow(p.player_id, p.name, p.position, is_development=False, counts_toward_quota=True)
        for p in team.players
        if p.is_foreign
    ]
    developmental = [
        ForeignHeldRow(p.player_id, p.name, p.tenure.position, is_development=True, counts_toward_quota=True)
        for p in development.players
        if p.tenure.profile.is_foreign_player
    ]
    return registered + developmental


def foreign_candidate_row(candidate: ForeignCandidate, *, world_seed: int, is_chosen: bool) -> ForeignCandidateRow:
    """「外国人」タブの候補ひとり（オフの指名と期中の獲得が共有する）。能力は球団のスカウトの見立てだけ。"""
    report = scouting_report(candidate, world_seed=world_seed)
    return ForeignCandidateRow(
        index=candidate.key.index,
        name=candidate.name,
        position=candidate.position,
        age=candidate.age,
        background=candidate.background,
        overall=overall_cell(report.ratings),
        cells=rating_cells(report.ratings),
        outlook=grade_outlook_cell(candidate_outlook_grade(candidate, report)),
        is_chosen=is_chosen,
    )


def used_numbers(team: Team) -> frozenset[str]:
    """球団でいま使われている背番号（支配下・育成とも）。期中に獲得した外国人の背番号の材料。"""
    return frozenset(player.number.value for player in team.active_players)


def surname_counts(team: Team) -> dict[str, int]:
    """球団の在籍選手の背ネームの苗字の人数（獲得した外国人の背ネームに名前の頭文字を付けるかの材料）。"""
    return dict(
        Counter(player.profile.back_name.split(".")[-1] for player in team.active_players if player.profile.back_name)
    )
