"""シーズンを締めるときの計算。**純粋な関数**で、DB にも Django にも触れない。

    引退を決める → 球団ごとに育成の昇格の希望 → 戦力外（`release.py`。昇格の希望の分も空ける）→ 昇格の確定と
    育成契約の満了（`development.py`。#170。ここまでが `plan_departures`）→ 外国人の獲得
    （`foreign_market.py`。補う人数は `draft.py`、選ぶ位置は戦力の穴）→ ドラフト会議の人数の幅と候補（ここまでが
    `prepare_meeting`。会議を始めるとき（#297 D3）も同じ関数）→ ドラフト会議（`draft_meeting.py`。全球団の
    日本人の新人。人数は新人の人数 − 獲得した外国人。本指名の後に育成の巡。GM が進めて終えた会議は保存した行動を
    再生する）→
    残る選手の能力を1年ぶん進める（伸びしろの無い選手には先に引いて付ける。`with_filled_potential`）→
    残る選手・新人・獲得した外国人をまとめて、項目ごとに水準の錨をかける（写像は支配下で決め、
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

from ..exceptions import DraftInProgress
from ..simulation.randomness import game_seed, game_uniform, make_random
from ..simulation.ratings import BatterRatings, PitcherRatings
from ..simulation.spread import MIN_POPULATION, rescale, spread_ratings, target_mean, target_sd
from ..value_objects import ContractStatus
from .aging import age_ratings
from .development import (
    DevelopmentDecision,
    Promotion,
    confirm_chosen_promotions,
    confirm_promotions,
    decide_expirations,
    decide_promotions,
    decisions_as_chosen,
)
from .draft import Draftee, DraftTeam, development_target
from .draft_meeting import DraftContext, DraftState, Lottery, MeetingSetup, finish_draft, open_meeting
from .draft_record import DraftRecord
from .foreign_market import ForeignSigning, OffseasonSignings, offseason_signings
from .offseason_player import (
    OffseasonClub,
    OffseasonPlayer,
    Ratings,
    overall_value,
    player_age,
)
from .outlook import with_filled_potential
from .ratings import PlayerRatings
from .release import ability_value, decide_releases, releases_as_chosen
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
    # 育成から支配下への昇格（`development.py`。#170）。翌年から支配下で、背番号は昇格後の番号。
    # 残る選手（`retained`）にも入る
    promotions: tuple[Promotion, ...]
    # 育成契約の満了で自由契約にした育成の選手（`development.py`。退団の経路は戦力外）。
    # 戦力外（`released`）とは別に持つ
    expired: tuple[int, ...]
    # 新人（ドラフトの日本人）。能力は錨をかけた後の値
    draftees: tuple[Draftee, ...]
    # 獲得した外国人（`foreign_market.py`）。能力は錨をかけた後の値。入団は翌年で、経路は新外国人
    signings: tuple[ForeignSigning, ...]
    # ドラフト会議の入札の重複と抽選の結果（D4 で記録する。いまは締めた結果の件数に使う）
    lotteries: tuple[Lottery, ...]
    # 能力が無いために翌年の能力を作らなかった選手
    without_ratings: tuple[int, ...]
    anchor: AnchorCorrection

    @property
    def newcomers(self) -> tuple[ForeignSigning | Draftee, ...]:
        """翌年に加入する選手（獲得した外国人、続けて新人）。"""
        return (*self.signings, *self.draftees)


def rating_change(before: Ratings, after: Ratings) -> float:
    """能力の変化。総合値（`overall_value`）の差。画面の「能力の変化」の出典。"""
    if isinstance(before, PitcherRatings) != isinstance(after, PitcherRatings):
        raise ValueError("野手と投手の能力は比べられません。")
    return overall_value(after) - overall_value(before)


def decide_retirements(
    clubs: Sequence[OffseasonClub], *, year: int, world_seed: int, start_year: int
) -> tuple[int, ...]:
    """引退（外国人は退団）する選手の id。球団の並び・選手の並びの順。

    能力は戦力外と同じ「所属球団のスカウトの見立てで見た見込みの能力の総合値」（`release.ability_value`。#308 P3b）。
    `PEAK_AGE` 歳以上は今の能力の総合値で、#308 より前と同じ確率。
    """
    retired = []
    for club in clubs:
        for player in club.players:
            chance = retirement_chance(
                age=player_age(player.profile, year, start_year=start_year),
                value=ability_value(
                    player, team_id=club.team_id, year=year, start_year=start_year, world_seed=world_seed
                ),
                playing_time=player.playing_time,
                is_foreign=player.profile.is_foreign_player,
                seasons_as_pro=player.seasons_as_pro(year),
            )
            if game_uniform(world_seed, year, f"retire-{player.player_id}") < chance:
                retired.append(player.player_id)
    return tuple(retired)


def _draft_team(club: OffseasonClub, remaining: Sequence[OffseasonPlayer], *, world_seed: int) -> DraftTeam:
    """引退・戦力外・昇格・満了の後の球団（昇格した選手は支配下・昇格後の背番号で数える）。
    人数と位置は支配下だけで数える（育成の捕手がいても、支配下の捕手が足りなければ補う）。
    育成ドラフトの人数と位置は、別に育成だけで数える（目標は `draft.development_target`）。

    外国人の人数・背番号・苗字は育成も含める（外国人の目標人数・背番号の重複・背ネームは区分によらない）。
    戦力（`squad`。外国人の獲得が穴を測る）は支配下の今の能力だけ（育成は1軍に出られない）。
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
        squad=tuple(
            (player.position, player.ratings.ratings)
            for player in remaining
            if player.is_registered and player.ratings is not None
        ),
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
    """引退・戦力外・昇格・育成契約の満了の後の球団（`plan_departures` の結果）。
    ドラフトの人数はこの後の人数で数える。"""

    retired: frozenset[int]
    # 戦力外の選手（球団ごとに外す順で、球団 id の順につなげたもの）
    released: tuple[int, ...]
    # 残る選手（球団 id の順・選手の並びの順）。昇格した選手は支配下・昇格後の背番号・翌年の昇格の年に置き換えてある
    survivors: tuple[OffseasonPlayer, ...]
    # 引退・戦力外・昇格・満了の後の球団（球団 id の昇順）
    draft_teams: tuple[DraftTeam, ...]
    # 育成から支配下への昇格（球団 id の順・球団の中は上げる順）と、育成契約の満了で自由契約にした選手（#170）
    promotions: tuple[Promotion, ...] = ()
    expired: tuple[int, ...] = ()


def plan_departures(
    clubs: Sequence[OffseasonClub],
    *,
    year: int,
    world_seed: int,
    start_year: int,
    manual_releases: Mapping[int, Collection[int]] | None = None,
    manual_development: Mapping[int, Mapping[int, DevelopmentDecision]] | None = None,
) -> Departures:
    """Y 年を締めるときの引退・戦力外・昇格・育成契約の満了（球団 id の昇順）。ドラフト会議の人数の幅（D3）も
    同じ関数で数える。

    順序は 引退 → 昇格の希望 → 戦力外（希望の分も空ける＝上乗せ）→ 昇格の確定 → 満了（`development.py`。#170）。
    `manual_releases`: GM が手動で戦力外を指名した球団（球団 id → 選手 id）。無い球団は自動（`release.py`）。
    `manual_development`: GM が手動で育成の扱いを指名した球団（球団 id → 選手 id → 扱い。#170 P2）。無い球団は自動
    （`development.py`）。手動の球団は指名どおりに上げ（支配下の上限を超える分だけ削る）、満了の選手を指名どおりに
    自由契約にする。戦力外が自動なら、指名した昇格の分も空ける（上乗せ。AI と同じ）。
    """
    ordered = sorted(clubs, key=lambda club: club.team_id)
    retired = set(decide_retirements(ordered, year=year, world_seed=world_seed, start_year=start_year))
    survivors: list[OffseasonPlayer] = []
    released: list[int] = []
    promotions: list[Promotion] = []
    expired: list[int] = []
    teams: list[DraftTeam] = []
    for club in ordered:
        remaining = [player for player in club.players if player.player_id not in retired]
        decisions = (manual_development or {}).get(club.team_id)
        chosen_development = None if decisions is None else decisions_as_chosen(decisions, remaining, year=year)
        wished = (
            decide_promotions(club, remaining, year=year, start_year=start_year, world_seed=world_seed)
            if chosen_development is None
            else chosen_development.promotions
        )
        # 戦力外は引退の後・ドラフトの前。ドラフトは戦力外の後の人数で数える。手動の球団は指名どおり
        chosen = (manual_releases or {}).get(club.team_id)
        leaving = set(
            decide_releases(
                club, remaining, year=year, start_year=start_year, world_seed=world_seed, promotions=len(wished)
            )
            if chosen is None
            else releases_as_chosen(chosen, remaining)
        )
        released.extend(sorted(leaving))
        remaining = [player for player in remaining if player.player_id not in leaving]
        promoted = (
            confirm_promotions(club, remaining, wished, year=year, world_seed=world_seed)
            if chosen_development is None
            else confirm_chosen_promotions(
                club, remaining, wished, year=year, start_year=start_year, world_seed=world_seed
            )
        )
        promotions.extend(promoted)
        lapsed = set(
            decide_expirations(
                club,
                remaining,
                promoted=[promotion.player_id for promotion in promoted],
                year=year,
                start_year=start_year,
                world_seed=world_seed,
            )
            if chosen_development is None
            else chosen_development.releases
        )
        expired.extend(sorted(lapsed))
        numbers = {promotion.player_id: promotion.number for promotion in promoted}
        remaining = [
            replace(
                player, contract=ContractStatus.REGISTERED, number=numbers[player.player_id], promoted_year=year + 1
            )
            if player.player_id in numbers
            else player
            for player in remaining
            if player.player_id not in lapsed
        ]
        survivors.extend(remaining)
        teams.append(_draft_team(club, remaining, world_seed=world_seed))
    return Departures(
        retired=frozenset(retired),
        released=tuple(released),
        survivors=tuple(survivors),
        draft_teams=tuple(teams),
        promotions=tuple(promotions),
        expired=tuple(expired),
    )


@dataclass(frozen=True)
class PreparedMeeting:
    """ドラフト会議を開くまでの計算（`prepare_meeting` の結果）。

    - `departures`: 引退と戦力外（`plan_departures`）
    - `market`: 外国人の獲得（`foreign_market.offseason_signings`）
    - `setup`: 会議の人数の幅・候補・指名順（`draft_meeting.open_meeting`）
    - `names`: 世界の全選手・獲得した外国人・候補の名前（新人の名前を重ねないため、続きの計算に渡す）
    """

    departures: Departures
    market: OffseasonSignings
    setup: MeetingSetup
    names: frozenset[str]


def prepare_meeting(
    clubs: Sequence[OffseasonClub],
    *,
    year: int,
    world_seed: int,
    start_year: int,
    used_names: Collection[str],
    draft: DraftContext,
    manual_releases: Mapping[int, Collection[int]] | None = None,
    manual_development: Mapping[int, Mapping[int, DevelopmentDecision]] | None = None,
) -> PreparedMeeting:
    """Y 年のドラフト会議を開くまで: 引退・戦力外（`plan_departures`）→ 外国人の獲得 →
    人数の幅と候補（`open_meeting`）。

    **会議を始めるとき（#297 D3）と締めるとき（`plan_offseason`）の唯一の関数**。会議の人数の幅と候補の位置ごとの人数は
    獲得した外国人の位置で決まるので、この順を両方で同じにする（獲得は決定的なので、入力が同じなら同じ幅になる）。
    引数は `plan_offseason` と同じ。
    """
    departures = plan_departures(
        clubs,
        year=year,
        world_seed=world_seed,
        start_year=start_year,
        manual_releases=manual_releases,
        manual_development=manual_development,
    )
    names = set(used_names)
    # 外国人の獲得は会議の前（獲得した外国人の分だけ、会議で指名する日本人が減る。#169）
    market = offseason_signings(departures.draft_teams, year=year, world_seed=world_seed, used_names=names)
    setup = open_meeting(
        market.teams, draft, year=year, world_seed=world_seed, used_names=names, signed=market.signed_positions
    )
    return PreparedMeeting(departures=departures, market=market, setup=setup, names=frozenset(names))


def replay_meeting(meeting: DraftRecord, setup: MeetingSetup) -> DraftState:
    """保存した会議（D3）を、締めるときに計算し直した設定で再生する。

    人数の幅が会議を始めたときと違う・候補の指紋が違う・行動を当てられない → `DraftChanged`。
    会議が終わっていない → `DraftInProgress`（締める処理は、途中の会議の残りを AI で進めない）。
    """
    if meeting.year != setup.year:
        raise ValueError("会議の年と締める年が違います。")
    meeting.ensure_same_slots(setup.slots)
    state = meeting.state(setup.strategy, setup.pool)
    if not state.is_complete:
        raise DraftInProgress()
    return state


def plan_offseason(
    clubs: Sequence[OffseasonClub],
    *,
    year: int,
    world_seed: int,
    start_year: int,
    used_names: Collection[str],
    draft: DraftContext,
    manual_releases: Mapping[int, Collection[int]] | None = None,
    manual_development: Mapping[int, Mapping[int, DevelopmentDecision]] | None = None,
    meeting: DraftRecord | None = None,
) -> OffseasonPlan:
    """Y 年を締める計画。`clubs` は球団 id の昇順で処理する。

    - `used_names`: 世界の全選手（引退した選手を含む）の名前。新人の名前が重ならないようにする
    - `start_year`: 世界の開幕年（生年月日の無い選手の年齢の推定に使う）
    - `draft`: ドラフト会議の設定（世界の規則と、Y 年の公式戦の最終順位）。既定値は持たない
    - `manual_releases`: GM が手動で戦力外を指名した球団（球団 id → 選手 id）。無い球団は自動（`release.py`）
    - `manual_development`: GM が手動で育成の扱いを指名した球団（球団 id → 選手 id → 扱い。#170 P2）。無い球団は自動
    - `meeting`: GM が進めて終えた会議の記録（D3。`replay_meeting` の検査を通す）。None なら会議を開かなかった年で、
      全球団を AI として最後まで進める
    """
    prepared = prepare_meeting(
        clubs,
        year=year,
        world_seed=world_seed,
        start_year=start_year,
        used_names=used_names,
        draft=draft,
        manual_releases=manual_releases,
        manual_development=manual_development,
    )
    departures, market, setup = prepared.departures, prepared.market, prepared.setup
    retired = departures.retired
    survivors = departures.survivors
    state = (
        DraftState.open(setup.strategy, setup.slots, setup.pool) if meeting is None else replay_meeting(meeting, setup)
    )
    result = finish_draft(setup, state, used_names=set(prepared.names))
    drafted = list(result.draftees)
    signed = list(market.signings)
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

    # 残る選手・新人・獲得した外国人をまとめて、項目ごとに水準の錨をかける（スタミナは別に、締める年の分布を保つ）。
    # 写像は**支配下**（残る支配下と、本指名の新人・獲得した外国人）で決め、育成（残る育成と育成の新人）にも
    # 同じ写像を当てる（毎年の育成の流入で1軍の水準が動かないように。2026-10-12 ユーザー決定。#198 D2）
    population: list[Ratings] = (
        [ratings for _, _, ratings in aged]
        + [draftee.ratings for draftee in drafted]
        + [signing.ratings for signing in signed]
    )
    inside = aged_inside + [not draftee.is_development for draftee in drafted] + [True for _ in signed]
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
    rookies_end = len(aged) + len(drafted)
    draftees = tuple(
        replace(draftee, ratings=ratings)
        for draftee, ratings in zip(drafted, anchored[len(aged) : rookies_end], strict=True)
    )
    signings = tuple(
        replace(signing, ratings=ratings) for signing, ratings in zip(signed, anchored[rookies_end:], strict=True)
    )
    return OffseasonPlan(
        year=year,
        retired=tuple(sorted(retired)),
        released=tuple(sorted(departures.released)),
        retained=retained,
        promotions=departures.promotions,
        expired=tuple(sorted(departures.expired)),
        draftees=draftees,
        signings=signings,
        lotteries=result.lotteries,
        without_ratings=tuple(without_ratings),
        anchor=anchor_correction([ratings for ratings, counted in zip(population, inside, strict=True) if counted]),
    )
