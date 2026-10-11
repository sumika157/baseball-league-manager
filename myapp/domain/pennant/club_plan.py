"""球団の編成（`ClubPlan`）。GM が AI 監督の決めた編成を上書きしたものだけを持つ。

球団ごとに1つ。4つの区画（1軍登録・オーダー・ローテーション・抑え）が、それぞれ
**「自動（`None`）」か「手動の値」** を持つ。自動の区画は、持たずに毎日 AI 監督が決める
（`simulation/manager.py`）。持っているのは**選手の id だけ**で、選手の中身（登録位置・外国人かどうか・
能力）は持たない。検査のたびに、その時点のロスターを引数で受け取る
（`Team` 集約に入れないのは、`Team` が実データと共有していて、ロスターの読み込みが通算成績の
集計まで連れてくるため）。

不変条件（上書きを**決めるとき**に検査する。`set_*`）:

- 1軍登録は29人まで。外国人はリーグの登録枠まで。球団の選手だけ。
  打順を組める野手9人・捕手1人以上・投手は下限（`MIN_ACTIVE_PITCHERS`）以上。
  外国人の出場枠の中で自動のスタメン9人を組めること（外国人の投手が先発する分の枠も見込む）
- オーダーは1軍から9人。重複なし。守備位置が9つ（捕・一・二・三・遊・左・中・右・指）をすべて満たし、
  各選手は登録位置が就ける位置だけ（AI と同じ規則）。
  捕手の枠は登録位置が捕手の選手。外国人は試合の出場枠まで
- ローテーションは1軍の投手（重複なし・`MIN_ROTATION_SIZE` 人以上 `ROTATION_SIZE` 人まで）
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

from ..exceptions import DomainError, ForeignPlayerQuotaExceeded, InvalidClubPlan, InvalidWorld
from ..simulation.manager import (
    ACTIVE_ROSTER_SIZE,
    FIELD_SLOTS,
    LINEUP_SIZE,
    MIN_ACTIVE_PITCHERS,
    MIN_ROTATION_SIZE,
    ROTATION_SIZE,
    ClubOrders,
    ClubRoster,
    LineupSlot,
    SimPitcher,
    assign_fielders,
    can_play,
    choose_active_roster,
    plan_pitching_staff,
    register_players,
)
from ..value_objects import FieldingPosition, Position

FP = FieldingPosition

# オーダーが満たす守備位置（8つの守備と指名打者）。並びは画面に出す順（捕から指名打者まで）
LINEUP_POSITION_ORDER: tuple[FieldingPosition, ...] = (
    FP.CATCHER,
    FP.FIRST_BASE,
    FP.SECOND_BASE,
    FP.THIRD_BASE,
    FP.SHORTSTOP,
    FP.LEFT_FIELD,
    FP.CENTER_FIELD,
    FP.RIGHT_FIELD,
    FP.DESIGNATED_HITTER,
)
LINEUP_POSITIONS: frozenset[FieldingPosition] = frozenset(LINEUP_POSITION_ORDER)
MIN_ACTIVE_BATTERS = LINEUP_SIZE


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
    """編成についての知らせ。理由は画面にそのまま出せる日本語。

    `falls_back` が True なら、上書きが使えず**その区画を自動に落とした**こと。False なら区画は落とさず、
    上書きが（あるいは自動の編成が）思ったとおりに効かないことの注意だけ（外国人の抑えが出場枠で投げられない）。"""

    section: PlanSection
    reason: str
    # False なら区画は自動に落とさず、手動のまま残して効かない理由を知らせるだけ（外国人の抑え）
    falls_back: bool = True


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


# --- 守備位置の割り当て ---


def unfilled_position(members: Iterable[ClubMember]) -> FieldingPosition | None:
    """野手では埋められない守備位置（捕・一・二・三・遊・左・中・右）。全部埋められれば None。"""
    return _unfilled_slot_of([m.position for m in members if not m.is_pitcher])


def _unfilled_slot_of(batter_positions: Sequence[Position]) -> FieldingPosition | None:
    owners = assign_fielders(batter_positions, lambda position: position)
    return next((slot for slot in FIELD_SLOTS if slot not in owners), None)


def _position_of(member: ClubMember) -> Position:
    return member.position


def arrange_lineup(
    choices: Sequence[LineupChoice],
    roster: dict[int, ClubMember],
    *,
    bench: Sequence[ClubMember] = (),
    foreign_game_limit: int | None = None,
) -> list[LineupChoice] | None:
    """全員が登録位置の就ける位置になるよう、オーダーの守備位置を割り直す。組めなければ None。

    すでに全員が就けているなら、そのまま返す。就けない選手がいるときは、正しい位置にいる選手は動かさず、
    割り当ての無い選手だけを増加路で動かす。それでも埋まらない位置があれば、控え（`bench`。良い順）から
    その位置に就ける選手を、割り当ての無い選手（打順の後ろから）と入れ替える。外国人は出場枠まで。
    自動のオーダーから手動を始めるとき、AI のオーダーが位置の外の選手を含む場合に使う
    （AI は空いた枠を位置を問わず埋める）。
    """
    players = [roster[c.player_id] for c in choices]
    if all(can_play(m.position, c.position) for m, c in zip(players, choices, strict=True)):
        return list(choices)
    owners = assign_fielders(
        players,
        _position_of,
        keep={c.position: m for c, m in zip(choices, players, strict=True) if c.position is not FP.DESIGNATED_HITTER},
    )
    spares = [b for b in bench if b.player_id not in {m.player_id for m in players} and not b.is_pitcher]
    while len(owners) < len(FIELD_SLOTS):
        seated = {m.player_id for m in owners.values()}
        unseated = [i for i, m in enumerate(players) if m.player_id not in seated]
        swap = None
        for spare in spares:
            for index in reversed(unseated):
                trial = [*players[:index], spare, *players[index + 1 :]]
                if foreign_game_limit is not None and sum(m.is_foreign for m in trial) > foreign_game_limit:
                    continue
                # 空いた位置を直接守れなくても、入れ替えて割り当てが増えるならよい（誰かが動いて空きを埋める）
                trial_owners = assign_fielders(trial, _position_of, keep=owners)
                if len(trial_owners) > len(owners):
                    swap = (spare, trial, trial_owners)
                    break
            if swap is not None:
                break
        if swap is None:
            return None
        spare, players, owners = swap
        spares.remove(spare)
    slot_of = {m.player_id: slot for slot, m in owners.items()}
    return [LineupChoice(m.player_id, slot_of.get(m.player_id, FP.DESIGNATED_HITTER)) for m in players]


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


def playable_shortfall(positions: Iterable[Position], *, subject: str = "1軍") -> str | None:
    """この登録位置の選手たちで試合を組めるか。組めなければ理由（日本語）、組めれば None。

    **試合を組める最小の条件の唯一の出典。** 手動の1軍登録の検査（`check_active`）と、世界の作成での
    名簿の検査（`ensure_roster_playable`）が同じ規則を通る。野手 `MIN_ACTIVE_BATTERS` 人以上・
    捕手1人以上・投手 `MIN_ACTIVE_PITCHERS` 人以上・守備の8枠（捕・一・二・三・遊・左・中・右）を
    `can_play` で埋められること。`subject` は文中で数えている対象（1軍登録か、在籍選手か）。
    """
    registered = list(positions)
    pitchers = sum(position.is_pitcher for position in registered)
    batters = len(registered) - pitchers
    if batters < MIN_ACTIVE_BATTERS or pitchers < MIN_ACTIVE_PITCHERS:
        return (
            f"{subject}には野手{MIN_ACTIVE_BATTERS}人以上と投手{MIN_ACTIVE_PITCHERS}人以上が要ります"
            f"（野手{batters}人・投手{pitchers}人）。"
        )
    if Position.CATCHER not in registered:
        return f"{subject}には捕手が1人以上要ります。"
    gap = _unfilled_slot_of([position for position in registered if not position.is_pitcher])
    if gap is not None:
        return (
            f"{subject}に{gap.full_name}を守れる野手がいないため、オーダーを組めません。"
            "捕手・内野手・外野手を守備位置ぶん（捕1・内4・外3）そろえてください。"
        )
    return None


def lineup_capacity(members: Sequence[ClubMember], foreign_game_limit: int) -> int:
    """外国人の出場枠の中で、スタメンに入れる野手の人数。

    外国人の投手が先発すると枠が1つ減るので、その分も見込む（自動のスタメンは出場枠の中で組む）。
    `LINEUP_SIZE` に満たなければ、自動のスタメンを組めず進行が止まる（`choose_lineup` が InvalidRoster）。
    """
    reserved = 1 if any(m.is_pitcher and m.is_foreign for m in members) else 0
    foreign_batters = sum(m.is_foreign and not m.is_pitcher for m in members)
    batters = sum(not m.is_pitcher for m in members)
    return batters - foreign_batters + min(foreign_batters, max(0, foreign_game_limit - reserved))


def ensure_roster_playable(team_name: str, members: Sequence[ClubMember], *, foreign_game_limit: int | None) -> None:
    """球団の在籍選手で試合を組めること。組めなければ InvalidWorld（世界の作成で、名簿の足りない球団を弾く）。

    `foreign_game_limit` は世界のリーグで最も厳しい出場枠（`strictest_game_limit`。交流戦はどのリーグとも当たる）。
    外国人が多くて枠の中でスタメンを組めない名簿も弾く。
    """
    shortfall = playable_shortfall((m.position for m in members), subject="在籍選手")
    if shortfall is not None:
        raise InvalidWorld(f"{team_name}は、試合を組める名簿ではありません。{shortfall}")
    if foreign_game_limit is not None:
        playable = lineup_capacity(members, foreign_game_limit)
        if playable < LINEUP_SIZE:
            raise InvalidWorld(
                f"{team_name}は、試合を組める名簿ではありません。外国人選手の出場は1試合{foreign_game_limit}人までなので、"
                f"スタメン{LINEUP_SIZE}人を組めません（組めるのは{playable}人）。外国人でない野手が足りません。"
            )


def check_active(player_ids: Sequence[int], roster: dict[int, ClubMember], limits: ClubLimits) -> None:
    """1軍登録として成立するか。成立しなければ DomainError。"""
    members = _members_of_ids(player_ids, roster, "1軍登録")
    if len(members) > limits.active_size:
        raise InvalidClubPlan(f"1軍登録は{limits.active_size}人までです（{len(members)}人）。")
    shortfall = playable_shortfall(m.position for m in members)
    if shortfall is not None:
        raise InvalidClubPlan(shortfall)
    foreign = sum(m.is_foreign for m in members)
    if limits.foreign_roster_limit is not None and foreign > limits.foreign_roster_limit:
        raise ForeignPlayerQuotaExceeded(
            f"外国人選手の1軍登録は{limits.foreign_roster_limit}人までです（{foreign}人）。"
        )
    if limits.foreign_game_limit is not None:
        playable = lineup_capacity(members, limits.foreign_game_limit)
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
        if not can_play(member.position, choice.position):
            raise InvalidClubPlan(
                f"{_label(member)}は、登録位置が{member.position.value}なので{choice.position.full_name}を守れません。"
            )
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
    if len(player_ids) < MIN_ROTATION_SIZE:
        raise InvalidClubPlan(
            f"ローテーションは{MIN_ROTATION_SIZE}人以上必要です（{len(player_ids)}人）。"
            "少ない人数で回すと、先発の登板が偏りすぎます。"
        )
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

    def release(self, player_ids: Collection[int]) -> tuple[ClubPlan, tuple[PlanSection, ...]]:
        """`player_ids`（引退した選手など）を含む区画を自動に戻した編成と、戻した区画を返す。

        **1軍登録を戻すときは、それに依存するオーダー・ローテーション・抑えも戻す**（1軍登録だけが
        自動に戻ると、残りの手動の区画が AI の1軍に照らして使えなくなり、注意が出続けるため）。
        戻した区画はすべて返す（知らせるため）。この編成自体は変えない。
        """
        gone = set(player_ids)
        has_gone = {
            PlanSection.ACTIVE: self.active_ids is not None and bool(gone.intersection(self.active_ids)),
            PlanSection.LINEUP: self.lineup is not None and any(choice.player_id in gone for choice in self.lineup),
            PlanSection.ROTATION: self.rotation is not None and bool(gone.intersection(self.rotation)),
            PlanSection.CLOSER: self.closer_id is not None and self.closer_id in gone,
        }
        releasing_active = has_gone[PlanSection.ACTIVE]
        # 戻すのは、退団者を含む区画と、1軍登録を戻すときに手動で残っている区画
        manual = {
            PlanSection.ACTIVE: self.active_ids is not None,
            PlanSection.LINEUP: self.lineup is not None,
            PlanSection.ROTATION: self.rotation is not None,
            PlanSection.CLOSER: self.closer_id is not None,
        }
        released = tuple(
            section for section in PlanSection if manual[section] and (has_gone[section] or releasing_active)
        )
        return (
            ClubPlan(
                self.team_id,
                None if PlanSection.ACTIVE in released else self.active_ids,
                None if PlanSection.LINEUP in released else self.lineup,
                None if PlanSection.ROTATION in released else self.rotation,
                None if PlanSection.CLOSER in released else self.closer_id,
            ),
            released,
        )


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
        effective = staff if staff is not None else plan_pitching_staff(roster.pitchers)
        full = lineup_foreign >= limits.foreign_game_limit
        if full and all(p.is_foreign for p in effective.rotation):
            fallbacks.append(
                PlanFallback(
                    PlanSection.LINEUP,
                    f"オーダーの外国人選手が出場枠（{limits.foreign_game_limit}人）いっぱいで、"
                    "ローテーションに外国人でない投手がいないため、先発を立てられません。",
                )
            )
            lineup = None
        elif full and effective.closer is not None and effective.closer.is_foreign:
            # オーダーが枠を使い切ると、外国人の抑えは1年中投げられない（黙って投げなくなる）。
            # 区画は外さず（自動の抑えも外国人になりうる）、GM に知らせるだけにする
            source = "手動で指定した" if closer is not None else "自動で選ばれた"
            fallbacks.append(
                PlanFallback(
                    PlanSection.CLOSER,
                    f"オーダーの外国人選手が出場枠（{limits.foreign_game_limit}人）いっぱいなので、"
                    f"{source}外国人の抑え（{effective.closer.name}）は投げられません。"
                    "外国人でない投手を抑えに手動で指定するか、オーダーの外国人を減らしてください。",
                    falls_back=False,
                )
            )
    orders = ClubOrders(lineup=lineup, staff=staff) if lineup is not None or staff is not None else None
    return ResolvedClub(roster=roster, orders=orders, fallbacks=tuple(fallbacks))
