"""調整・確認・テスト用の球団を作る。DB を使わず、乱数だけで選手と能力を作る。

- `average_club`: 全員の能力が50の球団。基準値の調整（水準の表）に使う。
- `spread_league`: 能力を散らした球団の集まり。ばらつき（首位打者・本塁打王などの分布）の調整に使う。

本番の世界の作成（実成績からの能力の推定）はこれとは別の段階の仕事。ここは調整の道具。
"""

from __future__ import annotations

from collections.abc import Sequence

from ..value_objects import Position
from .manager import ClubRoster, SimBatter, SimPitcher
from .randomness import GameRandom, normal
from .ratings import RATING_MAX, RATING_MIN, BatterRatings, GrowthType, PitcherRatings

# 1球団の登録候補。1軍29人（野手15・投手14）に、2軍の控えを足した人数
_BATTER_POSITIONS = (
    [Position.CATCHER] * 3 + [Position.INFIELDER] * 8 + [Position.OUTFIELDER] * 7 + [Position.DESIGNATED_HITTER] * 2
)
_PITCHER_COUNT = 19
# 球団の中の選手の番号は、球団 id × 100 + 番号（野手は 1〜、投手は 51〜）
_PITCHER_OFFSET = 50

# 能力の分布（平均・標準偏差）。**調整（P1）で決めた分布の唯一の出典**。タイトルの水準（首位打者・本塁打王など）が
# 目標帯に入るよう調整してあり、実成績から推定した能力の散らばりもこれに合わせる（`spread.py`）。
# 平均が 50 でなく 50 を下回るのは、球団の全員（2軍の控えを含む）の平均で、1軍の平均が 50 だから。
BATTER_MEAN = 44.5
PITCHER_MEAN = 45.0
BATTER_SD = 8.5
PITCHER_SD = 8.0
# 野手の項目ごとの平均のずれ（打撃の3項目だけ。走力は 0）
BATTER_ITEM_OFFSET = {"contact": 1.0, "power": -1.0, "eye": -1.0, "speed": 0.0}


def average_club(team_id: int, name: str | None = None, rating: int = 50) -> ClubRoster:
    """全員の能力が同じ球団（既定は50）。"""
    club_name = name or f"平均{team_id}"
    batters = tuple(
        SimBatter(
            player_id=team_id * 100 + number,
            name=f"{club_name}野手{number}",
            position=position,
            ratings=BatterRatings(rating, rating, rating, rating, rating),
        )
        for number, position in enumerate(_BATTER_POSITIONS, start=1)
    )
    pitchers = tuple(
        SimPitcher(
            player_id=team_id * 100 + _PITCHER_OFFSET + number,
            name=f"{club_name}投手{number}",
            ratings=PitcherRatings(rating, rating, rating, rating),
        )
        for number in range(1, _PITCHER_COUNT + 1)
    )
    return ClubRoster(team_id=team_id, name=club_name, batters=batters, pitchers=pitchers)


def _rating(rng: GameRandom, mean: float, sd: float, shared: float, shared_weight: float) -> int:
    """共通の素質（`shared`）に重みをつけて混ぜた正規分布から、1〜100 の整数を引く。"""
    independent = normal(rng)
    value = mean + sd * (shared_weight * shared + (1.0 - shared_weight**2) ** 0.5 * independent)
    return min(RATING_MAX, max(RATING_MIN, round(value)))


def spread_club(
    rng: GameRandom,
    team_id: int,
    name: str,
    *,
    batter_mean: float = BATTER_MEAN,
    pitcher_mean: float = PITCHER_MEAN,
    sd: float = BATTER_SD,
    pitcher_sd: float = PITCHER_SD,
) -> ClubRoster:
    """能力を散らした球団。野手・投手とも平均 `*_mean`、標準偏差 `sd` で、項目どうしは弱く相関する。

    先発向きの投手（スタミナが高い）と救援向きを分けて作る。
    """
    shared_weight = 0.5
    batters = []
    for number, position in enumerate(_BATTER_POSITIONS, start=1):
        talent = normal(rng)
        # 守備位置による傾向: 捕手・遊撃は守備が高め、指名打者は打撃が高めで守備が低い
        field_bias = {Position.CATCHER: 4.0, Position.INFIELDER: 2.0, Position.OUTFIELDER: 0.0}.get(position, -8.0)
        bat_bias = 3.0 if position is Position.DESIGNATED_HITTER else 0.0
        ratings = BatterRatings(
            contact=_rating(rng, batter_mean + bat_bias + BATTER_ITEM_OFFSET["contact"], sd, talent, shared_weight),
            power=_rating(rng, batter_mean + bat_bias + BATTER_ITEM_OFFSET["power"], sd, talent, shared_weight),
            eye=_rating(rng, batter_mean + BATTER_ITEM_OFFSET["eye"], sd, talent, shared_weight),
            speed=_rating(rng, batter_mean + BATTER_ITEM_OFFSET["speed"], sd, normal(rng), 0.0),
            fielding=_rating(rng, batter_mean + field_bias, sd, normal(rng), 0.0),
            growth=GrowthType.NORMAL,
        )
        batters.append(
            SimBatter(
                player_id=team_id * 100 + number,
                name=f"{name}野手{number}",
                position=position,
                ratings=ratings,
            )
        )

    pitchers = []
    for number in range(1, _PITCHER_COUNT + 1):
        talent = normal(rng)
        starter_type = number <= 8
        pitchers.append(
            SimPitcher(
                player_id=team_id * 100 + _PITCHER_OFFSET + number,
                name=f"{name}投手{number}",
                ratings=PitcherRatings(
                    stuff=_rating(rng, pitcher_mean, pitcher_sd, talent, shared_weight),
                    control=_rating(rng, pitcher_mean, pitcher_sd, talent, shared_weight),
                    home_run_avoidance=_rating(rng, pitcher_mean, pitcher_sd, talent, shared_weight),
                    stamina=_rating(rng, 62.0 if starter_type else 38.0, 10.0, normal(rng), 0.0),
                ),
            )
        )
    return ClubRoster(team_id=team_id, name=name, batters=tuple(batters), pitchers=tuple(pitchers))


def spread_league(rng: GameRandom, team_count: int, **kwargs: float) -> Sequence[ClubRoster]:
    """能力を散らした球団を `team_count` 作る（球団 id は 1 から）。"""
    return [spread_club(rng, team_id, f"球団{team_id}", **kwargs) for team_id in range(1, team_count + 1)]


def round_robin_days(team_ids: Sequence[int], repeats: int) -> list[list[tuple[int, int]]]:
    """総当たりを `repeats` 周する日程。1日に全チームが1試合ずつ（チーム数は偶数）。

    円盤法（1チームを固定して残りを回す）で組む。(ホーム, ビジター) を返し、ホームとビジターは
    周ごとに入れ替える。12チームで13周なら、1チーム143試合・全体で858試合（NPB の規模）。
    これは調整用の簡易な日程で、本番の日程（交流戦・休養日）は `generate_schedule` の仕事。
    """
    teams = list(team_ids)
    if len(teams) % 2:
        raise ValueError("チーム数は偶数にしてください。")
    days: list[list[tuple[int, int]]] = []
    for repeat in range(repeats):
        rotating = teams[1:]
        for round_index in range(len(teams) - 1):
            order = [teams[0], *rotating]
            day = []
            for slot in range(len(teams) // 2):
                first, second = order[slot], order[len(teams) - 1 - slot]
                if (round_index + slot + repeat) % 2:
                    first, second = second, first
                day.append((first, second))
            days.append(day)
            rotating = rotating[-1:] + rotating[:-1]
    return days
