"""受け持つ球団の選手の「見込み」（#308 P3）を、画面に見せる形にする。**DTO への写しはここだけ**。

見込みは domain の `outlook.roster_outlook_grade`（球団のスカウトの見立て）が出典で、ここは材料（能力・在籍・
プロフィール・世界のシード）をそろえて区分を写すだけ。年齢は締める処理と同じ「その年の4月1日」
（`offseason_player.player_age`。生年月日の無い選手は推定で補う）、在籍年数は戦力外の実績と同じ数え方
（`offseason_player.club_seasons`）。区分だけを出し、見込みの数値も伸びしろも出さない（2026-10-25 ユーザー決定 Q2 A）。
"""

from __future__ import annotations

from ..domain.pennant.offseason_player import Ratings, club_seasons, is_at_club, player_age
from ..domain.pennant.outlook import PEAK_AGE, roster_outlook_grade
from ..domain.pennant.world import World
from ..domain.simulation.ratings import RatingEmphasis
from .dto import ClubTenure, OutlookCell


def is_on_club(tenure: ClubTenure, *, team_id: int, year: int) -> bool:
    """`year` 年に球団 `team_id` に在籍しているか（判定は domain の `is_at_club`）。"""
    return is_at_club(tenure.stints, team_id=team_id, year=year)


def outlook_cell(
    ratings: Ratings, *, tenure: ClubTenure, world: World, team_id: int, player_id: int, year: int
) -> OutlookCell:
    """球団 `team_id` に在籍する選手の、`year` 年の能力から見た見込みの区分。`PEAK_AGE` 歳以上は区分なし（「—」）。"""
    grade = roster_outlook_grade(
        ratings,
        age=player_age(tenure.profile, year, start_year=world.start_year),
        position=tenure.position,
        world_seed=world.seed,
        team_id=team_id,
        player_id=player_id,
        seasons=club_seasons(tenure.stints, team_id=team_id, year=year),
    )
    if grade is None:
        return OutlookCell(grade=None, emphasis=RatingEmphasis.NONE, peak_age=PEAK_AGE)
    return OutlookCell(grade=grade.label, emphasis=grade.emphasis, peak_age=PEAK_AGE)
