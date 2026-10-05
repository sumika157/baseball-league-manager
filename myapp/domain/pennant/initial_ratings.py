"""世界の作成時に、分岐した選手の初期能力を実成績から決める。

推定の式は `simulation/estimate.py`。ここは選手全員を束ねる（リーグ全体の実測を求めてから
ひとりずつ推定し、成長型を引く）。**純粋な関数**で、DB にも Django にも触れない。

成長型は隠し値。乱数は選手ごとに `blake2b(世界のシード, 年, 選手)` から作るので、
同じ世界のシードなら何度作っても同じ能力になり、選手を足し引きしても他の選手の成長型は動かない。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

from ..entities import Player
from ..exceptions import InvalidProfile
from ..simulation.baseline import NPB, LeagueBaseline
from ..simulation.estimate import (
    CareerRecord,
    LeagueNorms,
    draw_growth_type,
    estimate_batter_ratings,
    estimate_pitcher_ratings,
    with_growth,
)
from ..simulation.randomness import game_seed, make_random
from ..simulation.ratings import AVERAGE_RATING, BatterRatings, PitcherRatings
from ..simulation.spread import spread_ratings
from ..value_objects import FieldingLine, Profile
from .ratings import PlayerRatings

# 年齢を数える日（開幕の月の初日）
SEASON_START_MONTH = 4

# 能力の高さ（総合値）を、成長型の傾きに使う尺度にする（10 点 = 1）
STRENGTH_UNIT = 10.0


def age_at_season_start(profile: Profile, year: int) -> int | None:
    """開幕年の4月1日時点の満年齢。生年月日が無い・その日より後に生まれたことになる選手は None。

    年齢は保持せず生年月日から求める。現実の今日ではなく、**世界の年**を渡す。
    """
    try:
        return profile.age(date(year, SEASON_START_MONTH, 1))
    except InvalidProfile:
        return None


def estimate_initial_ratings(
    records: Sequence[CareerRecord],
    *,
    seed: int,
    year: int,
    baseline: LeagueBaseline = NPB,
) -> list[PlayerRatings]:
    """選手全員の初期能力。`records` と同じ順に返す。

    推定 → 散らばりの調整（`spread.py`。全員を見渡して行う）→ 成長型、の順。成長型は調整後の
    能力の高さで傾くので、最後に引く。
    """
    norms = LeagueNorms.of(records)
    estimated = [_estimate(record, norms, baseline=baseline) for record in records]
    return [
        _with_growth(record, ratings, seed=seed, year=year)
        for record, ratings in zip(records, spread_ratings(estimated), strict=True)
    ]


def career_record(
    source: Player, fielding: Mapping[int, FieldingLine], *, year: int, player_id: int | None = None
) -> CareerRecord:
    """分岐元の選手から、推定の材料を作る。**材料の組み立てはここだけ**にする。

    世界の作成と確認用のコマンドが別々に組み立てると、リーグ全体の実測（`LeagueNorms`）の
    母集団がずれて、確認で合った水準が世界の作成で合わなくなる。

    `player_id` は能力を結びつける選手（世界の作成では写した先）。省略は分岐元の選手。
    成長型の乱数の種は、どちらでも分岐元の選手の id にする（同じシードなら、同じ分岐元から作った
    世界は、確認用のコマンドとも、同じ成長型になる）。`fielding` は分岐元の選手の id → 守備成績の合計。
    """
    assert source.id is not None, "分岐元は保存済みの選手"
    return CareerRecord(
        player_id=source.id if player_id is None else player_id,
        position=source.position,
        batting=source.batting,
        pitching=source.pitching,
        fielding=fielding.get(source.id, FieldingLine()),
        age=age_at_season_start(source.profile, year),
        seed_player_id=source.id,
    )


def _estimate(record: CareerRecord, norms: LeagueNorms, *, baseline: LeagueBaseline) -> BatterRatings | PitcherRatings:
    if record.position.is_pitcher:
        return estimate_pitcher_ratings(record.pitching, baseline=baseline)
    return estimate_batter_ratings(record.batting, record.fielding, record.position, norms, baseline=baseline)


def _with_growth(
    record: CareerRecord, ratings: BatterRatings | PitcherRatings, *, seed: int, year: int
) -> PlayerRatings:
    value = ratings.pitching_value if isinstance(ratings, PitcherRatings) else ratings.batting_value
    strength = (value - AVERAGE_RATING) / STRENGTH_UNIT
    key = record.player_id if record.seed_player_id is None else record.seed_player_id
    rng = make_random(game_seed(seed, year, f"growth-{key}"))
    growth = draw_growth_type(rng, age=record.age, strength=strength)
    return PlayerRatings(player_id=record.player_id, year=year, ratings=with_growth(ratings, growth))
