"""世界の「いま」。保存せず、日程と試合から導く。

ペナントの進行の状態（今日・シーズン中か）は世界に持たない。持つと日程・試合と食い違う
ので、消化した最後の試合日と未消化の対戦が残っているかだけから決める。
"""

from __future__ import annotations

import datetime
from enum import Enum

from .schedule import AdvanceTarget, ScheduleRules, default_opening_day

# 画面の1回の「進める」で作ってよい試合数の上限。8リーグの1週間（約140試合）が収まる大きさで、
# 静かな環境で約4秒、負荷時でも約10秒（設計書 12.「P3b の詳細」）
MAX_GAMES_PER_ADVANCE = 150
# 画面から進められる範囲（ボタンの並び順）。「月末まで」「シーズン終了まで」は1回の上限を超えうるので出さない。
# 画面に出す選択肢も、受け付ける範囲も、ここが唯一の出典
SCREEN_ADVANCE_TARGETS = (AdvanceTarget.DAY, AdvanceTarget.NEXT_MANAGED_GAME, AdvanceTarget.WEEK)
# 「進めた結果」のまとめに載せる期間の上限（日）。長く進めても、読める量に丸める
MAX_SUMMARY_DAYS = 31


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


def summary_period(
    since: datetime.date | None, today: datetime.date | None
) -> tuple[datetime.date, datetime.date] | None:
    """「進めた結果」のまとめの期間（`since` より後から `today` まで）。出さないときは None。

    `since` は URL から来る値なので信用しない。無い・今日以後（未来や、まだ進めていない日）は
    エラーにせずまとめを出さない。期間が長すぎるときは `MAX_SUMMARY_DAYS` 日に丸める。
    返す開始日は「この日**より後**の試合を数える」基準の日（その日の試合は含まない）。
    """
    if since is None or today is None or since >= today:
        return None
    return max(since, today - datetime.timedelta(days=MAX_SUMMARY_DAYS)), today
