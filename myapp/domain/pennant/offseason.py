"""シーズンを締めるときの計算。**純粋な関数**で、DB にも Django にも触れない。

    引退を決める → 球団ごとの新人を決める → 残る選手の能力を1年ぶん進める
    → 残る選手と新人をまとめて、項目ごとに水準の錨をかける

結果は `OffseasonPlan`（何を書くかの計画）。書くのは呼び出し側（application）で、ここは読み取った
材料（`OffseasonClub`）だけを見る。乱数の種は `game_seed(世界のシード, 年, 用途-選手 / 球団の id)` で、
同じ世界・同じ年なら何度計算しても同じ結果になる（選手を足し引きしても他の選手の引退は動かない）。

水準の錨は `simulation/spread.py` の `spread_ratings` をそのまま使う（世界の作成と同じ尺度。
「50 = 1軍の平均」という相対の定義を保つ）。錨の補正が大きいときは曲線（`aging.py`）が合っていない。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Sequence
from dataclasses import dataclass, replace
from math import sqrt

from ..simulation.randomness import game_seed, game_uniform, make_random
from ..simulation.ratings import BatterRatings, PitcherRatings
from ..simulation.spread import MIN_POPULATION, rescale, spread_ratings, target_mean, target_sd
from ..value_objects import Position, Profile
from .aging import age_ratings
from .draft import Draftee, DraftTeam, draft_class
from .fork import ESTIMATED_AGE_AT_START, with_birth_date
from .initial_ratings import age_at_season_start
from .ratings import PlayerRatings
from .retirement import PlayingTime, retirement_chance

# 能力を持たない選手の総合値（引退の確率に効かない値）
_NEUTRAL_VALUE = 45.0

Ratings = BatterRatings | PitcherRatings


@dataclass(frozen=True)
class OffseasonPlayer:
    """締める年 Y の現役の選手。"""

    player_id: int
    position: Position
    profile: Profile
    number: int
    # 現在の在籍の開始年（分岐した選手は一律に開幕年。入団年は entered_year）
    joined_year: int
    # Y の能力。無い選手は翌年の能力も作らない
    ratings: PlayerRatings | None
    playing_time: PlayingTime = PlayingTime()

    @property
    def entered_year(self) -> int:
        """プロ入りの年。入団年（`debut_year`）があればそれ、無ければ現在の在籍の開始年。

        分岐した選手の在籍の開始年は一律に開幕年になる。若手の保護（入団2年以内）を在籍の開始年で数えると、
        実際の経歴にかかわらず最初の2シーズン保護されてしまうので、入団年を優先する。
        """
        return self.profile.debut_year if self.profile.debut_year is not None else self.joined_year

    def seasons_as_pro(self, year: int) -> int:
        """`year` を含めてプロにいた年数（1以上）。若手の保護の判定に使う。"""
        return max(1, year - self.entered_year + 1)


@dataclass(frozen=True)
class OffseasonClub:
    """締める年 Y の球団。"""

    team_id: int
    # リーグの外国人の登録枠。None は無制限
    foreign_roster_limit: int | None
    players: tuple[OffseasonPlayer, ...]


@dataclass(frozen=True)
class RatingChange:
    """残る選手の、締める年と翌年の能力。"""

    player_id: int
    before: PlayerRatings
    after: PlayerRatings

    @property
    def delta(self) -> float:
        return rating_change(self.before.ratings, self.after.ratings)


@dataclass(frozen=True)
class AnchorCorrection:
    """錨の前の分布の、目標からのずれ。項目ごとの平均と標準偏差のずれ（点）の絶対値の最大。

    大きいなら年齢曲線が合っていない（錨で歪みを隠さない）。母集団のテストで確かめる。
    """

    mean_shift: float = 0.0
    sd_shift: float = 0.0
    # 平均のずれが最大の項目（野手 / 投手の名前付き）と、その向き付きのずれ（+ は目標より高かった）
    mean_item: str = ""
    mean_signed: float = 0.0


@dataclass(frozen=True)
class OffseasonPlan:
    """締める処理が書く内容。"""

    year: int
    retired: tuple[int, ...]
    retained: tuple[RatingChange, ...]
    # 新人。能力は錨をかけた後の値
    draftees: tuple[Draftee, ...]
    # 能力が無いために翌年の能力を作らなかった選手
    without_ratings: tuple[int, ...]
    anchor: AnchorCorrection


def overall_value(ratings: Ratings) -> float:
    """能力の総合値。野手は打撃（`batting_value`）、投手は抑える力（`pitching_value`）。画面の「総合」の出典。"""
    if isinstance(ratings, PitcherRatings):
        return ratings.pitching_value
    return ratings.batting_value


def rating_change(before: Ratings, after: Ratings) -> float:
    """能力の変化。総合値（`overall_value`）の差。画面の「能力の変化」の出典。"""
    if isinstance(before, PitcherRatings) != isinstance(after, PitcherRatings):
        raise ValueError("野手と投手の能力は比べられません。")
    return overall_value(after) - overall_value(before)


def retirement_value(position: Position, ratings: Ratings | None) -> float:
    """引退の確率に使う総合値。野手は打撃、投手は抑える力。捕手は守備力と打撃の大きい方。"""
    if ratings is None:
        return _NEUTRAL_VALUE
    if isinstance(ratings, BatterRatings) and position is Position.CATCHER:
        return max(float(ratings.fielding), ratings.batting_value)
    return overall_value(ratings)


def player_age(profile: Profile, year: int, *, start_year: int) -> int:
    """締める年 Y の4月1日時点の満年齢。生年月日が空なら推定で補って数える。"""
    age = age_at_season_start(with_birth_date(profile, start_year), year)
    return ESTIMATED_AGE_AT_START if age is None else age


def decide_retirements(
    clubs: Sequence[OffseasonClub], *, year: int, world_seed: int, start_year: int
) -> tuple[int, ...]:
    """引退（外国人は退団）する選手の id。球団の並び・選手の並びの順。"""
    retired = []
    for club in clubs:
        for player in club.players:
            chance = retirement_chance(
                age=player_age(player.profile, year, start_year=start_year),
                value=retirement_value(player.position, None if player.ratings is None else player.ratings.ratings),
                playing_time=player.playing_time,
                is_foreign=player.profile.is_foreign_player,
                seasons_as_pro=player.seasons_as_pro(year),
            )
            if game_uniform(world_seed, year, f"retire-{player.player_id}") < chance:
                retired.append(player.player_id)
    return tuple(retired)


def _draft_team(club: OffseasonClub, remaining: Sequence[OffseasonPlayer]) -> DraftTeam:
    surnames = Counter(player.profile.back_name.split(".")[-1] for player in remaining if player.profile.back_name)
    return DraftTeam(
        team_id=club.team_id,
        position_counts=dict(Counter(player.position for player in remaining)),
        foreign_count=sum(1 for player in remaining if player.profile.is_foreign_player),
        foreign_limit=club.foreign_roster_limit,
        used_numbers=frozenset(player.number for player in remaining),
        surname_counts=dict(surnames),
    )


def _stamina_target(clubs: Sequence[OffseasonClub]) -> tuple[float, float] | None:
    """スタミナを保つ目標（締める年の投手全員の平均と標準偏差）。人数が足りなければ None（かけない）。"""
    values = [
        player.ratings.ratings.stamina
        for club in clubs
        for player in club.players
        if player.ratings is not None and isinstance(player.ratings.ratings, PitcherRatings)
    ]
    return _mean_sd(values) if len(values) >= MIN_POPULATION else None


def anchor_stamina(ratings: Sequence[Ratings], goal: tuple[float, float] | None) -> list[Ratings]:
    """投手のスタミナの平均と標準偏差を、締める年の値に保つ。

    スタミナは先発型と救援型の二つの山で、`spread_ratings` の目標（`samples.py`）の対象外。錨が無いと毎年のぶれで
    標準偏差が広がり続ける（合成の10年で 9.9 → 13.4）。一次式で写すので、順位も二つの山の形も保たれる。
    目標は初期の分布そのもの（実データから分岐した世界は、分岐時のスタミナの分布を保つ）。
    整数への丸めで標準偏差がわずかに広がる（10年で約 0.1。1世界は10シーズンまでなので許容）。
    """
    result = list(ratings)
    if goal is None:
        return result
    places = [i for i, item in enumerate(result) if isinstance(item, PitcherRatings)]
    if len(places) < MIN_POPULATION:
        return result
    pitchers = [item for item in result if isinstance(item, PitcherRatings)]
    moved = rescale([item.stamina for item in pitchers], goal[0], goal[1])
    for place, item, stamina in zip(places, pitchers, moved, strict=True):
        result[place] = replace(item, stamina=stamina)
    return result


def _mean_sd(values: Sequence[float]) -> tuple[float, float]:
    """平均と標準偏差（母集団）。1,600 人 × 8 項目 × 毎年なので、厳密な分数計算（statistics）は使わない。"""
    if not values:
        return 0.0, 0.0
    mean = sum(values) / len(values)
    return mean, sqrt(sum((value - mean) ** 2 for value in values) / len(values))


def anchor_correction(before: Sequence[Ratings]) -> AnchorCorrection:
    """錨の前の母集団が、目標の分布からどれだけ離れていたか（項目ごとの平均と標準偏差のずれの最大。点）。

    錨の前後の平均の差は見ない（整数に丸めるので、半端な補正が 0 か 1 に量子化されてしまう）。
    スタミナは錨の対象外、人数が足りない区分は錨がかからないので、見ない。
    """
    mean_shift = sd_shift = signed = 0.0
    mean_item = ""
    for kind, pitcher in ((BatterRatings, False), (PitcherRatings, True)):
        group = [item for item in before if isinstance(item, kind)]
        if len(group) < MIN_POPULATION:
            continue
        for name in kind.LABELS:
            if name == "stamina":
                continue
            mean, sd = _mean_sd([getattr(item, name) for item in group])
            offset = mean - target_mean(name, pitcher=pitcher)
            if abs(offset) > mean_shift:
                mean_shift, signed = abs(offset), offset
                mean_item = f"{'投手' if pitcher else '野手'}の{kind.LABELS[name]}"
            sd_shift = max(sd_shift, abs(sd - target_sd(pitcher=pitcher)))
    return AnchorCorrection(mean_shift, sd_shift, mean_item, signed)


def plan_offseason(
    clubs: Sequence[OffseasonClub],
    *,
    year: int,
    world_seed: int,
    start_year: int,
    used_names: Collection[str],
) -> OffseasonPlan:
    """Y 年を締める計画。`clubs` は球団 id の昇順で処理する。

    - `used_names`: 世界の全選手（引退した選手を含む）の名前。新人の名前が重ならないようにする
    - `start_year`: 世界の開幕年（生年月日の無い選手の年齢の推定に使う）
    """
    ordered = sorted(clubs, key=lambda club: club.team_id)
    retired = set(decide_retirements(ordered, year=year, world_seed=world_seed, start_year=start_year))

    names = set(used_names)
    drafted: list[Draftee] = []
    survivors: list[OffseasonPlayer] = []
    for club in ordered:
        remaining = [player for player in club.players if player.player_id not in retired]
        survivors.extend(remaining)
        rng = make_random(game_seed(world_seed, year + 1, f"draft-{club.team_id}"))
        drafted.extend(draft_class(rng, _draft_team(club, remaining), year=year, used_names=names))

    aged: list[tuple[int, PlayerRatings, Ratings]] = []
    without_ratings: list[int] = []
    for player in survivors:
        if player.ratings is None:
            without_ratings.append(player.player_id)
            continue
        rng = make_random(game_seed(world_seed, year + 1, f"aging-{player.player_id}"))
        age = player_age(player.profile, year, start_year=start_year)
        aged.append((player.player_id, player.ratings, age_ratings(player.ratings.ratings, age=age, rng=rng)))

    # 残る選手と新人をまとめて、項目ごとに水準の錨をかける（スタミナは別に、締める年の分布を保つ）
    population: list[Ratings] = [ratings for _, _, ratings in aged] + [draftee.ratings for draftee in drafted]
    anchored = anchor_stamina(spread_ratings(population), _stamina_target(ordered))
    kept = anchored[: len(aged)]
    retained = tuple(
        RatingChange(
            player_id=player_id,
            before=before,
            after=PlayerRatings(player_id=player_id, year=year + 1, ratings=ratings),
        )
        for (player_id, before, _), ratings in zip(aged, kept, strict=True)
    )
    draftees = tuple(
        replace(draftee, ratings=ratings) for draftee, ratings in zip(drafted, anchored[len(aged) :], strict=True)
    )
    return OffseasonPlan(
        year=year,
        retired=tuple(sorted(retired)),
        retained=retained,
        draftees=draftees,
        without_ratings=tuple(without_ratings),
        anchor=anchor_correction(population),
    )
