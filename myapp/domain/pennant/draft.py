"""自動ドラフト。引退のあと、球団ごとに新人を決める。**純粋な関数**で、DB にも Django にも触れない。

人数は「1球団 34 人に戻す」ように 2〜8 人（40 人を超えない）。登録位置は目標の構成との差の大きい
位置から埋め、外国人は登録枠の内側で年に2人まで補う。新人の質は球団の順位によらず同じ。
能力は水準の錨の**前**の値（`offseason.py` が残る選手とまとめて錨をかける）。
名前とプロフィールの生成は `virtual_players/generator.py`（実データへの投入と同じ関数）。
"""

from __future__ import annotations

from collections.abc import Mapping, MutableSet
from dataclasses import dataclass, replace
from datetime import date
from enum import Enum

from ..simulation.estimate import draw_growth_type, with_growth
from ..simulation.randomness import GameRandom
from ..simulation.ratings import AVERAGE_RATING, BatterRatings, PitcherRatings
from ..simulation.samples import draw_ratings
from ..value_objects import Position, Profile
from ..virtual_players import generator
from ..virtual_players.generator import AmateurPath
from .initial_ratings import SEASON_START_MONTH, STRENGTH_UNIT

# 1球団の人数の目安。新人は「ここに戻す」人数（2〜8人）で、MAX_ROSTER を超えない
ROSTER_TARGET = 34
MIN_DRAFTEES = 2
MAX_DRAFTEES = 8
# 位置ごとの最低人数（これを下回る位置を先に埋める）
MIN_BY_POSITION = {
    Position.PITCHER: 15,
    Position.CATCHER: 3,
    Position.INFIELDER: 6,
    Position.OUTFIELDER: 5,
}
# 外国人の目標（リーグの登録枠が小さければ枠まで）と、1回のドラフトで足す上限
FOREIGN_TARGET = 4
MAX_FOREIGN_PER_DRAFT = 2

# 新人の能力の標準偏差。平均は経路ごと（`DraftRoute.rating_mean`）
ROOKIE_RATING_SD = 6.0
# 投手のスタミナ: 先発型の割合と、型ごとの（平均, 標準偏差）
STARTER_TYPE_RATIO = 0.55
STARTER_STAMINA = (55.0, 8.0)
RELIEVER_STAMINA = (36.0, 8.0)
# 外国人の新人の年齢（満年齢）の範囲
FOREIGN_AGE_RANGE = (25, 31)
# 背番号は 1〜99 から選び、尽きたら 100〜999
_NUMBER_POOLS = (range(1, 100), range(100, 1000))


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
        """新人の能力の平均（錨の前）。"""
        return _RATING_MEAN[self]

    @classmethod
    def from_amateur_path(cls, path: AmateurPath | None) -> DraftRoute:
        """日本人の経歴の経路から（名前で対応づける。経路を足して対応を忘れると KeyError。テストが見張る）。

        経歴の無い（日本の学校を持たない）新人は外国人。
        """
        return cls.FOREIGN if path is None else cls[path.name]


_RATING_MEAN = {
    DraftRoute.HIGH_SCHOOL: 35.0,
    DraftRoute.UNIVERSITY: 40.0,
    DraftRoute.CORPORATE: 43.0,
    DraftRoute.FOREIGN: 50.0,
}


@dataclass(frozen=True)
class DraftTeam:
    """ドラフトの前の球団の状態（引退の後）。"""

    team_id: int
    position_counts: Mapping[Position, int]
    foreign_count: int
    # リーグの外国人の登録枠。None は無制限
    foreign_limit: int | None
    # 引退の後の現在の在籍が使っている背番号
    used_numbers: frozenset[int]
    # 現在の選手の背ネームの苗字（ローマ字）ごとの人数。同じ苗字の新人に頭文字を付けるため
    surname_counts: Mapping[str, int]

    @property
    def roster_size(self) -> int:
        return sum(self.position_counts.values())


@dataclass(frozen=True)
class Draftee:
    """新人。能力は錨の前（`offseason.py` が錨をかけて差し替える）。"""

    team_id: int
    route: DraftRoute
    position: Position
    number: int
    name: str
    profile: Profile
    ratings: BatterRatings | PitcherRatings

    @property
    def is_foreign(self) -> bool:
        return self.route is DraftRoute.FOREIGN


@dataclass(frozen=True)
class _Pending:
    """背ネームが決まる前の新人（球団の同姓は全員を引いてから数える）。"""

    route: DraftRoute
    position: Position
    number: int
    name: str
    surname_romaji: str
    given_romaji: str
    profile: Profile
    ratings: BatterRatings | PitcherRatings


def draftee_count(roster_size: int) -> int:
    """新人の人数。34人に戻すよう 2〜8 人で、40人を超えない。"""
    wanted = min(MAX_DRAFTEES, max(MIN_DRAFTEES, ROSTER_TARGET - roster_size))
    return max(0, min(wanted, generator.MAX_ROSTER - roster_size))


def foreign_count_to_add(team: DraftTeam, draftees: int) -> int:
    """外国人にする新人の数。登録枠に達している球団には足さない。"""
    target = FOREIGN_TARGET if team.foreign_limit is None else min(FOREIGN_TARGET, team.foreign_limit)
    return max(0, min(target - team.foreign_count, MAX_FOREIGN_PER_DRAFT, draftees))


def choose_positions(team: DraftTeam, count: int) -> list[Position]:
    """新人の登録位置。最低人数に足りない位置を先に、そのあとは目標の構成との差が大きい位置から。"""
    targets = generator.largest_remainder(ROSTER_TARGET, generator.POSITION_RATIOS)
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


def _draw_number(rng: GameRandom, used: set[int]) -> int:
    for pool in _NUMBER_POOLS:
        available = [number for number in pool if number not in used]
        if available:
            number = generator.pick(rng, available)
            used.add(number)
            return number
    raise ValueError("背番号が尽きました")  # 1〜999 が全部埋まる球団は無い


def _draw_rookie_ratings(rng: GameRandom, route: DraftRoute, position: Position) -> BatterRatings | PitcherRatings:
    starter = rng.random() < STARTER_TYPE_RATIO
    stamina_mean, stamina_sd = STARTER_STAMINA if starter else RELIEVER_STAMINA
    return draw_ratings(
        rng,
        mean=route.rating_mean,
        sd=ROOKIE_RATING_SD,
        position=position,
        stamina_mean=stamina_mean,
        stamina_sd=stamina_sd,
    )


def _draw_rookie(
    rng: GameRandom, position: Position, *, foreign: bool, year: int, number: int, used_names: MutableSet[str]
) -> _Pending:
    """新人ひとりぶんの名前・プロフィール・能力。乱数は名前 → 経歴 → 生年月日 → 体格 → 能力 → 成長型の順。"""
    if foreign:
        overseas = generator.foreign_name(rng, used_names)
        full_name, kana, birthplace = overseas.name, overseas.kana, overseas.country
        surname_romaji, given_romaji = overseas.surname_romaji, overseas.given_romaji
    else:
        domestic = generator.japanese_name(rng, used_names)
        full_name, kana, birthplace = domestic.name, domestic.kana, generator.prefecture(rng)
        surname_romaji, given_romaji = domestic.surname_romaji, domestic.given_romaji
    career = generator.amateur_career(
        rng, is_foreign=foreign, birthplace=birthplace, foreign_age_range=FOREIGN_AGE_RANGE
    )
    route = DraftRoute.from_amateur_path(career.path)
    age = career.debut_age
    birth_date = generator.birth_date_for(rng, age=age, as_of=date(year + 1, SEASON_START_MONTH, 1))
    throws, bats = generator.handedness(rng, position)
    height_cm, weight_kg = generator.physique(rng, position)
    ratings = _draw_rookie_ratings(rng, route, position)
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
        debut_year=year + 1,
        high_school=career.high_school,
        university=career.university,
        corporate_team=career.corporate_team,
        nationality=birthplace if foreign else "",
        name_kana=kana,
        is_foreign_player=foreign,
    )
    return _Pending(
        route, position, number, full_name, surname_romaji, given_romaji, profile, with_growth(ratings, growth)
    )


def draft_class(rng: GameRandom, team: DraftTeam, *, year: int, used_names: MutableSet[str]) -> tuple[Draftee, ...]:
    """締める年 `year` のドラフトで、球団に入る新人（入団は翌年）。

    `used_names` は世界の全選手（引退した選手を含む）の名前で、新人の名前を足していく。
    乱数は `rng` から、背番号 → 指名の順（1人ずつ）に引く。
    """
    count = draftee_count(team.roster_size)
    positions = choose_positions(team, count)
    foreign_indexes = _foreign_picks(positions, foreign_count_to_add(team, count))
    used_numbers = set(team.used_numbers)
    pending = [
        _draw_rookie(
            rng,
            position,
            foreign=index in foreign_indexes,
            year=year,
            number=_draw_number(rng, used_numbers),
            used_names=used_names,
        )
        for index, position in enumerate(positions)
    ]

    # 背ネーム: 現在の選手や他の新人と苗字が重なる新人だけ、名前の頭文字を付ける
    surnames = dict(team.surname_counts)
    for rookie in pending:
        surnames[rookie.surname_romaji] = surnames.get(rookie.surname_romaji, 0) + 1
    return tuple(
        Draftee(
            team_id=team.team_id,
            route=rookie.route,
            position=rookie.position,
            number=rookie.number,
            name=rookie.name,
            profile=replace(
                rookie.profile,
                back_name=generator.back_name(
                    rookie.surname_romaji, rookie.given_romaji, shares_surname=surnames[rookie.surname_romaji] > 1
                ),
            ),
            ratings=rookie.ratings,
        )
        for rookie in pending
    )
