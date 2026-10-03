"""世界の「いま」。保存せず、日程と試合から導く。

ペナントの進行の状態（今日・シーズン中か）は世界に持たない。持つと日程・試合と食い違う
ので、消化した最後の試合日と未消化の対戦が残っているかだけから決める。
"""

from __future__ import annotations

import datetime
from enum import Enum

from .schedule import ScheduleRules, default_opening_day


class SeasonPhase(Enum):
    """シーズンの局面。値は画面に出す名前。"""

    BEFORE_OPENING = "開幕前"
    IN_SEASON = "シーズン中"
    FINISHED = "シーズン終了"


def season_phase(*, has_played: bool, fixtures_pending: bool) -> SeasonPhase:
    """試合を消化したか、未消化の対戦が残っているかから局面を決める。

    試合がまだ無ければ、日程を作る前でも作った後でも開幕前。試合があって対戦が尽きていれば終了。
    """
    if not has_played:
        return SeasonPhase.BEFORE_OPENING
    return SeasonPhase.IN_SEASON if fixtures_pending else SeasonPhase.FINISHED


def world_today(start_year: int, last_played_on: datetime.date | None) -> datetime.date:
    """世界の「今日」。最後に試合をした日。まだ試合が無ければ開幕年の開幕日（年齢の基準日に使う）。

    開幕日は `default_opening_day(開幕年, ScheduleRules())`（既定の規則）で決める。日程の規則が世界ごとに
    変わるようになったら、世界の規則を受け取る形にする（いまは規則が1つなので引数にしない）。
    """
    if last_played_on is not None:
        return last_played_on
    return default_opening_day(start_year, ScheduleRules())
