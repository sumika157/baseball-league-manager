"""投手の疲労。**保存しない。** 直近の登板の記録（`PitchingHistory`）と、その試合の対戦打者数から導く。

**率への効かせ方は能力の目減り**（`fatigued()`）。疲労点のぶん球威・制球・一発回避を下げた能力を
`odds.matchup()` に渡すので、効き方の出典は β のまま1つで、新しい率の式を作らない。

**AI の普段の起用では疲労点が0になるように置く**（6人ローテは中5日を割らない・交代は受け持ちの目安＋猶予の
内側）。疲労が効くのは酷使（手動の5人ローテの中4日、ブルペンが尽きたときの続投）だけなので、
リーグの水準（基準値・β）を調整し直さずに済む。

| 疲労の材料 | 0点（普段の起用） | 点 |
| --- | --- | --- |
| 先発の休養（前回の先発からの日数） | 中5日以上 | 中4日 3・中3日 6・中2日以下 10 |
| 試合中の続投 | 受け持ちの目安＋猶予（`MID_INNING_GRACE`）まで | 超えた打者1人ごとに1 |

救援の連投・直近の登板数は段階2（#97）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from enum import Enum
from functools import lru_cache

from .odds import RATING_UNIT
from .ratings import RATING_MIN, PitcherRatings

# 前回の先発から空ける日数。「中5日」は間に5日挟む＝6日後の登板
MIN_DAYS_BETWEEN_STARTS = 6
# 前の2日続けて投げた投手は、3連投になるので投げさせない
MAX_CONSECUTIVE_DAYS = 2
# 連投の判断に要る直近の日数（MAX_CONSECUTIVE_DAYS に余裕を1日足す）。
# 直近の登板を読む側と、画面に出す側の出典
RECENT_PITCHING_DAYS = MAX_CONSECUTIVE_DAYS + 1
# 交代のしきい値を超えても、回の途中ではこの人数ぶん待つ（回の区切りで代えたい）。
# AI の交代の規則（`manager.should_change_pitcher`）と、試合中の疲労の数え始めの両方の出典
MID_INNING_GRACE = 2

# 疲労点の上限。能力1段（`odds.RATING_UNIT` = 15点。率が exp(β) 倍になる幅）ぶん
MAX_FATIGUE_POINTS = int(RATING_UNIT)
# 先発の休養の疲労点。前回の先発からの日数 → 点。表に無い日数（6日以上）は0点、2日以下は最も重い点
_STARTER_REST_POINTS = {5: 3, 4: 6}
_STARTER_SHORTEST_REST_POINTS = 10
# 区分の「重い」の下限。中3日の先発（6点）から
HEAVY_FATIGUE_POINTS = 6


@dataclass(frozen=True)
class PitcherWorkload:
    """その日に投げるときの負荷。直近の登板から導く（保存しない）。

    疲労点の入力。故障（#41）を足すときも、故障の確率の入力にこれを使う。
    """

    days_since_start: int | None  # 前回の先発から何日か。先発の記録が無ければ None
    consecutive_days: int  # 前日から遡って、何日続けて投げているか


class FatigueLevel(Enum):
    """疲労の区分。**区分の境目の唯一の出典**で、画面・AI に境目の点数を書かない。"""

    NONE = "なし"
    LIGHT = "軽い"
    HEAVY = "重い"

    @property
    def label(self) -> str:
        return self.value

    @classmethod
    def from_points(cls, points: int) -> FatigueLevel:
        """疲労点から区分を引く。なし（0点）・軽い（1〜5点。中4日の先発）・重い（6点〜。中3日以下の先発）。"""
        if points <= 0:
            return cls.NONE
        if points < HEAVY_FATIGUE_POINTS:
            return cls.LIGHT
        return cls.HEAVY


class PitchingHistory:
    """直近の登板の記録。疲労はここから導く（保存する状態は持たない）。"""

    def __init__(self) -> None:
        self._pitched: dict[int, set[date]] = {}
        self._last_start: dict[int, date] = {}

    def record(self, played_on: date, pitcher_ids: Sequence[int]) -> None:
        """その日の登板を記録する。先頭が先発。"""
        for pitcher_id in pitcher_ids:
            self._pitched.setdefault(pitcher_id, set()).add(played_on)
        if pitcher_ids:
            # 日付の順に記録するとは限らない。いちばん新しい先発を覚える
            self._last_start[pitcher_ids[0]] = max(played_on, self._last_start.get(pitcher_ids[0], played_on))

    def days_since_start(self, pitcher_id: int, today: date) -> int | None:
        """前回の先発から何日経ったか。先発の記録が無ければ None。"""
        last = self._last_start.get(pitcher_id)
        return None if last is None else (today - last).days

    def pitched_on(self, pitcher_id: int, day: date) -> bool:
        return day in self._pitched.get(pitcher_id, ())

    def consecutive_days_before(self, pitcher_id: int, today: date) -> int:
        """今日の前日から遡って、何日続けて投げているか。"""
        days = self._pitched.get(pitcher_id)
        if not days:
            return 0
        count = 0
        day = today - timedelta(days=1)
        while day in days:
            count += 1
            day -= timedelta(days=1)
        return count

    def can_pitch_on(self, pitcher_id: int, today: date) -> bool:
        """今日、連投の上限に達していないか（直前の `MAX_CONSECUTIVE_DAYS` 日続けて投げていれば投げさせない）。"""
        return self.consecutive_days_before(pitcher_id, today) < MAX_CONSECUTIVE_DAYS

    def workload(self, pitcher_id: int, today: date) -> PitcherWorkload:
        """今日投げるときの負荷。"""
        return PitcherWorkload(
            days_since_start=self.days_since_start(pitcher_id, today),
            consecutive_days=self.consecutive_days_before(pitcher_id, today),
        )


def starter_rest_points(days_since_start: int | None) -> int:
    """先発の休養不足の疲労点。中5日以上（6日〜）と、先発の記録が無い投手は0点。"""
    if days_since_start is None or days_since_start >= MIN_DAYS_BETWEEN_STARTS:
        return 0
    return _STARTER_REST_POINTS.get(days_since_start, _STARTER_SHORTEST_REST_POINTS)


def pregame_fatigue_points(workload: PitcherWorkload, *, as_starter: bool) -> int:
    """登板前の疲労点。段階1では先発の休養だけを数える（救援の連投は段階2）。

    スタミナはかけない。スタミナは先発の受け持ちの長さに既に効いていて、二重に効かせない。
    """
    points = starter_rest_points(workload.days_since_start) if as_starter else 0
    return min(points, MAX_FATIGUE_POINTS)


def in_game_fatigue_points(faced: int, planned_target: int) -> int:
    """試合中の続投の疲労点。次の打者が、当初の目安＋猶予を何人超えるか（超えた打者1人ごとに1点）。

    `faced` はこの登板で既に対戦した打者数。**当初の目安で数える**（ブルペンが尽きて目安を延ばしても、
    延ばしたぶんは疲労として残る）。AI の普段の交代は目安＋猶予の内側なので0点。
    """
    return max(0, faced + 1 - (planned_target + MID_INNING_GRACE))


def fatigued(ratings: PitcherRatings, points: int) -> PitcherRatings:
    """疲労点のぶん、球威・制球・一発回避を下げた能力。1未満にはしない。スタミナと成長型は保つ。

    点は `MAX_FATIGUE_POINTS` で頭打ちにする。0点なら同じ能力をそのまま返す。
    """
    points = min(max(points, 0), MAX_FATIGUE_POINTS)
    if points == 0:
        return ratings
    return _fatigued(ratings, points)


@lru_cache(maxsize=1 << 12)
def _fatigued(ratings: PitcherRatings, points: int) -> PitcherRatings:
    # 能力は frozen なので覚えてよい。打席ごとに作り直すと検査（__post_init__）が毎回走る
    return replace(
        ratings,
        stuff=max(RATING_MIN, ratings.stuff - points),
        control=max(RATING_MIN, ratings.control - points),
        home_run_avoidance=max(RATING_MIN, ratings.home_run_avoidance - points),
    )
