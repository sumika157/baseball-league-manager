"""成績で測れない選手の能力を、分布から引く。**純粋な関数**で、DB にも Django にも触れない。

2か所が同じ引き方を通る（分布の値の出典はここだけ）:

- 自動ドラフトの新人（`draft.py`）。平均は経路ごと（`DraftRoute.rating_mean`）
- 世界の作成で、分岐元に試合の成績が無い選手＝**2軍の層**（`initial_ratings.py`）。平均は `farm_rating_mean`

どちらも水準の錨（`simulation/spread.py`）の尺度で引く（推定の尺度ではない）。項目どうしの相関と
守備位置の傾向は `simulation/samples.py` の `draw_ratings`。
"""

from __future__ import annotations

from ..simulation.randomness import GameRandom
from ..simulation.ratings import BatterRatings, PitcherRatings
from ..simulation.samples import BATTER_MEAN, PITCHER_MEAN, draw_ratings, mean_ratings
from ..value_objects import Position

# 能力の標準偏差（平均は呼び出し側が渡す）。錨の目標（`samples.BATTER_SD` 8.5 / `PITCHER_SD` 8.0）より狭い
PROSPECT_RATING_SD = 6.0
# 投手のスタミナ: 先発型の割合と、型ごとの（平均, 標準偏差）
STARTER_TYPE_RATIO = 0.55
STARTER_STAMINA = (55.0, 8.0)
RELIEVER_STAMINA = (36.0, 8.0)

# 2軍の層の平均を、錨の目標の平均（成績のある選手は散らばりの調整でこの平均に写る）から何点下げるか。
# 2軍の層は「成績のある選手の平均よりわずかに低く、散らばりが狭い（上に突き抜ける選手が少ない）層」。
# 野手 44.5 − 1 = 43.5、投手 45.0 − 1 = 44.0（#175 で水準を測った値）
FARM_GAP = 1.0


def farm_rating_mean(position: Position) -> float:
    """2軍の層の能力の平均（錨の尺度）。野手は `draw_ratings` が項目ごとのずれ（`BATTER_ITEM_OFFSET`）を足す。"""
    return (PITCHER_MEAN if position.is_pitcher else BATTER_MEAN) - FARM_GAP


def draw_prospect_ratings(
    rng: GameRandom, *, mean: float, position: Position, sd: float = PROSPECT_RATING_SD
) -> BatterRatings | PitcherRatings:
    """ひとりぶんの能力。投手は先発型か救援型かを先に決めて、スタミナをその型の分布から引く。成長型は普通。

    `sd` はドラフト会議の候補だけが広げる（会議は候補の上から取るので、指名された新人の散らばりが狭くなる分を補う）。
    """
    starter = rng.random() < STARTER_TYPE_RATIO
    stamina_mean, stamina_sd = STARTER_STAMINA if starter else RELIEVER_STAMINA
    return draw_ratings(
        rng,
        mean=mean,
        sd=sd,
        position=position,
        stamina_mean=stamina_mean,
        stamina_sd=stamina_sd,
    )


def expected_prospect_ratings(*, mean: float, position: Position) -> BatterRatings | PitcherRatings:
    """`draw_prospect_ratings` が引く能力の、項目ごとの平均（ぶれを除いた値）。戦力外が新人の見込みと比べるのに使う。

    項目のずれは `samples.mean_ratings`（`draw_ratings` と同じ出典）。スタミナは先発型と救援型の平均の加重平均
    （総合値に入らないので見込みの比べ方には効かない）。
    """
    stamina = STARTER_TYPE_RATIO * STARTER_STAMINA[0] + (1 - STARTER_TYPE_RATIO) * RELIEVER_STAMINA[0]
    return mean_ratings(mean=mean, position=position, stamina_mean=stamina)
