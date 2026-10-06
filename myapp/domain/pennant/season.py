"""世界の「いま」。保存せず、日程と試合から導く。

ペナントの進行の状態（今日・シーズン中か）は世界に持たない。持つと日程・試合と食い違う
ので、消化した最後の試合日と未消化の対戦が残っているかだけから決める。
"""

from __future__ import annotations

import datetime
from enum import Enum

from .schedule import MAX_GAMES_PER_ADVANCE as MAX_GAMES_PER_ADVANCE
from .schedule import AdvanceTarget, ScheduleRules, default_opening_day
from .world import MAX_SEASONS_PER_WORLD

# 画面から進められる範囲（ボタンの並び順。判断8: 目立つのは先頭の「1日」）。
# 1回の上限（`MAX_GAMES_PER_ADVANCE`。schedule.py が出典）を超える範囲は、件数が多い世界では選択肢から隠れる
# （「月末まで」「上限まで」。「シーズン終了まで」は画面に出さない）。
# 画面に出す選択肢も、受け付ける範囲も、ここが唯一の出典
SCREEN_ADVANCE_TARGETS = (
    AdvanceTarget.DAY,
    AdvanceTarget.NEXT_MANAGED_GAME,
    AdvanceTarget.WEEK,
    AdvanceTarget.MONTH_END,
    AdvanceTarget.LIMIT,
)
# 「進めた結果」のまとめに載せる期間の上限（日）。長く進めても、読める量に丸める
MAX_SUMMARY_DAYS = 31


class SeasonPhase(Enum):
    """シーズンの局面。値は画面に出す名前。"""

    BEFORE_OPENING = "開幕前"
    IN_SEASON = "シーズン中"
    FINISHED = "シーズン終了"


def season_phase(*, last_played_on: datetime.date | None, next_fixture_on: datetime.date | None) -> SeasonPhase:
    """最後に試合をした日と、次の対戦の日から局面を決める（年をまたいで使える。設計書 3.6）。

    - 試合がまだ無ければ、日程を作る前でも作った後でも開幕前
    - 試合があって対戦が尽きていれば終了（オフ）
    - 対戦が残っていて、次の対戦が最後の試合より後の年なら、翌年の開幕前（締めた直後など）
    - それ以外はシーズン中
    """
    if last_played_on is None:
        return SeasonPhase.BEFORE_OPENING
    if next_fixture_on is None:
        return SeasonPhase.FINISHED
    return SeasonPhase.BEFORE_OPENING if next_fixture_on.year > last_played_on.year else SeasonPhase.IN_SEASON


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


def ratings_year(*, next_game_on: datetime.date | None, last_played_on: datetime.date | None, start_year: int) -> int:
    """試合と編成と画面が使う能力の年。**能力を引く年の唯一の出典**。

    日程が残っていればその最初の日の年（次に試合をする年）、無ければ最後に試合をした年、
    まだ試合が無ければ開幕年。シミュレーション（日を進める）・編成・能力の表示が同じ年を使い、
    翌年の能力ができても（P6）、画面に出る能力と試合で使われる能力が食い違わないようにする。
    """
    if next_game_on is not None:
        return next_game_on.year
    if last_played_on is not None:
        return last_played_on.year
    return start_year


def season_year(
    *, next_fixture_on: datetime.date | None, last_played_on: datetime.date | None, start_year: int
) -> int:
    """世界の「いまの年度」。画面（GM ホーム・世界バー・一覧）が出す年。

    能力を引く年と同じ規則なので `ratings_year` をそのまま呼ぶ（年の決め方を2つに増やさない）。
    """
    return ratings_year(next_game_on=next_fixture_on, last_played_on=last_played_on, start_year=start_year)


def is_final_season(*, current_year: int, start_year: int) -> bool:
    """いまの年度が世界の最後のシーズン（`MAX_SEASONS_PER_WORLD` 番目）か。最後のシーズンは締められない。"""
    return current_year - start_year + 1 >= MAX_SEASONS_PER_WORLD


def is_season_closed(*, year: int, start_year: int, next_year_ratings_exist: bool) -> bool:
    """`year` 年のシーズンを締めたか。**「締めたか」の唯一の出典**（二重実行の検査とオフの結果の画面が使う）。

    締める処理だけが翌年（`year + 1`）の能力を作る。世界の作成は開幕年の能力を作るので、
    開幕年より前の年（`year + 1` が開幕年）は、翌年の能力があっても締めた年ではない。
    """
    return year >= start_year and next_year_ratings_exist


def stats_year(*, phase: SeasonPhase, last_played_on: datetime.date | None, current_year: int) -> int:
    """GM ホームの順位表・主力・タイトルに出す年。

    締めた後の開幕前は、翌年の成績がまだ無いので前年（最後に試合をした年）。それ以外はいまの年度。
    """
    if phase is SeasonPhase.BEFORE_OPENING and last_played_on is not None:
        return last_played_on.year
    return current_year
