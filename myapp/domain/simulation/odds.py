"""能力 → 個人の率 → 対戦の確率。

**個人の率**は `rate = 基準値 × exp(β × (能力 − 50) / 15)`。15点で exp(β) 倍になる。β は
項目ごとに `RatingSensitivity` が持ち、符号は「能力が高いほどその事象が起きやすいか」を表す
（三振に対するミートは負、本塁打に対するパワーは正）。

**対戦の確率**は odds ratio 法（log5 を二択に適用したもの）で合成する。

    odds(対戦) = odds(打者) × odds(投手) ÷ odds(リーグ)

打者の率は「平均の投手と当たったとき」、投手の率は「平均の打者と当たったとき」の値。
能力が50どうしなら3つの odds が同じ値になり、**対戦の確率は基準値と正確に一致する**
（水準の調整は基準値で、ばらつきの調整は β で、と分けられる）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache

from .baseline import NPB, LeagueBaseline
from .ratings import AVERAGE_RATING, BatterRatings, PitcherRatings

# 能力が15点違うと、率が exp(β) 倍になる
RATING_UNIT = 15.0
# 率の上下限。能力の端（1や100）でも確率が [0, 1] の内側に収まるようにする
MIN_RATE = 0.0005
MAX_RATE = 0.95


@dataclass(frozen=True)
class RatingSensitivity:
    """能力が率に効く強さ（β）。符号は「能力が高いほど事象が起きやすいか」。"""

    # 打者
    contact_strikeout: float = -0.35  # ミートが高いほど三振しにくい
    contact_in_play_hit: float = 0.12  # ミートが高いほどインプレーが安打になりやすい
    power_home_run: float = 0.55
    power_double: float = 0.20  # 長打の割合
    eye_walk: float = 0.40
    speed_triple: float = 0.50
    # 投手
    stuff_strikeout: float = 0.40
    control_walk: float = -0.35  # 制球が高いほど四球を出さない
    avoidance_home_run: float = -0.30  # 一発回避が高いほど本塁打を打たれない
    # 守備（守備側の守備位置で重みをつけた平均を使う）
    fielding_in_play_hit: float = -0.10
    fielding_error: float = -0.30


DEFAULT_SENSITIVITY = RatingSensitivity()


def scaled_rate(base: float, beta: float, rating: float) -> float:
    """基準値と能力から個人の率を作る。"""
    rate = base * math.exp(beta * (rating - AVERAGE_RATING) / RATING_UNIT)
    return min(max(rate, MIN_RATE), MAX_RATE)


def to_odds(probability: float) -> float:
    return probability / (1.0 - probability)


def from_odds(odds: float) -> float:
    return odds / (1.0 + odds)


def combine(batter: float, pitcher: float, league: float) -> float:
    """打者の率と投手の率とリーグの基準値から、対戦の確率を作る（odds ratio 法）。"""
    if league <= 0.0 or league >= 1.0:
        return league
    return from_odds(to_odds(batter) * to_odds(pitcher) / to_odds(league))


@dataclass(frozen=True)
class MatchupOdds:
    """1つの対戦の、判定の段ごとの確率。各段は前の段で落ちなかった打席に対する確率。"""

    hit_by_pitch: float
    walk: float  # 死球でなかった打席が四球になる
    strikeout: float  # 打数の打席が三振になる
    home_run: float  # 三振でなかった打数が本塁打になる
    in_play_hit: float  # インプレーが安打（本塁打を除く）になる
    double: float  # 安打が二塁打になる
    triple: float  # 二塁打でなかった安打が三塁打になる
    error: float  # 安打にならなかった打球が失策になる


def matchup(
    batter: BatterRatings,
    pitcher: PitcherRatings,
    defense: float = AVERAGE_RATING,
    baseline: LeagueBaseline = NPB,
    sensitivity: RatingSensitivity = DEFAULT_SENSITIVITY,
) -> MatchupOdds:
    """打者と投手（と守備側の守備力）の対戦の確率。

    ロスターは毎試合ほぼ同じ顔ぶれなので、同じ組み合わせの結果は覚えておく
    （守備力は0.1刻みに丸めて覚える。結果への影響は無視できる）。
    """
    return _matchup(batter, pitcher, round(defense, 1), baseline, sensitivity)


@lru_cache(maxsize=1 << 16)
def _matchup(
    batter: BatterRatings,
    pitcher: PitcherRatings,
    defense: float,
    baseline: LeagueBaseline,
    sensitivity: RatingSensitivity,
) -> MatchupOdds:
    s = sensitivity
    walk_league = baseline.walk_given_not_hit_by_pitch
    strikeout_league = baseline.strikeout_given_at_bat
    home_run_league = baseline.home_run_given_not_strikeout

    return MatchupOdds(
        hit_by_pitch=baseline.hit_by_pitch,
        walk=combine(
            scaled_rate(walk_league, s.eye_walk, batter.eye),
            scaled_rate(walk_league, s.control_walk, pitcher.control),
            walk_league,
        ),
        strikeout=combine(
            scaled_rate(strikeout_league, s.contact_strikeout, batter.contact),
            scaled_rate(strikeout_league, s.stuff_strikeout, pitcher.stuff),
            strikeout_league,
        ),
        home_run=combine(
            scaled_rate(home_run_league, s.power_home_run, batter.power),
            scaled_rate(home_run_league, s.avoidance_home_run, pitcher.home_run_avoidance),
            home_run_league,
        ),
        in_play_hit=combine(
            scaled_rate(baseline.babip, s.contact_in_play_hit, batter.contact),
            scaled_rate(baseline.babip, s.fielding_in_play_hit, defense),
            baseline.babip,
        ),
        double=scaled_rate(baseline.double_share, s.power_double, batter.power),
        triple=scaled_rate(baseline.triple_given_not_double, s.speed_triple, batter.speed),
        error=scaled_rate(baseline.error_share, s.fielding_error, defense),
    )
