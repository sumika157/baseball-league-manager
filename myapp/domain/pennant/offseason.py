"""シーズンを締めるときの計算。**純粋な関数**で、DB にも Django にも触れない。

    引退を決める → 球団ごとに戦力外（`release.py`。ここまでが `plan_departures`）→ 球団ごとの新人の人数と位置・
    外国人（`draft.py`）→ ドラフト会議（`draft_meeting.py`。全球団の日本人の新人。本指名の後に育成の巡）→
    残る選手の能力を1年ぶん進める（伸びしろの無い選手には先に引いて付ける。`with_filled_potential`）→
    残る選手と新人をまとめて、項目ごとに水準の錨をかける（写像は支配下で決め、
    育成にも同じ写像を当てる）

結果は `OffseasonPlan`（何を書くかの計画）。書くのは呼び出し側（application）で、ここは読み取った
材料（`OffseasonClub`）だけを見る。乱数の種は `game_seed(世界のシード, 年, 用途-選手 / 球団の id)` で、
同じ世界・同じ年なら何度計算しても同じ結果になる（選手を足し引きしても他の選手の引退は動かない）。

水準の錨は `simulation/spread.py` の `spread_ratings` をそのまま使う（世界の作成と同じ尺度。
「50 = 1軍の平均」という相対の定義を保つ）。錨の補正が大きいときは曲線（`aging.py`）が合っていない。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from math import sqrt

from ..simulation.estimate import draw_potential, with_potential
from ..simulation.randomness import game_seed, game_uniform, make_random
from ..simulation.ratings import BatterRatings, PitcherRatings
from ..simulation.spread import MIN_POPULATION, rescale, spread_ratings, target_mean, target_sd
from .aging import age_ratings
from .draft import Draftee, DraftTeam, development_target
from .draft_meeting import DraftAction, DraftContext, Lottery, run_draft
from .offseason_player import (
    OffseasonClub,
    OffseasonPlayer,
    Ratings,
    overall_value,
    player_age,
    retirement_value,
)
from .ratings import PlayerRatings
from .release import decide_releases, releases_as_chosen
from .retirement import retirement_chance


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
    # AI の戦力外（`release.py`）。引退した選手は含まない。翌年の能力を作らず、錨の母集団にも入れない
    released: tuple[int, ...]
    retained: tuple[RatingChange, ...]
    # 新人。能力は錨をかけた後の値
    draftees: tuple[Draftee, ...]
    # ドラフト会議の入札の重複と抽選の結果（D4 で記録する。いまは締めた結果の件数に使う）
    lotteries: tuple[Lottery, ...]
    # 能力が無いために翌年の能力を作らなかった選手
    without_ratings: tuple[int, ...]
    anchor: AnchorCorrection


def rating_change(before: Ratings, after: Ratings) -> float:
    """能力の変化。総合値（`overall_value`）の差。画面の「能力の変化」の出典。"""
    if isinstance(before, PitcherRatings) != isinstance(after, PitcherRatings):
        raise ValueError("野手と投手の能力は比べられません。")
    return overall_value(after) - overall_value(before)


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


# 伸びしろの無い選手に引く伸びしろの乱数の「年」。年に依らない種にして、どの年に締めても同じ値を引く
POTENTIAL_FILL_SEED_YEAR = 0


def with_filled_potential(ratings: Ratings, *, player_id: int, world_seed: int) -> Ratings:
    """伸びしろの無い能力（#308 より前に作った世界の選手）に、事前の分布から伸びしろを引いて付ける。

    締める処理が、翌年の能力を作る前に通す（その年の成長から効き、翌年の行に写る。締める年の行は書き換えない。
    2026-10-25 ユーザー決定。#308 Q4）。乱数は `potential-<世界の選手 id>` で年に依らない。
    伸びしろを持つ能力はそのまま。
    """
    if ratings.potential is not None:
        return ratings
    rng = make_random(game_seed(world_seed, POTENTIAL_FILL_SEED_YEAR, f"potential-{player_id}"))
    return with_potential(ratings, draw_potential(rng))


def _draft_team(club: OffseasonClub, remaining: Sequence[OffseasonPlayer], *, world_seed: int) -> DraftTeam:
    """引退・戦力外の後の球団。人数と位置は支配下だけで数える（育成の捕手がいても、支配下の捕手が足りなければ補う）。
    育成ドラフトの人数と位置は、別に育成だけで数える（目標は `draft.development_target`）。

    外国人の人数・背番号・苗字は育成も含める（外国人の目標人数・背番号の重複・背ネームは区分によらない）。
    """
    surnames = Counter(player.profile.back_name.split(".")[-1] for player in remaining if player.profile.back_name)
    return DraftTeam(
        team_id=club.team_id,
        position_counts=dict(Counter(player.position for player in remaining if player.is_registered)),
        foreign_count=sum(1 for player in remaining if player.profile.is_foreign_player),
        foreign_limit=club.foreign_rules.active,
        used_numbers=frozenset(player.number for player in remaining),
        surname_counts=dict(surnames),
        registered_limit=club.registered_player_limit,
        development_counts=dict(Counter(player.position for player in remaining if player.is_development)),
        development_target=development_target(club.team_id, world_seed=world_seed),
    )


def _stamina_target(clubs: Sequence[OffseasonClub]) -> tuple[float, float] | None:
    """スタミナを保つ目標（締める年の支配下の投手全員の平均と標準偏差）。人数が足りなければ None（かけない）。"""
    values = [
        player.ratings.ratings.stamina
        for club in clubs
        for player in club.players
        if player.ratings is not None
        and isinstance(player.ratings.ratings, PitcherRatings)
        and not player.is_development
    ]
    return _mean_sd(values) if len(values) >= MIN_POPULATION else None


def anchor_stamina(
    ratings: Sequence[Ratings], goal: tuple[float, float] | None, *, population: Sequence[bool] | None = None
) -> list[Ratings]:
    """投手のスタミナの平均と標準偏差を、締める年の値に保つ。

    スタミナは先発型と救援型の二つの山で、`spread_ratings` の目標（`samples.py`）の対象外。錨が無いと毎年のぶれで
    標準偏差が広がり続ける（合成の10年で 9.9 → 13.4）。一次式で写すので、順位も二つの山の形も保たれる。
    目標は初期の分布そのもの（実データから分岐した世界は、分岐時のスタミナの分布を保つ）。
    整数への丸めで標準偏差がわずかに広がる（10年で約 0.1。1世界は10シーズンまでなので許容）。
    `population` は `spread_ratings` と同じ印（写像は支配下で決め、育成にも同じ写像を当てる。省くと全員）。
    """
    result = list(ratings)
    if goal is None:
        return result
    places = [i for i, item in enumerate(result) if isinstance(item, PitcherRatings)]
    inside = None if population is None else [population[i] for i in places]
    if (len(places) if inside is None else sum(inside)) < MIN_POPULATION:
        return result
    pitchers = [item for item in result if isinstance(item, PitcherRatings)]
    moved = rescale([item.stamina for item in pitchers], goal[0], goal[1], population=inside)
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


@dataclass(frozen=True)
class Departures:
    """引退と戦力外の後の球団（`plan_departures` の結果）。ドラフトの人数はこの後の人数で数える。"""

    retired: frozenset[int]
    # 戦力外の選手（球団ごとに外す順で、球団 id の順につなげたもの）
    released: tuple[int, ...]
    # 残る選手（球団 id の順・選手の並びの順）
    survivors: tuple[OffseasonPlayer, ...]
    # 引退・戦力外の後の球団（球団 id の昇順）
    draft_teams: tuple[DraftTeam, ...]


def plan_departures(
    clubs: Sequence[OffseasonClub],
    *,
    year: int,
    world_seed: int,
    start_year: int,
    manual_releases: Mapping[int, Collection[int]] | None = None,
) -> Departures:
    """Y 年を締めるときの引退と戦力外（球団 id の昇順）。ドラフト会議の人数の幅（D3）も同じ関数で数える。

    `manual_releases`: GM が手動で戦力外を指名した球団（球団 id → 選手 id）。無い球団は自動（`release.py`）。
    """
    ordered = sorted(clubs, key=lambda club: club.team_id)
    retired = set(decide_retirements(ordered, year=year, world_seed=world_seed, start_year=start_year))
    survivors: list[OffseasonPlayer] = []
    released: list[int] = []
    teams: list[DraftTeam] = []
    for club in ordered:
        remaining = [player for player in club.players if player.player_id not in retired]
        # 戦力外は引退の後・ドラフトの前。ドラフトは戦力外の後の人数で数える。手動の球団は指名どおり
        chosen = (manual_releases or {}).get(club.team_id)
        leaving = set(
            decide_releases(club, remaining, year=year, start_year=start_year)
            if chosen is None
            else releases_as_chosen(chosen, remaining)
        )
        released.extend(sorted(leaving))
        remaining = [player for player in remaining if player.player_id not in leaving]
        survivors.extend(remaining)
        teams.append(_draft_team(club, remaining, world_seed=world_seed))
    return Departures(
        retired=frozenset(retired), released=tuple(released), survivors=tuple(survivors), draft_teams=tuple(teams)
    )


def plan_offseason(
    clubs: Sequence[OffseasonClub],
    *,
    year: int,
    world_seed: int,
    start_year: int,
    used_names: Collection[str],
    draft: DraftContext,
    manual_releases: Mapping[int, Collection[int]] | None = None,
    draft_actions: Sequence[DraftAction] = (),
) -> OffseasonPlan:
    """Y 年を締める計画。`clubs` は球団 id の昇順で処理する。

    - `used_names`: 世界の全選手（引退した選手を含む）の名前。新人の名前が重ならないようにする
    - `start_year`: 世界の開幕年（生年月日の無い選手の年齢の推定に使う）
    - `draft`: ドラフト会議の設定（世界の規則と、Y 年の公式戦の最終順位）。既定値は持たない
    - `manual_releases`: GM が手動で戦力外を指名した球団（球団 id → 選手 id）。無い球団は自動（`release.py`）
    - `draft_actions`: 保存した会議の行動（D3）。残りは AI が最後まで進める
    """
    departures = plan_departures(
        clubs, year=year, world_seed=world_seed, start_year=start_year, manual_releases=manual_releases
    )
    retired = departures.retired
    survivors = departures.survivors
    result = run_draft(
        departures.draft_teams,
        draft,
        year=year,
        world_seed=world_seed,
        used_names=set(used_names),
        actions=draft_actions,
    )
    drafted = list(result.draftees)
    ordered = sorted(clubs, key=lambda club: club.team_id)

    aged: list[tuple[int, PlayerRatings, Ratings]] = []
    # 錨の写像を決める母集団に入れるか（支配下。`aged` と同じ並び）
    aged_inside: list[bool] = []
    without_ratings: list[int] = []
    for player in survivors:
        if player.ratings is None:
            without_ratings.append(player.player_id)
            continue
        rng = make_random(game_seed(world_seed, year + 1, f"aging-{player.player_id}"))
        age = player_age(player.profile, year, start_year=start_year)
        current = with_filled_potential(player.ratings.ratings, player_id=player.player_id, world_seed=world_seed)
        aged.append((player.player_id, player.ratings, age_ratings(current, age=age, rng=rng)))
        aged_inside.append(not player.is_development)

    # 残る選手と新人をまとめて、項目ごとに水準の錨をかける（スタミナは別に、締める年の分布を保つ）。
    # 写像は**支配下**（残る支配下と、本指名・外国人の新人）で決め、育成（残る育成と育成の新人）にも同じ写像を当てる
    # （毎年の育成の流入で1軍の水準が動かないように。2026-10-12 ユーザー決定。#198 D2）
    population: list[Ratings] = [ratings for _, _, ratings in aged] + [draftee.ratings for draftee in drafted]
    inside = aged_inside + [not draftee.is_development for draftee in drafted]
    anchored = anchor_stamina(
        spread_ratings(population, population=inside), _stamina_target(ordered), population=inside
    )
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
        released=tuple(sorted(departures.released)),
        retained=retained,
        draftees=draftees,
        lotteries=result.lotteries,
        without_ratings=tuple(without_ratings),
        anchor=anchor_correction([ratings for ratings, counted in zip(population, inside, strict=True) if counted]),
    )
