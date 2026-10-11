"""世界の作成時に、分岐した選手の初期能力を実成績から決める。

推定の式は `simulation/estimate.py`。ここは選手全員を束ねる（リーグ全体の実測を求めてから
ひとりずつ推定し、成長型を引く）。**純粋な関数**で、DB にも Django にも触れない。

成長型は隠し値。乱数は選手ごとに `blake2b(世界のシード, 年, 選手)` から作るので、
同じ世界のシードなら何度作っても同じ能力になり、選手を足し引きしても他の選手の成長型は動かない。

**分岐元に試合の成績が無い選手（野手は打席 0、投手はアウト 0）は2軍の層として、分布から引く**
（`prospects.py`。自動ドラフトの新人と同じ引き方で、平均は錨の尺度の `farm_rating_mean`）。推定に通すと全員が
同じ事前の値に並び、散らばりの調整（`spread.py`。平均と標準偏差で標準化して目標へ写す）の母集団の
標準偏差が縮んで、成績のある1軍の選手が大きく押し上げられる（支配下 60人台＋育成の実データで、失点 4.66・
本塁打王 62本になった。#175）。そこで**散らばりの調整の母集団は成績のある選手だけ**にする。
2軍の層の乱数も選手ごとの `blake2b(世界のシード, 年, "farm-" 選手)` なので、同じシードで再現できる。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

from ..entities import Player
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
from .prospects import draw_prospect_ratings, farm_rating_mean
from .ratings import PlayerRatings

# 年齢を数える日（開幕の月の初日）
SEASON_START_MONTH = 4

# 能力の高さ（総合値）を、成長型の傾きに使う尺度にする（10 点 = 1）
STRENGTH_UNIT = 10.0


def age_at_season_start(profile: Profile, year: int) -> int | None:
    """開幕年の4月1日時点の満年齢。生年月日が無い・その日より後に生まれたことになる選手は None。

    年齢は保持せず生年月日から求める。現実の今日ではなく、**世界の年**を渡す。
    """
    return profile.age_or_none(date(year, SEASON_START_MONTH, 1))


def estimate_initial_ratings(
    records: Sequence[CareerRecord],
    *,
    seed: int,
    year: int,
    baseline: LeagueBaseline = NPB,
) -> list[PlayerRatings]:
    """選手全員の初期能力。`records` と同じ順に返す。

    成績のある選手は 推定 → 散らばりの調整（`spread.py`。成績のある選手全員を見渡して行う）、成績の無い選手は
    2軍の層として分布から引く。成長型は調整後の能力の高さで傾くので、最後に引く。
    """
    norms = LeagueNorms.of(records)
    played = [index for index, record in enumerate(records) if has_record(record)]
    spread = spread_ratings([_estimate(records[index], norms, baseline=baseline) for index in played])
    by_index = dict(zip(played, spread, strict=True))
    return [
        _with_growth(
            record,
            by_index[index] if index in by_index else _farm_ratings(record, seed=seed, year=year),
            seed=seed,
            year=year,
        )
        for index, record in enumerate(records)
    ]


def has_record(record: CareerRecord) -> bool:
    """能力を推定できる成績があるか。投手は投げたアウト、野手は打席で見る（登録位置の役割の成績）。"""
    if record.position.is_pitcher:
        return record.pitching.innings.outs > 0
    return record.batting.plate_appearances > 0


def _farm_ratings(record: CareerRecord, *, seed: int, year: int) -> BatterRatings | PitcherRatings:
    """成績の無い選手（2軍の層）の能力。乱数の種は成長型と同じく分岐元の選手の id。"""
    rng = make_random(game_seed(seed, year, f"farm-{_seed_key(record)}"))
    return draw_prospect_ratings(rng, mean=farm_rating_mean(record.position), position=record.position)


def _seed_key(record: CareerRecord) -> int:
    return record.player_id if record.seed_player_id is None else record.seed_player_id


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
    rng = make_random(game_seed(seed, year, f"growth-{_seed_key(record)}"))
    growth = draw_growth_type(rng, age=record.age, strength=strength)
    return PlayerRatings(player_id=record.player_id, year=year, ratings=with_growth(ratings, growth))
