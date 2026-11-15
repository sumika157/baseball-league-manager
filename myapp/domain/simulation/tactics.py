"""采配の傾向（`Tactics`）。AI 監督（`manager.py`）とエンジン（`engine.py`）の規則の重みをずらす（#210 M1）。

**規則は書き分けない。** 傾向は今の関数が受け取って定数をずらすだけで、判断の形（いつ代えるか・誰を出すか）は
全球団で同じ。傾向は4軸で、どれも −2〜+2 の5段階（`TacticLevel`）。**優劣ではなく傾向**で、どの段階が
勝ちやすいということは作らない（2026-11-15 ユーザー決定。差は年 ±1〜3勝の目安に収める）。

| 軸 | ずらすもの | 1段あたり |
| --- | --- | --- |
| 先発の引っ張り（`starter_leash`） | 先発が受け持つ打者数の目安の平均 | `STARTER_BATTERS_PER_LEVEL` 人 |
| 代打（`pinch_hitting`） | 代打に出す控えが上回るべき打撃の差（「明確に上」） | −`PINCH_HITTER_MARGIN_PER_LEVEL` |
| 盗塁（`base_stealing`） | 盗塁を試みる確率の odds | × exp(`STEAL_ODDS_PER_LEVEL`) |
| 犠打（`bunting`） | 犠打を選ぶ確率の odds | × exp(`BUNT_ODDS_PER_LEVEL`) |

**乱数を引く回数は変えない。** 先発の目安は `normal()` を1回、盗塁の企図と犠打は `random()` を1回引いて率と比べ、
代打は乱数を引かない。平均と率をずらすだけなので、どの傾向でも1打席あたりの乱数の消費は同じ。
**標準（`Tactics.STANDARD`。4軸とも0）なら、傾向を足す前と1打席も違わない**（段階0は値をそのまま返す）。
段階ごとの表記（「先発を引っ張る」など）の唯一の出典は `TacticAxis.label`。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, fields
from enum import Enum, IntEnum
from typing import ClassVar

from .odds import from_odds, to_odds

# 段階 → 重みの係数（実測は Wiki「ペナントモードの仕組み」の「実測値」の「首脳陣」）
STARTER_BATTERS_PER_LEVEL = 1.5
PINCH_HITTER_MARGIN_PER_LEVEL = 1.5
STEAL_ODDS_PER_LEVEL = 0.25
BUNT_ODDS_PER_LEVEL = 0.3


class TacticLevel(IntEnum):
    """傾向の段階（−2〜+2）。0 が標準。正の向きは「その采配を多く・長く」。"""

    MUCH_LESS = -2
    LESS = -1
    STANDARD = 0
    MORE = 1
    MUCH_MORE = 2


class TacticAxis(Enum):
    """傾向の軸。値は `Tactics` のフィールド名。段階ごとの表記はここが唯一の出典。"""

    STARTER_LEASH = "starter_leash"
    PINCH_HITTING = "pinch_hitting"
    BASE_STEALING = "base_stealing"
    BUNTING = "bunting"

    @property
    def title(self) -> str:
        """軸の名前（画面の見出し）。"""
        return _TITLES[self]

    def label(self, level: TacticLevel) -> str:
        """その段階の表記（−2 から +2 の順に5つ）。"""
        return _LABELS[self][level + 2]


_TITLES = {
    TacticAxis.STARTER_LEASH: "継投",
    TacticAxis.PINCH_HITTING: "代打",
    TacticAxis.BASE_STEALING: "盗塁",
    TacticAxis.BUNTING: "犠打",
}

_LABELS = {
    TacticAxis.STARTER_LEASH: ("継投が早い", "継投がやや早い", "標準", "先発をやや引っ張る", "先発を引っ張る"),
    TacticAxis.PINCH_HITTING: ("代打は少ない", "代打はやや少ない", "標準", "代打がやや多い", "代打が多い"),
    TacticAxis.BASE_STEALING: ("盗塁は少ない", "盗塁はやや少ない", "標準", "盗塁がやや多い", "盗塁が多い"),
    TacticAxis.BUNTING: ("犠打は少ない", "犠打はやや少ない", "標準", "犠打がやや多い", "犠打が多い"),
}


def _scaled_odds(probability: float, level: TacticLevel, per_level: float) -> float:
    """確率の odds を exp(per_level × 段階) 倍する。段階0は値をそのまま返す（標準で今と1打席も違わない）。"""
    if level == TacticLevel.STANDARD or probability <= 0.0 or probability >= 1.0:
        return probability
    return from_odds(to_odds(probability) * math.exp(per_level * level))


@dataclass(frozen=True)
class Tactics:
    """1球団の采配の傾向（4軸）。試合の中の判断の重みで、GM の編成の上書き（`ClubOrders`）とは別の引数。"""

    starter_leash: TacticLevel = TacticLevel.STANDARD
    pinch_hitting: TacticLevel = TacticLevel.STANDARD
    base_stealing: TacticLevel = TacticLevel.STANDARD
    bunting: TacticLevel = TacticLevel.STANDARD

    # 4軸とも標準。監督のいない球団・実データの確認用の道具はこれで回す
    STANDARD: ClassVar[Tactics]

    def __post_init__(self) -> None:
        # 整数で渡されても段階にそろえる（範囲の外は ValueError）
        for item in fields(self):
            object.__setattr__(self, item.name, TacticLevel(getattr(self, item.name)))

    def level(self, axis: TacticAxis) -> TacticLevel:
        level: TacticLevel = getattr(self, axis.value)
        return level

    def starter_batters_shift(self) -> float:
        """先発が受け持つ打者数の目安の平均に足す人数。"""
        return STARTER_BATTERS_PER_LEVEL * self.starter_leash

    def pinch_hitter_margin(self, standard: float) -> float:
        """代打に出す控えが上回るべき打撃の差。段階が高いほど小さく（代打を出しやすく）する。"""
        return standard - PINCH_HITTER_MARGIN_PER_LEVEL * self.pinch_hitting

    def steal_attempt_rate(self, probability: float) -> float:
        """盗塁を試みる確率（走者の走力で決めた後の値をずらす）。"""
        return _scaled_odds(probability, self.base_stealing, STEAL_ODDS_PER_LEVEL)

    def bunt_rate(self, probability: float) -> float:
        """犠打を選ぶ確率。"""
        return _scaled_odds(probability, self.bunting, BUNT_ODDS_PER_LEVEL)


Tactics.STANDARD = Tactics()
