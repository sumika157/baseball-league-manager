"""ドラフトの新人の人数・位置・作り方。**純粋な関数**で、DB にも Django にも触れない。

人数は、引退・退団の後の**支配下**の人数から、開幕前の支配下が帯（`roster.OPENING_REGISTERED_BAND`。62〜66人）に
入るように決める（育成は数えない。`draftee_count`）。登録位置は支配下の構成と目標の構成との差の大きい位置から決める。
この人数は**外国人の獲得を含めた新しい支配下の選手の枠**で、外国人はリーグの1軍の外国人枠の計の内側で目標（4人）まで
獲得で補う（`foreign_count_to_add`。戦力外で外した外国人の入れ替え。#168）。獲得は会議の前に `foreign_market.py` が
候補から選び（#169。NPB の会議に外国人はいない）、日本人の新人は残りの人数（新人の人数 − 獲得した外国人）を
**ドラフト会議**（`draft_meeting.py`。指名順は世界の規則 `draft_order.py`）で候補から指名する。
球団ごとの人数と位置は `plan_team_draft` が決める。
新人は支配下で入り、入団の経路は「ドラフト」（`Draftee.acquisition_route`）。新人はドラフトの指名
（`DraftPick`。年は締める年、順位は会議の巡）を持つ。
本指名の後の**育成の巡**（#198 D2）で、球団ごとの育成の目標（`development_target`。目安の帯 `roster.DEVELOPMENT_BAND`
の中）までの不足を1年 `MAX_DEVELOPMENT_PICKS` 人まで指名する。育成の新人は育成で入り（3桁の背番号）、
経路は「育成ドラフト」、指名は `DraftPick(is_development=True)`。支配下の帯と上限には数えない。
能力は水準の錨の**前**の値（`offseason.py` が残る選手とまとめて錨をかける）。
名前とプロフィールの生成は `virtual_players/generator.py`（実データへの投入と同じ関数）。
"""

from __future__ import annotations

from collections.abc import Mapping, MutableSet, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from enum import Enum

from ..simulation.estimate import draw_growth_type, draw_potential, with_growth, with_potential
from ..simulation.randomness import GameRandom, game_seed, game_uniform, make_random
from ..simulation.ratings import AVERAGE_RATING, POTENTIAL_AVERAGE, BatterRatings, PitcherRatings
from ..value_objects import (
    DEVELOPMENTAL_FIRST_NUMBERS,
    DEVELOPMENTAL_MIN_NUMBER,
    DEVELOPMENTAL_SPARE_NUMBERS,
    AcquisitionRoute,
    ContractStatus,
    DraftPick,
    ForeignPlayerRules,
    Position,
    Profile,
)
from ..virtual_players import generator
from ..virtual_players.generator import AmateurPath
from .initial_ratings import SEASON_START_MONTH, STRENGTH_UNIT
from .prospects import PROSPECT_RATING_SD, draw_prospect_ratings
from .roster import DEVELOPMENT_BAND, OPENING_REGISTERED_BAND

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
# 外国人の目標（リーグの1軍の外国人枠の計が小さければそこまで）。1回のオフで目標まで獲得で補う
# （#168 で年2人の上限をやめた。外す外国人は戦力外が新しく迎える外国人の見込みと比べて決めるので、
# 外した人数を同じオフに埋める。獲得は `foreign_market.py`。#169）
# 値は協約の1軍の外国人枠の計（`ForeignPlayerRules.NPB.active`。1軍に置ける人数を保有の目安にする。#204）
# `or 0` は型のため（`active` は `int | None`。NPB の値は4で None にならない）
FOREIGN_TARGET = ForeignPlayerRules.NPB.active or 0

# 新人の能力の平均は経路ごと（`DraftRoute.rating_mean`）。標準偏差とスタミナの分布は `prospects.py`
# （世界の作成で成績の無い選手＝2軍の層と同じ引き方）
# 社会人の新人の能力の、入団の年齢の範囲の両端（23歳と26歳）の差（点。`DraftRoute.rating_mean_at`。#308 Q2）。
# 平均 43 のまま 23歳 約41.3 → 26歳 約44.8（計画の「23歳 41.5 → 26歳 45」）
CORPORATE_AGE_SPREAD = 3.5
# 育成ドラフトで1球団が1年に迎える育成の上限（ドラフト会議の育成の巡の最多。#198 の一般化。GM の上限も同じ＝D3）。
# 目安の帯（`roster.DEVELOPMENT_BAND`）の下にいる球団も、一度には埋めない（本指名の `MAX_DRAFTEES` と同じ考え）
MAX_DEVELOPMENT_PICKS = 6
# 背番号（表記）。支配下の新人（と獲得した外国人）は 1〜99 から選び、尽きたら 0・00（育成の3桁は使わない）
NUMBER_POOLS = (tuple(str(number) for number in range(1, DEVELOPMENTAL_MIN_NUMBER)), ("0", "00"))
# 育成の新人は3桁。100番台から選び、尽きたら 200〜999（範囲の出典は `value_objects` で、実データの投入と共有）
_DEVELOPMENT_NUMBER_POOLS = (DEVELOPMENTAL_FIRST_NUMBERS, DEVELOPMENTAL_SPARE_NUMBERS)


class DraftRoute(Enum):
    """新人の経路。**経路の選択肢の唯一の出典**で、画面に表を複製しない。値は画面に出す名前。"""

    HIGH_SCHOOL = AmateurPath.HIGH_SCHOOL.value
    UNIVERSITY = AmateurPath.UNIVERSITY.value
    CORPORATE = AmateurPath.CORPORATE.value

    @property
    def label(self) -> str:
        return self.value

    @property
    def rating_mean(self) -> float:
        """新人の能力の平均（錨の前）。ドラフト会議で指名される日本人の新人は、経路ごとにこの平均に合わせ込む
        （`draft_meeting.POOL_RATING_SHIFT`）。"""
        return _RATING_MEAN[self]

    def rating_mean_at(self, age: int) -> float:
        """`age` 歳で入る新人の能力の平均（錨の前）。

        社会人だけ年齢で傾ける（#308 Q2。2026-10-25 ユーザー決定）: 入団の年齢の範囲（`CORPORATE_DEBUT_AGE_RANGE`）の
        両端で `CORPORATE_AGE_SPREAD` 点違い、範囲の中央で経路の平均（`rating_mean`）になる（年齢は一様に引くので経路の
        平均は変わらない）。年上ほど今の能力が高く、伸びる年が短い。ほかの経路は年齢が1つなので `rating_mean` のまま。
        """
        if self is not DraftRoute.CORPORATE:
            return self.rating_mean
        low, high = generator.CORPORATE_DEBUT_AGE_RANGE
        return self.rating_mean + CORPORATE_AGE_SPREAD * (age - (low + high) / 2) / (high - low)

    @property
    def allows_development_pick(self) -> bool:
        """この経路の選手を育成で指名できるか。**育成で指名できるかの唯一の出典**（#307）。

        社会人（日本野球連盟の企業チーム）の選手は育成で指名しない（育成制度の理念から外れるとして、日本野球連盟が
        2011年に「指名するなら支配下で」と申し入れた慣行。明文の規定ではない。クラブチームの選手は育成でも指名できるが、
        このアプリの社会人は企業とクラブを分けず、名前はすべて企業チーム）。
        """
        return self is not DraftRoute.CORPORATE

    @classmethod
    def development_amateur_paths(cls) -> tuple[AmateurPath, ...]:
        """育成で指名できる日本人の経歴の経路（`allows_development_pick` から導く。候補と実データの投入が使う）。"""
        return tuple(path for path in AmateurPath if cls.from_amateur_path(path).allows_development_pick)

    @classmethod
    def from_amateur_path(cls, path: AmateurPath) -> DraftRoute:
        """日本人の経歴の経路から（名前で対応づける。経路を足して対応を忘れると KeyError。テストが見張る）。"""
        return cls[path.name]

    @classmethod
    def of_profile(cls, profile: Profile) -> DraftRoute:
        """新人のプロフィールから経路を導く（経路は保存していない。画面がオフの結果を読むときに使う）。

        経歴のいちばん後ろの段階（社会人 > 大学 > 高校）が経路。獲得した外国人（日本の経歴を持たない）は
        新人ではないので、呼び出し側が先に分ける（オフの結果の表記は `foreign_market.newcomer_route_label`）。
        """
        if profile.corporate_team:
            return cls.CORPORATE
        if profile.university:
            return cls.UNIVERSITY
        return cls.HIGH_SCHOOL


_RATING_MEAN = {
    DraftRoute.HIGH_SCHOOL: 35.0,
    DraftRoute.UNIVERSITY: 40.0,
    DraftRoute.CORPORATE: 43.0,
}


@dataclass(frozen=True)
class DraftTeam:
    """ドラフトの前の球団の状態（引退・戦力外の後）。"""

    team_id: int
    # 今の区分が支配下の選手の、位置ごとの人数（育成は数えない。試合に出られず、指名数と位置の補充に効かせない）
    position_counts: Mapping[Position, int]
    # 外国人の人数（育成も数える。目標 `foreign_target` は保有の目安。獲得で補う人数 `foreign_count_to_add` に使う）
    foreign_count: int
    # リーグの外国人の1軍登録の枠の計（`ForeignPlayerRules.active`）。None は無制限。目標人数の上限に使う
    foreign_limit: int | None
    # 引退の後の現在の在籍（と獲得した外国人）が使っている背番号
    used_numbers: frozenset[str]
    # 現在の選手の背ネームの苗字（ローマ字）ごとの人数。同じ苗字の新人に頭文字を付けるため
    surname_counts: Mapping[str, int]
    # リーグの支配下の上限。None は無制限
    registered_limit: int | None
    # 今の区分が育成の選手の、位置ごとの人数（引退の後。育成は戦力外にならない）。育成ドラフトの人数と位置に使う
    development_counts: Mapping[Position, int] = field(default_factory=dict)
    # 育成の人数の目標（`development_target`。目安の帯の中）。0 なら育成ドラフトで指名しない
    development_target: int = 0

    @property
    def registered_count(self) -> int:
        """支配下の人数（引退・退団の後）。"""
        return sum(self.position_counts.values())

    @property
    def development_count(self) -> int:
        """育成の人数（引退の後）。"""
        return sum(self.development_counts.values())


@dataclass(frozen=True)
class Draftee:
    """ドラフトの新人（日本人）。能力は錨の前（`offseason.py` が錨をかけて差し替える）。"""

    team_id: int
    route: DraftRoute
    position: Position
    number: str
    name: str
    profile: Profile
    ratings: BatterRatings | PitcherRatings

    @property
    def is_development(self) -> bool:
        """育成ドラフトの新人か（指名 `DraftPick.is_development` から）。"""
        return _drafted_as_development(self.profile)

    @property
    def contract(self) -> ContractStatus:
        """入団の契約区分。育成ドラフトの新人は育成（背番号は3桁）、本指名は支配下。"""
        return ContractStatus.DEVELOPMENTAL if self.is_development else ContractStatus.REGISTERED

    @property
    def acquisition_route(self) -> AcquisitionRoute:
        """在籍に記録する入団の経路。ドラフトの指名から（`DraftPick.acquisition_route`。本指名はドラフト、
        育成は育成ドラフト）。"""
        draft = self.profile.draft
        return AcquisitionRoute.DRAFT if draft is None else draft.acquisition_route


@dataclass(frozen=True)
class Rookie:
    """背番号・背ネーム・指名が決まる前の新人（ドラフト会議の候補）。能力は錨の前。"""

    route: DraftRoute
    position: Position
    name: str
    surname_romaji: str
    given_romaji: str
    profile: Profile
    ratings: BatterRatings | PitcherRatings


@dataclass(frozen=True)
class TeamDraftPlan:
    """球団の今年の新人の人数と位置。獲得した外国人の位置を先に除き、日本人（会議で指名する）は残り。"""

    team_id: int
    # 新しい支配下の選手の位置（`choose_positions` の並び。獲得する外国人と日本人の新人を合わせた全員の計画）
    positions: tuple[Position, ...]
    # 獲得した外国人の位置（会議の前に `foreign_market.py` が獲得した。`DraftSlot.base_counts` に足す）
    foreign_positions: tuple[Position, ...]
    # 日本人の新人の位置（会議で指名する人数と、AI が埋める位置の計画）
    japanese_positions: tuple[Position, ...]
    # 日本人の新人の最少（帯の下端までの不足から外国人の分を引いた数。0未満は0）。GM の選択終了（D3）が使う
    japanese_minimum: int
    # 育成ドラフトの新人の位置（会議の育成の巡で AI が埋める人数と位置の計画。`development_positions`）
    development_positions: tuple[Position, ...] = ()

    @property
    def japanese_count(self) -> int:
        return len(self.japanese_positions)


def draftee_count(registered: int, *, registered_limit: int | None, expected_other: int = 0) -> int:
    """新人の人数。`registered` は引退・退団の後の支配下の人数（育成は数えない）。

    開幕前の支配下が帯（`OPENING_REGISTERED_BAND`）に入る範囲で、ふだんは `USUAL_DRAFTEES` 人。
    下端に届かないなら届く人数まで増やす（1年に `MAX_DRAFTEES` 人まで）。上端とリーグの支配下の上限
    （`registered_limit`）は超えない。既に達している・超えている球団は0人（締める処理は上限で失敗させない）。
    ふつうは、その前の戦力外（`release.py`）が `USUAL_DRAFTEES` 人を迎える空きを作っている。

    この人数は外国人の獲得を含めた新しい支配下の選手の枠（獲得した外国人の分だけ日本人の新人が減る。#169 Q2）。
    `expected_other` は、ほかの獲得手段（トレード・昇格。どれも未実装）で埋まる見込みの人数。
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
    """外国人の獲得で補う人数。目標（`foreign_target`）に達している球団は0。新しい支配下の選手の枠（`draftees`）の
    内側（#169 Q2）。"""
    return max(0, min(foreign_target(team.foreign_limit) - team.foreign_count, draftees))


def newcomer_plan(team: DraftTeam) -> tuple[int, tuple[Position, ...]]:
    """新しい支配下の選手（獲得する外国人と日本人の新人）の人数と、位置の計画（`choose_positions` の並び）。

    外国人の獲得（`foreign_market.py`）と会議の人数（`plan_team_draft`）が同じ計画を見る。
    """
    count = draftee_count(team.registered_count, registered_limit=team.registered_limit)
    return count, tuple(choose_positions(team, count))


def plan_team_draft(team: DraftTeam, signed: Sequence[Position] = ()) -> TeamDraftPlan:
    """球団の今年の新人の人数と位置（会議で指名する日本人と、育成の巡の人数と位置）。

    人数と位置は `newcomer_plan`（獲得した外国人を含めた計画）。`signed` は会議の前に獲得した外国人の位置
    （`foreign_market.py`）で、計画から外国人の位置を1つずつ除いた残りが日本人の位置（計画に無い位置の外国人は、
    捕手でない後ろの位置を除く）。日本人の人数は「新人の人数 − 獲得した外国人」（#169 Q2）。獲得が0なら計画のまま。
    育成は `development_draftee_count` と `development_positions`（支配下の人数・位置には効かない）。
    """
    count, positions = newcomer_plan(team)
    if len(signed) > count:
        raise ValueError("獲得した外国人が新しい支配下の選手の枠を超えています。")
    japanese = list(positions)
    for position in signed:
        japanese.pop(_signed_place(japanese, position))
    shortfall = OPENING_REGISTERED_BAND.gap(team.registered_count, limit=team.registered_limit).shortfall
    development = development_draftee_count(team.development_count, target=team.development_target)
    return TeamDraftPlan(
        team_id=team.team_id,
        positions=positions,
        foreign_positions=tuple(signed),
        japanese_positions=tuple(japanese),
        japanese_minimum=max(0, min(shortfall, count) - len(signed)),
        development_positions=development_positions(team.development_counts, development),
    )


def _signed_place(positions: Sequence[Position], signed: Position) -> int:
    """獲得した外国人の位置 `signed` を計画から除く添字。同じ位置の後ろから。無ければ捕手でない後ろの位置、
    それも無ければ末尾（会議の人数だけを合わせる）。"""
    same = [index for index, position in enumerate(positions) if position is signed]
    if same:
        return same[-1]
    others = [index for index, position in enumerate(positions) if position is not Position.CATCHER]
    return others[-1] if others else len(positions) - 1


def development_target(team_id: int, *, world_seed: int) -> int:
    """球団の育成の人数の目標。目安の帯（`DEVELOPMENT_BAND`）の中から、世界のシードと球団 id で決まる
    （年では動かない）。

    球団ごとに違う目標にして、育成を多く抱える球団と少ない球団の差を出す（NPB も球団差が大きい）。
    """
    band = DEVELOPMENT_BAND
    span = band.high - band.low + 1
    return band.low + min(span - 1, int(game_uniform(world_seed, 0, f"development-target-{team_id}") * span))


def development_draftee_count(current: int, *, target: int) -> int:
    """育成ドラフトで迎える人数。`current` は引退の後の育成の人数。目標 `target` までの不足を、
    1年 `MAX_DEVELOPMENT_PICKS` 人まで。目標に達していれば0。支配下の帯・支配下の上限には数えない。"""
    return min(MAX_DEVELOPMENT_PICKS, max(0, target - current))


def development_positions(counts: Mapping[Position, int], count: int) -> tuple[Position, ...]:
    """育成の新人 `count` 人の登録位置。育成の構成と目標の構成（`POSITION_RATIOS`）との差を足す
    （実データの投入と同じ `generator.additions_by_position`）。位置の最低人数は支配下の話なので見ない。"""
    if count <= 0:
        return ()
    adds = generator.additions_by_position(sum(counts.values()) + count, counts)
    return tuple(position for position in generator.POSITION_RATIOS for _ in range(adds.get(position, 0)))


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


def draw_number(rng: GameRandom, used: set[str], pools: Sequence[Sequence[str]] = NUMBER_POOLS) -> str:
    """空いている背番号を1つ選んで `used` に足す（新人と獲得した外国人。`pools` の前から）。"""
    for pool in pools:
        available = [number for number in pool if number not in used]
        if available:
            number = generator.pick(rng, available)
            used.add(number)
            return number
    # 00・0〜99 が埋まる球団は無い（新人と外国人を迎えるのは支配下66人未満）。
    # 100〜999 も同じ（育成は目安の帯で30人まで）
    raise ValueError("背番号が尽きました")


def draw_rookie(
    rng: GameRandom,
    position: Position,
    *,
    year: int,
    used_names: MutableSet[str],
    path: AmateurPath | None = None,
    rating_shift: float = 0.0,
    rating_sd: float = PROSPECT_RATING_SD,
) -> Rookie:
    """日本人の新人（会議の候補）ひとりぶんの名前・プロフィール・能力。乱数は名前 → 出身地 → 経歴 → 生年月日 →
    体格 → 能力 → 成長型の順。

    `path` を渡すと日本人の経路をそれにする（省くと `AmateurPath.weight` で引く）。`rating_shift` は能力の平均を
    経路と年齢の平均（`DraftRoute.rating_mean_at`。社会人だけ年齢で傾く）からずらす点、`rating_sd` は能力の
    標準偏差（どちらもドラフト会議の候補の合わせ込み。`draft_meeting.py`）。
    指名（`Profile.draft`）は持たない（会議の結果で付ける）。
    """
    domestic = generator.japanese_name(rng, used_names)
    birthplace = generator.prefecture(rng)
    career = generator.amateur_career(rng, is_foreign=False, birthplace=birthplace, path=path)
    assert career.path is not None, "日本人の経歴は経路を持つ"
    route = DraftRoute.from_amateur_path(career.path)
    age = career.debut_age
    birth_date = generator.birth_date_for(rng, age=age, as_of=date(year + 1, SEASON_START_MONTH, 1))
    throws, bats = generator.handedness(rng, position)
    height_cm, weight_kg = generator.physique(rng, position)
    ratings = draw_prospect_ratings(
        rng, mean=route.rating_mean_at(age) + rating_shift, position=position, sd=rating_sd
    )
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
        name_kana=domestic.kana,
    )
    return Rookie(
        route,
        position,
        domestic.name,
        domestic.surname_romaji,
        domestic.given_romaji,
        profile,
        with_growth(ratings, growth),
    )


def club_draftees(
    rng: GameRandom,
    team: DraftTeam,
    plan: TeamDraftPlan,
    *,
    drafted: Sequence[tuple[Rookie, DraftPick]],
) -> tuple[Draftee, ...]:
    """締める年に球団に入る新人（入団は翌年）。

    `drafted` はドラフト会議で指名した日本人（本指名の巡の順、続けて育成の巡の順）。`team` は獲得した外国人の
    背番号と苗字を含めた球団（外国人の獲得は会議の前。`foreign_market.py`）。乱数は `rng` から、支配下の背番号
    （本指名の順）→ 育成の背番号（3桁。育成の指名の順）の順に引く（育成を足しても、本指名の背番号は変わらない）。
    返す並びは本指名・育成。
    """
    signed = [replace(rookie, profile=replace(rookie.profile, draft=pick)) for rookie, pick in drafted]
    registered = [rookie for rookie in signed if not _drafted_as_development(rookie.profile)]
    development = [rookie for rookie in signed if _drafted_as_development(rookie.profile)]
    rookies = registered + development
    used_numbers = set(team.used_numbers)
    numbers = [draw_number(rng, used_numbers) for _ in registered] + [
        draw_number(rng, used_numbers, _DEVELOPMENT_NUMBER_POOLS) for _ in development
    ]

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


def _drafted_as_development(profile: Profile) -> bool:
    """育成ドラフトの指名を持つか（`Draftee.is_development` と会議の結果の振り分けが共有する判定）。"""
    draft = profile.draft
    return draft is not None and draft.is_development


def with_drawn_potential(
    rookie: Rookie, *, world_seed: int, year: int, key: str, mean: float = POTENTIAL_AVERAGE
) -> Rookie:
    """新人に伸びしろを付ける。乱数は用途 `key` の専用の流れ（`draw_rookie` の乱数の流れに引く回数を足さない）。

    会議の候補（`draft_meeting.py`。平均は合わせ込んだ `POOL_POTENTIAL_MEAN`。経路ごと）が通る（外国人の獲得の候補は
    `foreign_market.py` が同じ事前の分布から、平均は普通の50で引く）。
    """
    potential = draw_potential(make_random(game_seed(world_seed, year, key)), mean=mean)
    return replace(rookie, ratings=with_potential(rookie.ratings, potential))
