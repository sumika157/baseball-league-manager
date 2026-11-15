"""ペナントの首脳陣（#210 M1: 監督）。**純粋な値と規則**で、DB にも Django にも触れない。

監督は人（`StaffMember`）と就任（`StaffTenure`。球団 × 役割 × 初年〜最終年）の2つで持つ。人の素の傾向
（`ManagerStyle`）を AI 監督の重み（`simulation.tactics.Tactics`）として試合に渡す（`tactics_of`）。
傾向は優劣ではない（采配の上手さは持たせない。2026-11-15 ユーザー決定）。

- 実データには監督がいない。`Team` 集約には入れず（実データと共有）、別の表に置く。不変条件（同じ球団の同じ役割は
  同時に1人・1人1職）はリポジトリが `ensure_valid_tenures` で守る
- 年齢は保持しない。生年月日から、その年の4月1日時点の満年齢を求める（選手と同じ基準日）
- 世界の作成で全球団に架空の監督を置く（`plan_opening_managers`）。シーズンを締めるときは、高齢の監督が退任し
  （`manager_retires`）、退任した球団と監督のいない球団（この機能より前の世界）に架空の監督が就く
  （`plan_manager_changes`。就任は翌年から）。成績での交代・GM が選ぶのは M2
- 乱数は世界のシード・年・球団の id から作る（同じ世界なら何度計算しても同じ監督になる）
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, MutableSet, Sequence
from dataclasses import dataclass, replace
from datetime import date
from enum import Enum

from ..exceptions import InvalidStaffTenure
from ..simulation.randomness import GameRandom, game_seed, game_uniform, make_random, weighted_index
from ..simulation.tactics import TacticLevel, Tactics
from ..value_objects import Season
from ..virtual_players.generator import birth_date_for, japanese_name, randint

# 架空の監督の就任時の年齢の幅
MIN_MANAGER_AGE = 45
MAX_MANAGER_AGE = 65
# 傾向の段階（−2〜+2）の出る比率。0 を中心に対称（標準が半分、両端が5%ずつ）
STYLE_WEIGHTS: Mapping[TacticLevel, float] = {
    TacticLevel.MUCH_LESS: 0.05,
    TacticLevel.LESS: 0.20,
    TacticLevel.STANDARD: 0.50,
    TacticLevel.MORE: 0.20,
    TacticLevel.MUCH_MORE: 0.05,
}
# 翌年の4月1日の年齢がこれ以上なら、必ず退任する
MANDATORY_RETIREMENT_AGE = 70
# 翌年の年齢ごとの退任の確率（ここに無い年齢は退任しない）
RETIREMENT_CHANCE: Mapping[int, float] = {65: 0.10, 66: 0.15, 67: 0.20, 68: 0.30, 69: 0.40}


class StaffRole(Enum):
    """首脳陣の役割。値は保存する綴り。M1 は監督だけ（コーチは M4）。"""

    MANAGER = "監督"

    @property
    def label(self) -> str:
        return self.value


@dataclass(frozen=True)
class ManagerStyle:
    """監督の素の傾向（4軸。−2〜+2）。M1 はそのまま采配の重みになる（`tactics_of`）。"""

    starter_leash: TacticLevel = TacticLevel.STANDARD
    pinch_hitting: TacticLevel = TacticLevel.STANDARD
    base_stealing: TacticLevel = TacticLevel.STANDARD
    bunting: TacticLevel = TacticLevel.STANDARD

    def __post_init__(self) -> None:
        # 整数で渡されても段階にそろえる（範囲の外は ValueError。保存の側は CHECK 制約が止める）
        for name in ("starter_leash", "pinch_hitting", "base_stealing", "bunting"):
            object.__setattr__(self, name, TacticLevel(getattr(self, name)))


@dataclass(frozen=True)
class StaffMember:
    """首脳陣のひとり。`id` は保存するまで None。"""

    name: str
    name_kana: str
    birth_date: date
    style: ManagerStyle
    id: int | None = None

    def age_in(self, year: int) -> int:
        """その年の4月1日時点の満年齢（`age_in_year`）。"""
        return age_in_year(self.birth_date, year)


def age_in_year(birth_date: date, year: int) -> int:
    """その年の基準日（`Season.age_reference_date`。選手と同じ4月1日）時点の満年齢。首脳陣の年齢の出典はここ1つ。"""
    reference = Season(year).age_reference_date
    had_birthday = (reference.month, reference.day) >= (birth_date.month, birth_date.day)
    return reference.year - birth_date.year - (0 if had_birthday else 1)


@dataclass(frozen=True)
class StaffTenure:
    """就任ひとつ（人・球団・役割・初年〜最終年）。最終年が None なら、いまも就いている。"""

    member_id: int
    team_id: int
    role: StaffRole
    first_year: int
    last_year: int | None = None

    def __post_init__(self) -> None:
        if self.last_year is not None and self.last_year < self.first_year:
            raise InvalidStaffTenure(
                f"就任の最終年（{self.last_year}年）が初年（{self.first_year}年）より前になっています。"
            )

    def covers(self, year: int) -> bool:
        """その年に就いているか。"""
        return self.first_year <= year and (self.last_year is None or year <= self.last_year)

    @property
    def is_current(self) -> bool:
        """いまも就いている（最終年が決まっていない）か。"""
        return self.last_year is None

    def close(self, last_year: int) -> StaffTenure:
        """`last_year` を最終年にして閉じた就任。既に閉じていれば InvalidStaffTenure。"""
        if not self.is_current:
            raise InvalidStaffTenure(f"既に{self.last_year}年で退任している就任は閉じられません。")
        return replace(self, last_year=last_year)

    def overlaps(self, other: StaffTenure) -> bool:
        """期間が1年でも重なるか。"""
        self_last = self.last_year if self.last_year is not None else float("inf")
        other_last = other.last_year if other.last_year is not None else float("inf")
        return self.first_year <= other_last and other.first_year <= self_last


@dataclass(frozen=True)
class StaffAssignment:
    """保存済みの人と、その就任の組（読み出しの単位）。"""

    member: StaffMember
    tenure: StaffTenure


@dataclass(frozen=True)
class Appointment:
    """新しい人の就任（人はまだ保存していない）。保存すると人の id で `StaffTenure` になる。"""

    member: StaffMember
    team_id: int
    role: StaffRole
    first_year: int

    def tenure(self, member_id: int) -> StaffTenure:
        return StaffTenure(member_id=member_id, team_id=self.team_id, role=self.role, first_year=self.first_year)


@dataclass(frozen=True)
class ManagerChanges:
    """シーズンを締めるときの監督の交代。`retirements` は閉じた後の就任（最終年 = 締める年）。"""

    retirements: tuple[StaffTenure, ...] = ()
    appointments: tuple[Appointment, ...] = ()

    @property
    def count(self) -> int:
        """新しく就いた監督の数（退任の後任と、監督のいなかった球団への就任）。"""
        return len(self.appointments)


def ensure_valid_tenures(tenures: Iterable[StaffTenure]) -> None:
    """就任の集まりの不変条件。同じ球団の同じ役割は同時に1人、1人が同時に就けるのは1職。破れば InvalidStaffTenure。"""
    by_post: dict[tuple[int, StaffRole], list[StaffTenure]] = defaultdict(list)
    by_member: dict[int, list[StaffTenure]] = defaultdict(list)
    for tenure in tenures:
        by_post[(tenure.team_id, tenure.role)].append(tenure)
        by_member[tenure.member_id].append(tenure)
    for (team_id, role), group in by_post.items():
        if _first_overlap(group) is not None:
            raise InvalidStaffTenure(f"同じ球団（id={team_id}）の{role.label}が、同じ年に2人就くことになります。")
    for member_id, group in by_member.items():
        if _first_overlap(group) is not None:
            raise InvalidStaffTenure(f"同じ人（id={member_id}）が、同じ年に2つの職に就くことになります。")


def _first_overlap(group: Sequence[StaffTenure]) -> tuple[StaffTenure, StaffTenure] | None:
    ordered = sorted(group, key=lambda tenure: tenure.first_year)
    for before, after in zip(ordered, ordered[1:], strict=False):
        if before.overlaps(after):
            return before, after
    return None


def tactics_of(style: ManagerStyle) -> Tactics:
    """監督の傾向を、試合に渡す采配の重みにする（M1 は素の傾向そのまま）。"""
    return Tactics(
        starter_leash=style.starter_leash,
        pinch_hitting=style.pinch_hitting,
        base_stealing=style.base_stealing,
        bunting=style.bunting,
    )


def _draw_level(rng: GameRandom) -> TacticLevel:
    levels = list(STYLE_WEIGHTS)
    return levels[weighted_index(rng, [STYLE_WEIGHTS[level] for level in levels])]


def draw_manager(rng: GameRandom, *, year: int, used_names: MutableSet[str]) -> StaffMember:
    """架空の監督をひとり作る（`year` の4月1日に45〜65歳。傾向は各軸とも0を中心に対称）。

    名前は `used_names` に足す（呼ぶ側が渡した名前＝世界の選手・いまの監督と重ねない）。
    """
    age = randint(rng, MIN_MANAGER_AGE, MAX_MANAGER_AGE)
    birth_date = birth_date_for(rng, age=age, as_of=Season(year).age_reference_date)
    name = japanese_name(rng, used_names)
    style = ManagerStyle(
        starter_leash=_draw_level(rng),
        pinch_hitting=_draw_level(rng),
        base_stealing=_draw_level(rng),
        bunting=_draw_level(rng),
    )
    return StaffMember(name=name.name, name_kana=name.kana, birth_date=birth_date, style=style)


def manager_retires(age: int, draw: float) -> bool:
    """翌年の年齢 `age` の監督が退任するか。`draw` は [0, 1) の一様乱数。70歳以上は必ず、65〜69歳は確率で。"""
    if age >= MANDATORY_RETIREMENT_AGE:
        return True
    return draw < RETIREMENT_CHANCE.get(age, 0.0)


def _manager_random(world_seed: int, year: int, team_key: int) -> GameRandom:
    return make_random(game_seed(world_seed, year, f"manager:{team_key}"))


def plan_opening_managers(
    team_keys: Mapping[int, int], *, year: int, world_seed: int, used_names: MutableSet[str]
) -> list[Appointment]:
    """世界の作成で全球団に置く監督（`year` から）。`team_keys` は写した球団の id → 乱数の鍵（分岐元の球団の id）。

    分岐元の球団の id で乱数を作るので、同じシードで同じリーグを分岐すれば同じ監督になる。
    """
    return [
        Appointment(
            member=draw_manager(
                _manager_random(world_seed, year, team_keys[team_id]), year=year, used_names=used_names
            ),
            team_id=team_id,
            role=StaffRole.MANAGER,
            first_year=year,
        )
        for team_id in sorted(team_keys)
    ]


def plan_manager_changes(
    serving: Sequence[StaffAssignment],
    team_ids: Iterable[int],
    *,
    year: int,
    world_seed: int,
    used_names: MutableSet[str],
) -> ManagerChanges:
    """`year` を締めるときの監督の交代。`serving` は `year` に就いている首脳陣。

    - 監督がいる球団: 翌年の年齢で退任を決める（`manager_retires`）。退任したら就任を `year` で閉じ、後任が翌年から就く
    - 監督のいない球団（この機能より前の世界）: 翌年から監督が就く（締める年には遡らない）
    """
    managers = {
        item.tenure.team_id: item
        for item in serving
        if item.tenure.role is StaffRole.MANAGER and item.tenure.covers(year) and item.tenure.is_current
    }
    retirements: list[StaffTenure] = []
    appointments: list[Appointment] = []
    for team_id in sorted(set(team_ids)):
        current = managers.get(team_id)
        if current is not None:
            draw = game_uniform(world_seed, year, f"manager-retire:{team_id}")
            if not manager_retires(current.member.age_in(year + 1), draw):
                continue
            retirements.append(current.tenure.close(year))
        member = draw_manager(_manager_random(world_seed, year + 1, team_id), year=year + 1, used_names=used_names)
        appointments.append(Appointment(member=member, team_id=team_id, role=StaffRole.MANAGER, first_year=year + 1))
    return ManagerChanges(retirements=tuple(retirements), appointments=tuple(appointments))
