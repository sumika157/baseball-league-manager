"""推定した能力の散らばりを、調整（P1）で決めた分布に合わせる。

実成績から収縮推定した能力は、選手どうしの差が小さい（打席の少ない選手が平均へ寄るうえ、
控えを含む全員が中ほどに集まる）。そのまま使うと、首位打者や本塁打王などの水準が
P1 の目標帯より低くなる。

**順位は保ったまま、項目ごとの散らばりだけを合わせる。** 項目ごと・野手 / 投手ごとに、推定値を
母集団の平均と標準偏差で標準化し、目標の平均・標準偏差（`samples.py` の分布）へ写して 1〜100 に丸める
（一次式なので、強い選手は強いまま、成績の無い選手は平均より低いまま）。

    能力 = 目標の平均 + 目標の標準偏差 × (推定値 − 母集団の平均) ÷ 母集団の標準偏差

平均も目標に写すのは、P1 の分布が「この分布なら水準が目標帯に入る」と調整したものだから。
推定の平均を保ったまま標準偏差だけ広げる案も測ったが、防御率・失点が元の実データより
11〜12% 高く出て、写す案（6〜8%）より悪かった（Wiki「ペナントモードの仕組み」の「能力と推定」）。

母集団は呼び出し側が渡す。世界の作成では**成績のある選手だけ**（成績の無い選手は全員が同じ事前の値に並び、
標準偏差を縮めて成績のある選手を押し上げるので、2軍の層として分布から引く。`pennant/initial_ratings.py`）、
オフの水準の錨では世界の現役の**支配下**の選手全員で写像を決め、育成にも同じ写像を当てる（`pennant/offseason.py`。
毎年の育成の流入で1軍の水準が動かないように。#198 D2）。母集団の外の選手にも同じ写像を当てるときは
`population` で印を渡す。
人数が `MIN_POPULATION` に満たないと標準偏差が安定しないので、その区分は手を付けない。
スタミナは先発と救援で2つに分かれる値（`samples.spread_club` と同じ）で、成績から決まる先発の割合を
反映しているので、散らばりを動かさない。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from statistics import fmean, pstdev

from .ratings import RATING_MAX, RATING_MIN, BatterRatings, PitcherRatings
from .samples import BATTER_ITEM_OFFSET, BATTER_MEAN, BATTER_SD, PITCHER_MEAN, PITCHER_SD

# 標準偏差を求めるのに最低限必要な人数。これに満たない区分（野手 / 投手）は元のまま返す
MIN_POPULATION = 30

# 散らばりを合わせる項目。守備力は登録位置で平均が違うが、位置の偏りも母集団の標準偏差に含まれる
# （目標の平均は野手の平均。位置ごとの傾向は推定が持っている）
_BATTER_ITEMS = ("contact", "power", "eye", "speed", "fielding")
_PITCHER_ITEMS = ("stuff", "control", "home_run_avoidance")


def target_mean(name: str, *, pitcher: bool) -> float:
    """項目の目標の平均。**錨の目標の出典**（`samples.py` の分布）。"""
    return PITCHER_MEAN if pitcher else BATTER_MEAN + BATTER_ITEM_OFFSET.get(name, 0.0)


def target_sd(*, pitcher: bool) -> float:
    """項目の目標の標準偏差。"""
    return PITCHER_SD if pitcher else BATTER_SD


def rescale(
    values: Sequence[int], goal_mean: float, goal_sd: float, *, population: Sequence[bool] | None = None
) -> list[int]:
    """順位を保ったまま、平均と標準偏差を目標に写す。母集団に差が無ければ（標準偏差 0）そのまま。

    `population` は値ごとの「写像を決める母集団に入れるか」の印（省くと全員）。写像（中心と散らばり）は印の付いた
    値だけで決め、**全員に同じ写像を当てる**（オフの錨は支配下で決め、育成にも同じ写像を当てる。#198 D2）。
    """
    base = (
        list(values)
        if population is None
        else [value for value, inside in zip(values, population, strict=True) if inside]
    )
    spread = pstdev(base) if len(base) > 1 else 0.0
    if spread == 0.0:
        return list(values)
    center = fmean(base)
    return [
        min(RATING_MAX, max(RATING_MIN, round(goal_mean + goal_sd * (value - center) / spread))) for value in values
    ]


def _population_size(count: int, population: Sequence[bool] | None) -> int:
    return count if population is None else sum(1 for inside in population if inside)


def spread_batter_ratings(
    ratings: Sequence[BatterRatings], *, population: Sequence[bool] | None = None
) -> list[BatterRatings]:
    """野手の能力の散らばりを合わせる。入力と同じ順に返す。`population` は `rescale` と同じ。

    `LABELS` に無いフィールド（成長型・伸びしろ）は変えない（作り直さず `replace` で項目だけを差し替える）。
    """
    if _population_size(len(ratings), population) < MIN_POPULATION:
        return list(ratings)

    def column(name: str) -> list[int]:
        return rescale(
            [getattr(item, name) for item in ratings],
            target_mean(name, pitcher=False),
            target_sd(pitcher=False),
            population=population,
        )

    contact, power, eye, speed, fielding = (column(name) for name in _BATTER_ITEMS)
    return [
        replace(item, contact=contact[i], power=power[i], eye=eye[i], speed=speed[i], fielding=fielding[i])
        for i, item in enumerate(ratings)
    ]


def spread_pitcher_ratings(
    ratings: Sequence[PitcherRatings], *, population: Sequence[bool] | None = None
) -> list[PitcherRatings]:
    """投手の能力の散らばりを合わせる（スタミナを除く）。入力と同じ順に返す。
    `population` は `rescale` と同じ。成長型・伸びしろ・スタミナは変えない（`replace` で項目だけを差し替える）。
    """
    if _population_size(len(ratings), population) < MIN_POPULATION:
        return list(ratings)

    def column(name: str) -> list[int]:
        return rescale(
            [getattr(item, name) for item in ratings],
            target_mean(name, pitcher=True),
            target_sd(pitcher=True),
            population=population,
        )

    stuff, control, avoidance = (column(name) for name in _PITCHER_ITEMS)
    return [
        replace(item, stuff=stuff[i], control=control[i], home_run_avoidance=avoidance[i])
        for i, item in enumerate(ratings)
    ]


def spread_ratings(
    ratings: Sequence[BatterRatings | PitcherRatings], *, population: Sequence[bool] | None = None
) -> list[BatterRatings | PitcherRatings]:
    """野手と投手が混ざった能力を、野手 / 投手ごとに散らばりを合わせる。入力と同じ順に返す。

    `population` は能力ごとの「写像を決める母集団に入れるか」の印（省くと全員。`rescale`）。
    """
    batter_places = [i for i, item in enumerate(ratings) if isinstance(item, BatterRatings)]
    pitcher_places = [i for i, item in enumerate(ratings) if isinstance(item, PitcherRatings)]
    result: list[BatterRatings | PitcherRatings] = list(ratings)
    spread_batters = spread_batter_ratings(
        [_batter(ratings[i]) for i in batter_places],
        population=None if population is None else [population[i] for i in batter_places],
    )
    spread_pitchers = spread_pitcher_ratings(
        [_pitcher(ratings[i]) for i in pitcher_places],
        population=None if population is None else [population[i] for i in pitcher_places],
    )
    for place, batter in zip(batter_places, spread_batters, strict=True):
        result[place] = batter
    for place, pitcher in zip(pitcher_places, spread_pitchers, strict=True):
        result[place] = pitcher
    return result


def _batter(item: BatterRatings | PitcherRatings) -> BatterRatings:
    assert isinstance(item, BatterRatings)
    return item


def _pitcher(item: BatterRatings | PitcherRatings) -> PitcherRatings:
    assert isinstance(item, PitcherRatings)
    return item
