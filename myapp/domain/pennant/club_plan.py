"""球団の編成（`ClubPlan`）。GM が AI 監督の決めた編成を上書きしたものだけを持つ。

球団ごとに1つ。4つの区画（1軍登録・オーダー・ローテーション・抑え）が、それぞれ
**「自動（`None`）」か「手動の値」** を持つ。自動の区画は、持たずに毎日 AI 監督が決める
（`simulation/manager.py`）。持っているのは**選手の id だけ**で、選手の中身（登録位置・外国人かどうか・
能力）は持たない。検査のたびに、その時点のロスターを引数で受け取る
（`Team` 集約に入れないのは、`Team` が実データと共有していて、ロスターの読み込みが通算成績の
集計まで連れてくるため）。

不変条件（上書きを**決めるとき**に検査する。`set_*`）:

- 1軍登録は29人まで。外国人はリーグの登録枠まで。球団の選手だけ。打順を組める野手9人と投手1人以上・捕手1人以上。
  外国人の出場枠の中で自動のスタメン9人を組めること（外国人の投手が先発する分の枠も見込む）
- オーダーは1軍から9人。重複なし。守備位置が9つ（捕・一・二・三・遊・左・中・右・指）をすべて満たす。
  捕手の枠は登録位置が捕手の選手。外国人は試合の出場枠まで
- ローテーションは1軍の投手（重複なし・6人まで）
- 抑えは1軍の投手で、手動のローテーションの中にいない。ほかに先発できる1軍の投手がいる

オーダーの外国人が出場枠を使い切り、ローテーションが外国人だけのときは、その日のオーダーを自動に落とす
（オーダーとローテーションは別々に決めるので、組み合わせは当てはめるときに見る）。

**決めたあとにロスターが変わる**（登録を外した選手がオーダーにいる、など）と、上書きは使えなくなる。
そのときは例外で進行を止めず、**その区画だけ自動に落とし**、理由を返す（`resolve_club`）。不正なソートキーを
既定の並びに落とす規則と同じ扱い。
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from enum import Enum

from ..exceptions import DomainError, ForeignPlayerQuotaExceeded, InvalidClubPlan
from ..simulation.manager import (
    ACTIVE_ROSTER_SIZE,
    LINEUP_SIZE,
    ROTATION_SIZE,
    ClubOrders,
    ClubRoster,
    LineupSlot,
    SimPitcher,
    choose_active_roster,
    plan_pitching_staff,
    register_players,
)
from ..value_objects import FieldingPosition, Position

FP = FieldingPosition

# オーダーが満たす守備位置（8つの守備と指名打者）
LINEUP_POSITIONS: frozenset[FieldingPosition] = frozenset(
    {
        FP.CATCHER,
        FP.FIRST_BASE,
        FP.SECOND_BASE,
        FP.THIRD_BASE,
        FP.SHORTSTOP,
        FP.LEFT_FIELD,
        FP.CENTER_FIELD,
        FP.RIGHT_FIELD,
        FP.DESIGNATED_HITTER,
    }
)
MIN_ACTIVE_BATTERS = LINEUP_SIZE
MIN_ACTIVE_PITCHERS = 1


class PlanSection(Enum):
    """編成の区画。値は画面に出す名前。"""

    ACTIVE = "1軍登録"
    LINEUP = "オーダー"
    ROTATION = "ローテーション"
    CLOSER = "抑え"


@dataclass(frozen=True)
class ClubMember:
    """編成の検査に使う、球団の選手1人（検査のたびに、その時点のロスターから作る）。"""

    player_id: int
    name: str
    position: Position
    is_foreign: bool = False

    @property
    def is_pitcher(self) -> bool:
        return self.position.is_pitcher


@dataclass(frozen=True)
class ClubLimits:
    """編成が守る枠（None なら無制限）。登録枠は所属リーグの値、出場枠は世界のリーグで最も厳しい値
    （`strictest_game_limit`）。"""

    active_size: int = ACTIVE_ROSTER_SIZE
    foreign_roster_limit: int | None = None
    foreign_game_limit: int | None = None


@dataclass(frozen=True)
class LineupChoice:
    """オーダーの1枠（リストの位置が打順）。誰がどこを守るか。選手は id だけ持つ。"""

    player_id: int
    position: FieldingPosition


@dataclass(frozen=True)
class PlanFallback:
    """上書きが使えず、その区画を自動に落としたこと。理由は画面にそのまま出せる日本語。"""

    section: PlanSection
    reason: str


@dataclass(frozen=True)
class ResolvedClub:
    """その日の球団の編成。1軍の `roster` と、上書きが有効な区画の `orders`、自動に落ちた区画の `fallbacks`。"""

    roster: ClubRoster
    orders: ClubOrders | None
    fallbacks: tuple[PlanFallback, ...]


def strictest_game_limit(game_limits: Iterable[int | None]) -> int | None:
    """世界のリーグの出場枠のうち、最も厳しいもの（None は無制限）。

    交流戦はホーム球団のリーグの枠で行うので、編成はどのリーグと当たっても組める枠で検査する
    （自リーグの枠で検査すると、枠の厳しいリーグとの交流戦でスタメンを組めず進行が止まる）。
    """
    limits = [limit for limit in game_limits if limit is not None]
    return min(limits) if limits else None


def members_of(pool: ClubRoster) -> dict[int, ClubMember]:
    """登録候補（能力のある在籍選手）を、検査に使う形にする。"""
    members = {b.player_id: ClubMember(b.player_id, b.name, b.position, b.is_foreign) for b in pool.batters}
    members.update(
        {p.player_id: ClubMember(p.player_id, p.name, Position.PITCHER, p.is_foreign) for p in pool.pitchers}
    )
    return members


# --- 検査 ---


def _label(member: ClubMember) -> str:
    return f"{member.name}（id={member.player_id}）"


def _members_of_ids(player_ids: Sequence[int], roster: dict[int, ClubMember], what: str) -> list[ClubMember]:
    """id を選手に引き直す。重複と、球団の選手でない id を弾く。"""
    if len(set(player_ids)) != len(player_ids):
        raise InvalidClubPlan(f"{what}に同じ選手が重複しています。")
    unknown = [player_id for player_id in player_ids if player_id not in roster]
    if unknown:
        raise InvalidClubPlan(
            f"{what}に、この球団に在籍していない（または能力のない）選手が含まれています（id={unknown[0]}）。"
        )
    return [roster[player_id] for player_id in player_ids]


def check_active(player_ids: Sequence[int], roster: dict[int, ClubMember], limits: ClubLimits) -> None:
    """1軍登録として成立するか。成立しなければ DomainError。"""
    members = _members_of_ids(player_ids, roster, "1軍登録")
    if len(members) > limits.active_size:
        raise InvalidClubPlan(f"1軍登録は{limits.active_size}人までです（{len(members)}人）。")
    pitchers = sum(m.is_pitcher for m in members)
    batters = len(members) - pitchers
    if batters < MIN_ACTIVE_BATTERS or pitchers < MIN_ACTIVE_PITCHERS:
        raise InvalidClubPlan(
            f"1軍には野手{MIN_ACTIVE_BATTERS}人以上と投手{MIN_ACTIVE_PITCHERS}人以上が要ります"
            f"（野手{batters}人・投手{pitchers}人）。"
        )
    if not any(m.position is Position.CATCHER for m in members):
        raise InvalidClubPlan("1軍には捕手が1人以上要ります。")
    foreign = sum(m.is_foreign for m in members)
    if limits.foreign_roster_limit is not None and foreign > limits.foreign_roster_limit:
        raise ForeignPlayerQuotaExceeded(
            f"外国人選手の1軍登録は{limits.foreign_roster_limit}人までです（{foreign}人）。"
        )
    if limits.foreign_game_limit is not None:
        # 自動のスタメンは出場枠の中で組む。外国人の投手が先発すると枠が1つ減るので、その分も見込む
        reserved = 1 if any(m.is_pitcher and m.is_foreign for m in members) else 0
        foreign_batters = sum(m.is_foreign and not m.is_pitcher for m in members)
        playable = batters - foreign_batters + min(foreign_batters, max(0, limits.foreign_game_limit - reserved))
        if playable < LINEUP_SIZE:
            raise ForeignPlayerQuotaExceeded(
                f"外国人選手の出場は1試合{limits.foreign_game_limit}人までなので、この1軍ではスタメン{LINEUP_SIZE}人を"
                f"組めません（組めるのは{playable}人）。外国人でない野手を増やしてください。"
            )


def check_lineup(
    choices: Sequence[LineupChoice],
    roster: dict[int, ClubMember],
    active_ids: Collection[int],
    limits: ClubLimits,
) -> None:
    """オーダーとして成立するか。成立しなければ DomainError。"""
    if len(choices) != LINEUP_SIZE:
        raise InvalidClubPlan(f"オーダーは{LINEUP_SIZE}人です（{len(choices)}人）。")
    members = _members_of_ids([c.player_id for c in choices], roster, "オーダー")
    for member in members:
        if member.is_pitcher:
            raise InvalidClubPlan(f"投手はオーダーに入れられません（{_label(member)}）。")
        if member.player_id not in active_ids:
            raise InvalidClubPlan(f"1軍に登録されていない選手はオーダーに入れられません（{_label(member)}）。")
    positions = [c.position for c in choices]
    if len(set(positions)) != len(positions) or set(positions) != LINEUP_POSITIONS:
        raise InvalidClubPlan(
            "オーダーは守備位置（捕・一・二・三・遊・左・中・右・指）を1つずつ満たす必要があります。"
        )
    for choice, member in zip(choices, members, strict=True):
        if choice.position is FP.CATCHER and member.position is not Position.CATCHER:
            raise InvalidClubPlan(f"捕手の枠には、登録位置が捕手の選手だけ入れられます（{_label(member)}）。")
    foreign = sum(m.is_foreign for m in members)
    if limits.foreign_game_limit is not None and foreign > limits.foreign_game_limit:
        raise ForeignPlayerQuotaExceeded(
            f"外国人選手の出場は1試合{limits.foreign_game_limit}人までです（オーダーに{foreign}人）。"
        )


def check_rotation(
    player_ids: Sequence[int],
    roster: dict[int, ClubMember],
    active_ids: Collection[int],
    closer_id: int | None = None,
) -> None:
    """ローテーションとして成立するか。成立しなければ DomainError。"""
    if not player_ids:
        raise InvalidClubPlan("ローテーションの投手が1人もいません。")
    if len(player_ids) > ROTATION_SIZE:
        raise InvalidClubPlan(f"ローテーションは{ROTATION_SIZE}人までです（{len(player_ids)}人）。")
    for member in _members_of_ids(player_ids, roster, "ローテーション"):
        if not member.is_pitcher:
            raise InvalidClubPlan(f"ローテーションに入れられるのは投手だけです（{_label(member)}）。")
        if member.player_id not in active_ids:
            raise InvalidClubPlan(f"1軍に登録されていない投手はローテーションに入れられません（{_label(member)}）。")
        if member.player_id == closer_id:
            raise InvalidClubPlan(f"抑えに指定した投手はローテーションに入れられません（{_label(member)}）。")


def check_closer(
    player_id: int,
    roster: dict[int, ClubMember],
    active_ids: Collection[int],
    rotation_ids: Sequence[int] | None = None,
) -> None:
    """抑えとして成立するか。成立しなければ DomainError。`rotation_ids` は手動のローテーション（自動なら None）。"""
    (member,) = _members_of_ids([player_id], roster, "抑え")
    if not member.is_pitcher:
        raise InvalidClubPlan(f"抑えにできるのは投手だけです（{_label(member)}）。")
    if member.player_id not in active_ids:
        raise InvalidClubPlan(f"1軍に登録されていない投手は抑えにできません（{_label(member)}）。")
    if rotation_ids is not None and member.player_id in rotation_ids:
        raise InvalidClubPlan(f"ローテーションに入れた投手は抑えにできません（{_label(member)}）。")
    # 抑えは自動のローテーションから外れるので、ほかに先発できる 1軍の投手が要る
    if not any(pid != player_id and pid in roster and roster[pid].is_pitcher for pid in active_ids):
        raise InvalidClubPlan(f"抑えのほかに先発できる1軍の投手がいません（{_label(member)}）。")


# --- 集約 ---


@dataclass
class ClubPlan:
    """球団ひとつの編成の上書き。`None` の区画は自動（AI 監督が毎日決める）。

    上書きは `set_*` で決める（検査を通ったものだけが入る）。コンストラクタは保存した値を
    そのまま写すので検査しない（ロスターが変わって使えなくなった上書きも、読み込めなければならない）。
    """

    team_id: int
    active_ids: tuple[int, ...] | None = None
    lineup: tuple[LineupChoice, ...] | None = None
    rotation: tuple[int, ...] | None = None
    closer_id: int | None = None

    @property
    def is_empty(self) -> bool:
        """すべて自動か（保存する必要がない）。"""
        return self.active_ids is None and self.lineup is None and self.rotation is None and self.closer_id is None

    def set_active(self, player_ids: Sequence[int], *, roster: dict[int, ClubMember], limits: ClubLimits) -> None:
        check_active(player_ids, roster, limits)
        self.active_ids = tuple(player_ids)

    def set_lineup(
        self,
        choices: Sequence[LineupChoice],
        *,
        roster: dict[int, ClubMember],
        active_ids: Collection[int],
        limits: ClubLimits,
    ) -> None:
        """`active_ids` は、いま 1軍にいる選手（1軍登録が自動なら AI が選んだ人）。"""
        check_lineup(choices, roster, active_ids, limits)
        self.lineup = tuple(choices)

    def set_rotation(
        self, player_ids: Sequence[int], *, roster: dict[int, ClubMember], active_ids: Collection[int]
    ) -> None:
        check_rotation(player_ids, roster, active_ids, self.closer_id)
        self.rotation = tuple(player_ids)

    def set_closer(self, player_id: int, *, roster: dict[int, ClubMember], active_ids: Collection[int]) -> None:
        check_closer(player_id, roster, active_ids, self.rotation)
        self.closer_id = player_id

    def clear_active(self) -> None:
        self.active_ids = None

    def clear_lineup(self) -> None:
        self.lineup = None

    def clear_rotation(self) -> None:
        self.rotation = None

    def clear_closer(self) -> None:
        self.closer_id = None


# --- その日の編成に当てはめる ---


def resolve_club(plan: ClubPlan | None, pool: ClubRoster, limits: ClubLimits) -> ResolvedClub:
    """上書きを、その日の編成に当てはめる。使えない区画は自動に落とし、理由を返す（例外にしない）。

    `pool` は登録候補（球団の、能力のある在籍選手）。自動の部分は AI 監督の関数をそのまま使う。
    区画ごとに独立して判断する（オーダーが使えなくても、ローテーションの上書きは生きる）。
    """
    members = members_of(pool)
    fallbacks: list[PlanFallback] = []

    roster: ClubRoster | None = None
    if plan is not None and plan.active_ids is not None:
        try:
            check_active(plan.active_ids, members, limits)
            roster = register_players(pool, plan.active_ids)
        except DomainError as error:
            fallbacks.append(PlanFallback(PlanSection.ACTIVE, str(error)))
    if roster is None:
        roster = choose_active_roster(pool, limits.foreign_roster_limit)
    active_ids = {b.player_id for b in roster.batters} | {p.player_id for p in roster.pitchers}

    batters = {b.player_id: b for b in roster.batters}
    pitchers = {p.player_id: p for p in roster.pitchers}

    lineup: tuple[LineupSlot, ...] | None = None
    if plan is not None and plan.lineup is not None:
        try:
            check_lineup(plan.lineup, members, active_ids, limits)
            lineup = tuple(LineupSlot(batters[c.player_id], c.position) for c in plan.lineup)
        except DomainError as error:
            fallbacks.append(PlanFallback(PlanSection.LINEUP, str(error)))

    rotation: tuple[SimPitcher, ...] | None = None
    if plan is not None and plan.rotation is not None:
        try:
            check_rotation(plan.rotation, members, active_ids)
            rotation = tuple(pitchers[player_id] for player_id in plan.rotation)
        except DomainError as error:
            fallbacks.append(PlanFallback(PlanSection.ROTATION, str(error)))

    closer: SimPitcher | None = None
    if plan is not None and plan.closer_id is not None:
        try:
            check_closer(plan.closer_id, members, active_ids, plan.rotation if rotation is not None else None)
            closer = pitchers[plan.closer_id]
        except DomainError as error:
            fallbacks.append(PlanFallback(PlanSection.CLOSER, str(error)))

    staff = None
    if rotation is not None or closer is not None:
        staff = plan_pitching_staff(roster.pitchers, rotation=rotation, closer=closer)

    # 手動のオーダーが外国人の出場枠を使い切っていると、外国人だけのローテーションでは
    # 枠の中で先発を立てられない（エンジンはオーダーを先に枠に数える）。その日はオーダーを自動に落とす
    if lineup is not None and limits.foreign_game_limit is not None and roster.pitchers:
        lineup_foreign = sum(slot.batter.is_foreign for slot in lineup)
        starters = staff.rotation if staff is not None else plan_pitching_staff(roster.pitchers).rotation
        if lineup_foreign >= limits.foreign_game_limit and all(p.is_foreign for p in starters):
            fallbacks.append(
                PlanFallback(
                    PlanSection.LINEUP,
                    f"オーダーの外国人選手が出場枠（{limits.foreign_game_limit}人）いっぱいで、"
                    "ローテーションに外国人でない投手がいないため、先発を立てられません。",
                )
            )
            lineup = None
    orders = ClubOrders(lineup=lineup, staff=staff) if lineup is not None or staff is not None else None
    return ResolvedClub(roster=roster, orders=orders, fallbacks=tuple(fallbacks))
