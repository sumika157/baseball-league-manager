"""ドラフトの新人の人数・位置・作り方。**純粋な関数**で、DB にも Django にも触れない。

人数は、引退・退団の後の**支配下**の人数から、開幕前の支配下が帯（`roster.OPENING_REGISTERED_BAND`。62〜66人）に
入るように決める（育成は数えない。`draftee_count`）。登録位置は支配下の構成と目標の構成との差の大きい位置から決め、
外国人はリーグの1軍の外国人枠の計の内側で目標（4人）まで補う（戦力外で外した外国人の入れ替え。#168）。
球団ごとの人数と位置は `plan_team_draft` が決め、日本人は**ドラフト会議**（`draft_meeting.py`。指名順は
世界の規則 `draft_order.py`）で候補から指名し、外国人は会議の外でここが作る（NPB の会議に外国人はいない）。
新人は支配下で入り、入団の経路は日本人が「ドラフト」、外国人が「新外国人」（`Draftee.acquisition_route`）。
日本人の新人はドラフトの指名（`DraftPick`。年は締める年、順位は会議の巡）を持ち、外国人は持たない。
能力は水準の錨の**前**の値（`offseason.py` が残る選手とまとめて錨をかける）。
名前とプロフィールの生成は `virtual_players/generator.py`（実データへの投入と同じ関数）。
"""

from __future__ import annotations

from collections.abc import Mapping, MutableSet, Sequence
from dataclasses import dataclass, replace
from datetime import date
from enum import Enum

from ..simulation.estimate import draw_growth_type, with_growth
from ..simulation.randomness import GameRandom
from ..simulation.ratings import AVERAGE_RATING, BatterRatings, PitcherRatings
from ..value_objects import AcquisitionRoute, DraftPick, ForeignPlayerRules, Position, Profile
from ..virtual_players import generator
from ..virtual_players.generator import AmateurPath
from .initial_ratings import SEASON_START_MONTH, STRENGTH_UNIT
from .prospects import PROSPECT_RATING_SD, draw_prospect_ratings, expected_prospect_ratings
from .roster import OPENING_REGISTERED_BAND

# 帯（開幕前の支配下）の出典は `roster.OPENING_REGISTERED_BAND`。ここは指名の人数の決め方だけを持つ
# 帯に入る範囲の中で、ふだん迎える新人の数（NPB の本指名は1球団平均約6人）。帯の中の引退・退団（約6〜7人）とつり合う
USUAL_DRAFTEES = 6
# 1年に迎える新人の上限。帯の下の球団（以前の実データから分岐した球団は支配下が約34人だった）が帯に追いつく速さ。
# 一度に埋めると、新人（平均約22歳・能力は平均より低い）が母集団の半分近くになり、年齢の分布と水準の錨が崩れる。
# NPB の「各球団は原則10名まで」と同じ値（ドラフト会議の1球団の上限）
MAX_DRAFTEES = 10
# 位置ごとの最低人数（これを下回る位置を先に埋める）
MIN_BY_POSITION = {
    Position.PITCHER: 15,
    Position.CATCHER: 3,
    Position.INFIELDER: 6,
    Position.OUTFIELDER: 5,
}
# 外国人の目標（リーグの1軍の外国人枠の計が小さければそこまで）。1回のオフで目標まで足す
# （#168 で年2人の上限をやめた。外す外国人は戦力外が新しく迎える外国人の見込みと比べて決めるので、
# 外した人数を同じオフに埋める）
# 値は協約の1軍の外国人枠の計（`ForeignPlayerRules.NPB.active`。1軍に置ける人数を保有の目安にする。#204）
# `or 0` は型のため（`active` は `int | None`。NPB の値は4で None にならない）
FOREIGN_TARGET = ForeignPlayerRules.NPB.active or 0

# 新人の能力の平均は経路ごと（`DraftRoute.rating_mean`）。標準偏差とスタミナの分布は `prospects.py`
# （世界の作成で成績の無い選手＝2軍の層と同じ引き方）
# 外国人の新人の年齢（満年齢）の範囲
FOREIGN_AGE_RANGE = (25, 31)
# 背番号（表記）。新人は支配下なので 1〜99 から選び、尽きたら 0・00（育成の3桁は使わない）
_NUMBER_POOLS = (tuple(str(number) for number in range(1, 100)), ("0", "00"))


class DraftRoute(Enum):
    """新人の経路。**経路の選択肢の唯一の出典**で、画面に表を複製しない。値は画面に出す名前。"""

    HIGH_SCHOOL = AmateurPath.HIGH_SCHOOL.value
    UNIVERSITY = AmateurPath.UNIVERSITY.value
    CORPORATE = AmateurPath.CORPORATE.value
    FOREIGN = "外国人"

    @property
    def label(self) -> str:
        return self.value

    @property
    def rating_mean(self) -> float:
        """新人の能力の平均（錨の前）。ドラフト会議で指名される日本人の新人は、経路ごとにこの平均に合わせ込む
        （`draft_meeting.POOL_RATING_SHIFT`）。"""
        return _RATING_MEAN[self]

    @classmethod
    def from_amateur_path(cls, path: AmateurPath | None) -> DraftRoute:
        """日本人の経歴の経路から（名前で対応づける。経路を足して対応を忘れると KeyError。テストが見張る）。

        経歴の無い（日本の学校を持たない）新人は外国人。
        """
        return cls.FOREIGN if path is None else cls[path.name]

    @classmethod
    def of_profile(cls, profile: Profile) -> DraftRoute:
        """新人のプロフィールから経路を導く（経路は保存していない。画面がオフの結果を読むときに使う）。

        外国人は外国人。日本人は、経歴のいちばん後ろの段階（社会人 > 大学 > 高校）が経路。
        """
        if profile.is_foreign_player:
            return cls.FOREIGN
        if profile.corporate_team:
            return cls.CORPORATE
        if profile.university:
            return cls.UNIVERSITY
        return cls.HIGH_SCHOOL


_RATING_MEAN = {
    DraftRoute.HIGH_SCHOOL: 35.0,
    DraftRoute.UNIVERSITY: 40.0,
    DraftRoute.CORPORATE: 43.0,
    DraftRoute.FOREIGN: 50.0,
}


@dataclass(frozen=True)
class DraftTeam:
    """ドラフトの前の球団の状態（引退・戦力外の後）。"""

    team_id: int
    # 今の区分が支配下の選手の、位置ごとの人数（育成は数えない。試合に出られず、指名数と位置の補充に効かせない）
    position_counts: Mapping[Position, int]
    # 外国人の人数（育成も数える。目標 `foreign_target` は保有の目安）
    foreign_count: int
    # リーグの外国人の1軍登録の枠の計（`ForeignPlayerRules.active`）。None は無制限。目標人数の上限に使う
    foreign_limit: int | None
    # 引退の後の現在の在籍が使っている背番号
    used_numbers: frozenset[str]
    # 現在の選手の背ネームの苗字（ローマ字）ごとの人数。同じ苗字の新人に頭文字を付けるため
    surname_counts: Mapping[str, int]
    # リーグの支配下の上限。None は無制限
    registered_limit: int | None

    @property
    def registered_count(self) -> int:
        """支配下の人数（引退・退団の後）。"""
        return sum(self.position_counts.values())


@dataclass(frozen=True)
class Draftee:
    """新人。能力は錨の前（`offseason.py` が錨をかけて差し替える）。"""

    team_id: int
    route: DraftRoute
    position: Position
    number: str
    name: str
    profile: Profile
    ratings: BatterRatings | PitcherRatings

    @property
    def is_foreign(self) -> bool:
        return self.route is DraftRoute.FOREIGN

    @property
    def acquisition_route(self) -> AcquisitionRoute:
        """在籍に記録する入団の経路。日本人はドラフトの指名から（`DraftPick.acquisition_route`。今は本指名だけなので
        ドラフト）、外国人は新外国人（新人はどちらも支配下で入る）。"""
        if self.is_foreign:
            return AcquisitionRoute.NEW_FOREIGN
        draft = self.profile.draft
        return AcquisitionRoute.DRAFT if draft is None else draft.acquisition_route


@dataclass(frozen=True)
class Rookie:
    """背番号・背ネーム・指名が決まる前の新人（ドラフト会議の候補と、会議の外の外国人）。能力は錨の前。"""

    route: DraftRoute
    position: Position
    name: str
    surname_romaji: str
    given_romaji: str
    profile: Profile
    ratings: BatterRatings | PitcherRatings


@dataclass(frozen=True)
class TeamDraftPlan:
    """球団の今年の新人の人数と位置。外国人の位置を先に決め、日本人（会議で指名する）は残り。"""

    team_id: int
    # 新人の位置（`choose_positions` の並び。外国人と日本人を合わせた全員）
    positions: tuple[Position, ...]
    # 外国人にする新人の位置（会議の外で作る）
    foreign_positions: tuple[Position, ...]
    # 日本人の新人の位置（会議で指名する人数と、AI が埋める位置の計画）
    japanese_positions: tuple[Position, ...]
    # 日本人の新人の最少（帯の下端までの不足から外国人の分を引いた数。0未満は0）。GM の選択終了（D3）が使う
    japanese_minimum: int

    @property
    def japanese_count(self) -> int:
        return len(self.japanese_positions)


def draftee_count(registered: int, *, registered_limit: int | None, expected_other: int = 0) -> int:
    """新人の人数。`registered` は引退・退団の後の支配下の人数（育成は数えない）。

    開幕前の支配下が帯（`OPENING_REGISTERED_BAND`）に入る範囲で、ふだんは `USUAL_DRAFTEES` 人。
    下端に届かないなら届く人数まで増やす（1年に `MAX_DRAFTEES` 人まで）。上端とリーグの支配下の上限
    （`registered_limit`）は超えない。既に達している・超えている球団は0人（締める処理は上限で失敗させない）。
    ふつうは、その前の戦力外（`release.py`）が `USUAL_DRAFTEES` 人を迎える空きを作っている。

    `expected_other` は、ほかの獲得手段（外国人の獲得・トレード・昇格。どれも未実装）で埋まる見込みの人数。
    ドラフトはその残りを埋める。いまは手段が無いので常に 0。
    """
    gap = OPENING_REGISTERED_BAND.gap(registered, limit=registered_limit, expected=expected_other)
    return min(max(USUAL_DRAFTEES, gap.shortfall), MAX_DRAFTEES, gap.vacancy)


def foreign_target(foreign_limit: int | None) -> int:
    """球団の外国人の目標人数。`foreign_limit` はリーグの外国人の1軍登録の枠の計（None は無制限）で、
    小さければそこまで。

    オフが外国人を足す数の出典。協約（第82条の2）は保有の人数を縛らないので、これは AI の編成の目安で、
    1軍に置ける外国人の数（NPB は4人）を保有の目標にしている（1軍に置けない外国人を抱えない）。
    """
    return FOREIGN_TARGET if foreign_limit is None else min(FOREIGN_TARGET, foreign_limit)


def foreign_count_to_add(team: DraftTeam, draftees: int) -> int:
    """外国人にする新人の数。目標（`foreign_target`）に達している球団には足さない。"""
    return max(0, min(foreign_target(team.foreign_limit) - team.foreign_count, draftees))


def plan_team_draft(team: DraftTeam) -> TeamDraftPlan:
    """球団の今年の新人の人数と位置（外国人と、会議で指名する日本人）。

    人数は `draftee_count`、位置は `choose_positions`、外国人は後ろの指名の捕手でない位置から（`_foreign_picks`）。
    """
    count = draftee_count(team.registered_count, registered_limit=team.registered_limit)
    positions = choose_positions(team, count)
    foreign_indexes = _foreign_picks(positions, foreign_count_to_add(team, count))
    shortfall = OPENING_REGISTERED_BAND.gap(team.registered_count, limit=team.registered_limit).shortfall
    return TeamDraftPlan(
        team_id=team.team_id,
        positions=tuple(positions),
        foreign_positions=tuple(positions[index] for index in sorted(foreign_indexes)),
        japanese_positions=tuple(position for index, position in enumerate(positions) if index not in foreign_indexes),
        japanese_minimum=max(0, min(shortfall, count) - len(foreign_indexes)),
    )


def expected_rookie_ratings(route: DraftRoute, position: Position) -> BatterRatings | PitcherRatings:
    """経路 `route` の新人に見込める能力（ぶれを除いた平均。錨の前の尺度）。

    戦力外が、今の外国人と新しく迎える外国人を比べるのに使う。分布の出典は新人を引く
    `draw_rookie`（経路ごとの平均 `DraftRoute.rating_mean` と `prospects.draw_prospect_ratings`）。
    """
    return expected_prospect_ratings(mean=route.rating_mean, position=position)


def choose_positions(team: DraftTeam, count: int) -> list[Position]:
    """新人の登録位置。最低人数に足りない位置を先に、そのあとは目標の構成との差が大きい位置から。

    数えるのは支配下だけ（`DraftTeam.position_counts`）。目標の構成は、指名の後の支配下の人数を位置の割合で割り振ったもの。
    """
    targets = generator.largest_remainder(team.registered_count + count, generator.POSITION_RATIOS)
    counts = {position: team.position_counts.get(position, 0) for position in generator.POSITION_RATIOS}
    order = list(generator.POSITION_RATIOS)
    chosen: list[Position] = []
    for _ in range(count):
        short = {position: MIN_BY_POSITION[position] - counts[position] for position in MIN_BY_POSITION}
        if max(short.values()) > 0:
            position = max(short, key=lambda item: (short[item], -order.index(item)))
        else:
            position = max(order, key=lambda item: (targets[item] - counts[item], -order.index(item)))
        counts[position] += 1
        chosen.append(position)
    return chosen


def _foreign_picks(positions: list[Position], count: int) -> set[int]:
    """外国人にする新人の添字。捕手にはせず、後ろの指名から選ぶ。"""
    eligible = [index for index, position in enumerate(positions) if position is not Position.CATCHER]
    return set(eligible[len(eligible) - min(count, len(eligible)) :])


def _draw_number(rng: GameRandom, used: set[str]) -> str:
    for pool in _NUMBER_POOLS:
        available = [number for number in pool if number not in used]
        if available:
            number = generator.pick(rng, available)
            used.add(number)
            return number
    raise ValueError("背番号が尽きました")  # 00・0〜99 が埋まる球団は無い（新人を迎えるのは支配下66人未満）


def draw_rookie(
    rng: GameRandom,
    position: Position,
    *,
    foreign: bool,
    year: int,
    used_names: MutableSet[str],
    path: AmateurPath | None = None,
    rating_shift: float = 0.0,
    rating_sd: float = PROSPECT_RATING_SD,
) -> Rookie:
    """新人ひとりぶんの名前・プロフィール・能力。乱数は名前 → 経歴 → 生年月日 → 体格 → 能力 → 成長型の順。

    `path` を渡すと日本人の経路をそれにする（省くと `AmateurPath.weight` で引く）。`rating_shift` は能力の平均を
    経路の平均（`DraftRoute.rating_mean`）からずらす点、`rating_sd` は能力の標準偏差（どちらもドラフト会議の候補の
    合わせ込み。`draft_meeting.py`）。
    指名（`Profile.draft`）は持たない（会議の結果で付ける）。
    """
    if foreign:
        overseas = generator.foreign_name(rng, used_names)
        full_name, kana, birthplace = overseas.name, overseas.kana, overseas.country
        surname_romaji, given_romaji = overseas.surname_romaji, overseas.given_romaji
    else:
        domestic = generator.japanese_name(rng, used_names)
        full_name, kana, birthplace = domestic.name, domestic.kana, generator.prefecture(rng)
        surname_romaji, given_romaji = domestic.surname_romaji, domestic.given_romaji
    career = generator.amateur_career(
        rng, is_foreign=foreign, birthplace=birthplace, path=path, foreign_age_range=FOREIGN_AGE_RANGE
    )
    route = DraftRoute.from_amateur_path(career.path)
    age = career.debut_age
    birth_date = generator.birth_date_for(rng, age=age, as_of=date(year + 1, SEASON_START_MONTH, 1))
    throws, bats = generator.handedness(rng, position)
    height_cm, weight_kg = generator.physique(rng, position)
    ratings = draw_prospect_ratings(rng, mean=route.rating_mean + rating_shift, position=position, sd=rating_sd)
    value = ratings.pitching_value if isinstance(ratings, PitcherRatings) else ratings.batting_value
    # 成長型は錨の前の能力の高さで傾ける（新人は平均より低く入るので、若いほど晩成に傾くのは意図どおり）
    growth = draw_growth_type(rng, age=age, strength=(value - AVERAGE_RATING) / STRENGTH_UNIT)
    profile = Profile(
        birth_date=birth_date,
        throws=throws,
        bats=bats,
        height_cm=height_cm,
        weight_kg=weight_kg,
        birthplace=birthplace,
        high_school=career.high_school,
        university=career.university,
        corporate_team=career.corporate_team,
        nationality=birthplace if foreign else "",
        name_kana=kana,
        is_foreign_player=foreign,
    )
    return Rookie(route, position, full_name, surname_romaji, given_romaji, profile, with_growth(ratings, growth))


def club_draftees(
    rng: GameRandom,
    team: DraftTeam,
    plan: TeamDraftPlan,
    *,
    drafted: Sequence[tuple[Rookie, DraftPick]],
    year: int,
    used_names: MutableSet[str],
) -> tuple[Draftee, ...]:
    """締める年 `year` に球団に入る新人（入団は翌年）。

    `drafted` はドラフト会議で指名した日本人（指名の巡の順）。外国人は会議の外で、`plan.foreign_positions` の位置に
    ここで作る（ドラフトの指名を持たない）。`used_names` は世界の全選手（引退した選手・会議の候補を含む）の名前で、
    外国人の名前を足していく。乱数は `rng` から、外国人（1人ずつ）→ 背番号（日本人の指名の順、続けて外国人）の
    順に引く。
    """
    foreign = [
        draw_rookie(rng, position, foreign=True, year=year, used_names=used_names)
        for position in plan.foreign_positions
    ]
    rookies = [replace(rookie, profile=replace(rookie.profile, draft=pick)) for rookie, pick in drafted] + foreign
    used_numbers = set(team.used_numbers)
    numbers = [_draw_number(rng, used_numbers) for _ in rookies]

    # 背ネーム: 現在の選手や他の新人と苗字が重なる新人だけ、名前の頭文字を付ける
    surnames = dict(team.surname_counts)
    for rookie in rookies:
        surnames[rookie.surname_romaji] = surnames.get(rookie.surname_romaji, 0) + 1
    return tuple(
        Draftee(
            team_id=team.team_id,
            route=rookie.route,
            position=rookie.position,
            number=number,
            name=rookie.name,
            profile=replace(
                rookie.profile,
                back_name=generator.back_name(
                    rookie.surname_romaji, rookie.given_romaji, shares_surname=surnames[rookie.surname_romaji] > 1
                ),
            ),
            ratings=rookie.ratings,
        )
        for rookie, number in zip(rookies, numbers, strict=True)
    )
