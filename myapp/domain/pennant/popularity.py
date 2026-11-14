"""ペナントの球団の人気（#208 E1）。**純粋な値と規則**で、DB にも Django にも触れない。

人気は球団 × 年の値（1〜100。平均50）で、その年のホーム試合の観客数に効く
（`simulation.game_day.simulated_attendance`）。
試合の集計ではなく、初期値・成績による上下・ぶれを積み重ねた**年ごとの状態**（能力の行と同じ型）なので、
年ごとの値として保存する（2026-11-14 ユーザー決定。CLAUDE.md の「集計を保存する例外」には当たらない）。

- 書くのは世界の作成（開幕年の初期値）とシーズンを締める処理（翌年の値）だけ。締めた年の値は書き換えない
- 初期値は全球団50（`initial_popularity`。収容人数や勝率は使わない。2026-11-14 ユーザー決定）。
  この機能より前に作った世界は行を持たないので、読む側が初期値で補う（補う場所は application の1か所）
- 翌年の値（`next_popularities`）は、平均への戻り＋勝率＋リーグ優勝＋日本一＋スター選手＋ぶれ。
  優勝・日本一・スター選手の項は**世界の球団の平均を引いて**から足す（毎年どこかが優勝するので、
  そのまま足すと世界全体の人気が年を追って上がり続ける）。勝率の項は 0.500 からの差なので平均はほぼ0
- 収容人数の分からない球団（本拠地が無い・未設定）は、ペナントの世界でだけ標準の収容人数
  `PENNANT_DEFAULT_CAPACITY` で観客数を作る（#44 の「根拠の無い数を作らない」をペナントでだけ緩める。
  2026-11-14 ユーザー決定。実データの側は変えない）
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from ..exceptions import InvalidPopularity
from ..simulation.randomness import game_seed, make_random, normal
from ..value_objects import Season
from .offseason_player import TrackRecord

MIN_POPULARITY = 1
MAX_POPULARITY = 100
MEAN_POPULARITY = 50

# 収容人数の分からない球団の、ペナントの世界での標準の収容人数（3万人前後。NPB の本拠地の中ほど）
PENNANT_DEFAULT_CAPACITY = 30_000

# --- 翌年の人気の式（実測は docs/design/club-finance.md の E1） ---
# 平均（50）へ戻る割合（1年あたり）。成績が平凡な年が続けば、人気は平均へ寄っていく
REVERSION = 0.2
# 勝率 0.500 からの差 1.0 あたりの点（勝率 .600 で +4 点、.400 で −4 点）
WINNING_POINTS = 40.0
# リーグ優勝・日本一の加点（世界の平均を引いてから足す）
LEAGUE_CHAMPION_POINTS = 3.0
JAPAN_CHAMPION_POINTS = 3.0
# スター選手の力 1 あたりの点（世界の平均を引いてから足す）
STAR_POINTS = 1.0
# ぶれの標準偏差（点）
NOISE_SD = 2.0

# --- スター選手（2026-10-12 ユーザー決定: 実績のあるベテラン・生え抜きのスター） ---
# タイトル1つあたりの力と、1人で数えるタイトルの上限
STAR_PER_TITLE = 0.5
STAR_MAX_TITLES = 3
# 生え抜きで長く在籍した選手（その球団での在籍年数がこれ以上）の力
STAR_HOMEGROWN_SEASONS = 8
STAR_HOMEGROWN = 0.5
# 球団のスター選手の力の上限（何人いても人気が際限なく上がらない）
STAR_CAP = 4.0


@dataclass(frozen=True, order=True)
class Popularity:
    """球団の人気（1〜100 の整数。平均50）。"""

    value: int

    def __post_init__(self) -> None:
        if isinstance(self.value, bool) or not isinstance(self.value, int):
            raise InvalidPopularity("人気は整数で指定してください。")
        if not MIN_POPULARITY <= self.value <= MAX_POPULARITY:
            raise InvalidPopularity(f"人気は{MIN_POPULARITY}〜{MAX_POPULARITY}の範囲で指定してください。")

    @classmethod
    def clamped(cls, value: float) -> Popularity:
        """実数を四捨五入して範囲に収めた人気。"""
        return cls(min(max(round(value), MIN_POPULARITY), MAX_POPULARITY))


def initial_popularity() -> Popularity:
    """世界の作成（と、この機能より前の世界の補い）での人気。全球団50（2026-11-14 ユーザー決定）。"""
    return Popularity(MEAN_POPULARITY)


@dataclass(frozen=True)
class ClubPopularity:
    """球団 × 年の人気。世界は球団のリーグで決まる。"""

    team_id: int
    year: int
    popularity: Popularity

    def __post_init__(self) -> None:
        object.__setattr__(self, "year", Season(self.year).year)


@dataclass(frozen=True)
class PopularityFactors:
    """締める年の、人気を動かす材料（球団ひとつ）。

    - `winning_percentage`: 公式戦の勝率（0〜1）
    - `league_champion`: 公式戦のリーグ優勝
    - `japan_champion`: ポストシーズンの優勝（日本一。1リーグの世界は CS の優勝）
    - `star_power`: スター選手の力（`star_power`。0〜`STAR_CAP`）
    """

    winning_percentage: float
    league_champion: bool = False
    japan_champion: bool = False
    star_power: float = 0.0


def star_power(records: Iterable[TrackRecord]) -> float:
    """球団のスター選手の力（0〜`STAR_CAP`）。いま在籍している選手の実績（`TrackRecord`）から導く。

    タイトルを取った選手（1人で `STAR_MAX_TITLES` 個まで数える）と、生え抜きで長く在籍した選手が力になる。
    """
    total = 0.0
    for record in records:
        total += STAR_PER_TITLE * min(record.titles, STAR_MAX_TITLES)
        if record.homegrown and record.seasons >= STAR_HOMEGROWN_SEASONS:
            total += STAR_HOMEGROWN
    return min(total, STAR_CAP)


def next_popularities(
    current: Mapping[int, Popularity],
    factors: Mapping[int, PopularityFactors],
    *,
    world_seed: int,
    year: int,
) -> dict[int, Popularity]:
    """締める年 `year` の人気（`current`）と材料から、翌年 `year + 1` の人気を球団ごとに導く。

    材料の無い球団（その年に試合をしていない。ふつうは無い）は、勝率 .500・優勝なし・スターなしとして扱う。
    ぶれは球団ごとに別の乱数（用途 `popularity-{球団}`）で、同じ世界・年・球団なら同じ値になる。
    """
    neutral = PopularityFactors(winning_percentage=0.5)
    resolved = {team_id: factors.get(team_id, neutral) for team_id in current}
    if not resolved:
        return {}
    # 優勝・日本一・スターは世界の平均を引いて足す（世界全体の人気が漂流しないように）
    mean_bonus = sum(_bonus(item) for item in resolved.values()) / len(resolved)
    result: dict[int, Popularity] = {}
    for team_id, popularity in current.items():
        item = resolved[team_id]
        rng = make_random(game_seed(world_seed, year, f"popularity-{team_id}"))
        value = (
            popularity.value
            + REVERSION * (MEAN_POPULARITY - popularity.value)
            + WINNING_POINTS * (item.winning_percentage - 0.5)
            + _bonus(item)
            - mean_bonus
            + normal(rng, 0.0, NOISE_SD)
        )
        result[team_id] = Popularity.clamped(value)
    return result


def _bonus(factors: PopularityFactors) -> float:
    return (
        LEAGUE_CHAMPION_POINTS * factors.league_champion
        + JAPAN_CHAMPION_POINTS * factors.japan_champion
        + STAR_POINTS * factors.star_power
    )


def with_initial(team_ids: Iterable[int], found: Mapping[int, Popularity]) -> dict[int, Popularity]:
    """球団ごとの人気。行の無い球団（この機能より前の世界）は初期値で補う。**補いの規則の唯一の出典**。"""
    return {team_id: found.get(team_id, initial_popularity()) for team_id in team_ids}
