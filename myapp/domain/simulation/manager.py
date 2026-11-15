"""AI 監督。最小限の規則だけを持つ。

| 判断 | 規則 |
| --- | --- |
| 1軍登録 | 能力順に29人。捕手2人以上・投手14人（足りなければ減らす）。外国人はリーグの枠（計・投手・野手）の範囲で |
| スタメン | 総合値の順に守備の枠を埋め、守備力の高い順に重要な位置へ。DH は残りの最強打者。打順は出塁→長打の定型 |
| ローテーション | 先発6人。前回の先発から中5日以上空ける |
| 継投 | スタミナと抑える力から受け持ちの目安を決め、失点が嵩めば早めに代える。抑えはセーブの状況で出す |
| 救援の連投 | 2連投まで、スタミナ55以上は3連投目も（`fatigue.max_consecutive_days`）。疲労が重い救援は後回し |
| 代打 | 7回以降・負けている・控えに明確に上の打者がいる、が揃えば。1試合2人まで |
| 外国人の出場 | ホーム球団のリーグの外国人枠（`ForeignPlayerRules`。計・投手・野手）を守る |

**疲労は保存しない。** 直近の登板の記録（`fatigue.PitchingHistory`）から導く。
疲労が率に効くのはエンジン（`fatigue.py`）。
乱数は呼び出し側から受け取る（`random()` だけを使う）。
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta

from ..exceptions import InvalidRoster
from ..services.decisions import SAVE_LEAD_LIMIT
from ..value_objects import FieldingPosition, ForeignPlayerRules, Handedness, Position
from .fatigue import (
    MID_INNING_GRACE,
    MIN_DAYS_BETWEEN_STARTS,
    FatigueLevel,
    PitchingHistory,
    pregame_fatigue_points,
)
from .randomness import GameRandom, normal, weighted_index
from .ratings import BatterRatings, PitcherRatings

FP = FieldingPosition

ACTIVE_ROSTER_SIZE = 29
ACTIVE_PITCHERS = 14
MIN_ACTIVE_CATCHERS = 2
LINEUP_SIZE = 9
ROTATION_SIZE = 6
# 手動のローテーションの下限（AI の6人より1人少ない5人まで。これを割ると同じ先発に登板が偏りすぎる）
MIN_ROTATION_SIZE = ROTATION_SIZE - 1
PINCH_HITTER_FROM_INNING = 7
# 抑えが登板できる回（セーブがつく状況は9回から）
CLOSER_FROM_INNING = 9
MAX_PINCH_HITTERS = 2
# 代打に出す控えが、打席に立つ選手より打撃の総合値でこれ以上上回ること（「明確に上」）
PINCH_HITTER_MARGIN = 6.0
# 中継ぎの序列の減衰率。上位ほど選ばれやすいが、下位にも出番を回す
BULLPEN_DECAY = 0.85
# 大差（この点差以上）の試合は、序列の低い投手で消化する
BLOWOUT_LEAD = 5
SETUP_PITCHERS = 2
# 1軍の投手の下限。ローテーションの下限・抑え1人・セットアップを賄える人数
MIN_ACTIVE_PITCHERS = MIN_ROTATION_SIZE + 1 + SETUP_PITCHERS

# 先発・救援が受け持つ打者数の目安（スタミナ50のとき）と、スタミナ1点あたりの増減、ばらつき
STARTER_BATTERS = 24.0
STARTER_BATTERS_PER_STAMINA = 0.22
# 抑える力（`pitching_value`）が50を上回る先発には、1点あたりこの人数ぶん長く任せる（エースは長く投げさせる）
STARTER_BATTERS_PER_PITCHING_VALUE = 0.45
STARTER_BATTERS_SD = 3.0
# 救援は1回（3アウトに約4.4人）を投げ切れる人数。これより少ないと、抑えが回の途中で代わってセーブがつかない
RELIEVER_BATTERS = 4.4
RELIEVER_BATTERS_PER_STAMINA = 0.04
RELIEVER_BATTERS_SD = 0.9
STARTER_KNOCKOUT_RUNS = 5
RELIEVER_KNOCKOUT_RUNS = 3

# 野手の総合値で、守備力にかける重み（捕手が最も重い）
_FIELDING_WEIGHT = {
    Position.CATCHER: 0.45,
    Position.INFIELDER: 0.35,
    Position.OUTFIELDER: 0.25,
    Position.DESIGNATED_HITTER: 0.0,
}

_CLASS_SLOTS: tuple[tuple[Position, tuple[FieldingPosition, ...]], ...] = (
    (Position.CATCHER, (FP.CATCHER,)),
    (Position.INFIELDER, (FP.SHORTSTOP, FP.SECOND_BASE, FP.THIRD_BASE, FP.FIRST_BASE)),
    (Position.OUTFIELDER, (FP.CENTER_FIELD, FP.RIGHT_FIELD, FP.LEFT_FIELD)),
)
# 空いた守備位置を埋める順（重要な位置から）
_FILL_PRIORITY = (
    FP.SHORTSTOP,
    FP.CENTER_FIELD,
    FP.SECOND_BASE,
    FP.THIRD_BASE,
    FP.CATCHER,
    FP.RIGHT_FIELD,
    FP.LEFT_FIELD,
    FP.FIRST_BASE,
)
# 登録位置ごとに就ける守備位置。指名打者として登録された選手は一塁・左翼・右翼ならこなす
_PLAYABLE: dict[Position, frozenset[FieldingPosition]] = {
    Position.CATCHER: frozenset({FP.CATCHER}),
    Position.INFIELDER: frozenset({FP.FIRST_BASE, FP.SECOND_BASE, FP.THIRD_BASE, FP.SHORTSTOP}),
    Position.OUTFIELDER: frozenset({FP.LEFT_FIELD, FP.CENTER_FIELD, FP.RIGHT_FIELD}),
    Position.DESIGNATED_HITTER: frozenset({FP.FIRST_BASE, FP.LEFT_FIELD, FP.RIGHT_FIELD}),
}


# 守備に就く8つの枠（指名打者は誰でも就けるので含めない）
FIELD_SLOTS: tuple[FieldingPosition, ...] = (
    FP.CATCHER,
    FP.FIRST_BASE,
    FP.SECOND_BASE,
    FP.THIRD_BASE,
    FP.SHORTSTOP,
    FP.LEFT_FIELD,
    FP.CENTER_FIELD,
    FP.RIGHT_FIELD,
)


@dataclass(frozen=True)
class SimBatter:
    """シミュレーションに出る野手。登録位置（`Position`）と能力を持つ。"""

    player_id: int
    name: str
    position: Position
    ratings: BatterRatings
    # その年の外国人枠で数えるか。国籍やフラグの事実ではない（誰を数えるかは選手を組み立てる側が決める）
    counts_as_foreign: bool = False
    # 投げる手。AI の守備位置の割り振りが、左投げを捕・二・三・遊に就かせないために見る（不明なら制限しない）
    throws: Handedness | None = field(kw_only=True)


@dataclass(frozen=True)
class SimPitcher:
    """シミュレーションに出る投手。"""

    player_id: int
    name: str
    ratings: PitcherRatings
    # その年の外国人枠で数えるか。国籍やフラグの事実ではない（誰を数えるかは選手を組み立てる側が決める）
    counts_as_foreign: bool = False


@dataclass(frozen=True)
class ClubRoster:
    """1球団の選手。1軍登録にするのは `choose_active_roster`。"""

    team_id: int
    name: str
    batters: tuple[SimBatter, ...]
    pitchers: tuple[SimPitcher, ...]


@dataclass(frozen=True)
class LineupSlot:
    """打順の1枠（リストの位置が打順）。誰がどこを守るか。"""

    batter: SimBatter
    position: FieldingPosition


@dataclass(frozen=True)
class PitchingStaff:
    """投手陣の役割。ローテーションと、抑え・中継ぎの序列（良い順）。"""

    rotation: tuple[SimPitcher, ...]
    closer: SimPitcher | None
    setup: tuple[SimPitcher, ...]
    middle: tuple[SimPitcher, ...]

    @property
    def bullpen(self) -> tuple[SimPitcher, ...]:
        return ((self.closer,) if self.closer else ()) + self.setup + self.middle


@dataclass(frozen=True)
class ClubOrders:
    """GM が決めた編成の上書き。`None` の区画は AI 監督が決める（`ClubPlan` から当てはめる）。

    `lineup` の選手は `ClubRoster.batters` の中から選んだものに限る（呼ぶ側が検査する）。
    """

    lineup: tuple[LineupSlot, ...] | None = None
    staff: PitchingStaff | None = None


@dataclass
class ForeignQuota:
    """1試合にそのチームで出た外国人選手（登録位置の並び）と、守る規則。

    規則はその試合のホーム球団のリーグの外国人枠（`ForeignPlayerRules`）。出場者は1軍の中から出るので、
    同じ規則で選んだ1軍なら縛りにならない。交流戦で相手のリーグの規則のほうが厳しいときに効く。
    投手か野手かは `ForeignPlayerRules.counts`（登録位置）で数える。
    """

    rules: ForeignPlayerRules = ForeignPlayerRules.UNLIMITED
    # 出場した外国人選手の登録位置（出た順）
    appeared: list[Position] = field(default_factory=list)

    @property
    def pitchers(self) -> int:
        return ForeignPlayerRules.counts(self.appeared)[0]

    @property
    def batters(self) -> int:
        return ForeignPlayerRules.counts(self.appeared)[1]

    @property
    def batter_room(self) -> int | None:
        """あと何人の外国人の野手を出せるか（None は無制限）。"""
        pitchers, batters = ForeignPlayerRules.counts(self.appeared)
        return self.rules.batter_room(pitchers=pitchers, batters=batters)

    def allows(self, counts_as_foreign: bool, *, position: Position, extra: int = 0) -> bool:
        """登録位置 `position` の選手を（すでに出たぶんに、同じ登録位置の外国人 `extra` 人を加えて）出せるか。"""
        if not counts_as_foreign:
            return True
        return self.rules.has_room_for([*self.appeared, *([position] * extra)], position)

    def register(self, counts_as_foreign: bool, *, position: Position) -> None:
        if counts_as_foreign:
            self.appeared.append(position)


# --- 値 ---


def regular_value(batter: SimBatter) -> float:
    """レギュラーとしての総合値。打撃と守備を登録位置ごとの重みで合わせる。"""
    weight = _FIELDING_WEIGHT[batter.position]
    return (1.0 - weight) * batter.ratings.batting_value + weight * batter.ratings.fielding


def can_play(registered: Position, position: FieldingPosition) -> bool:
    """登録位置の選手が、守備位置に就ける位置か（指名打者の枠は誰でも就ける）。AI と GM の編成で共通の規則。"""
    if position is FP.DESIGNATED_HITTER:
        return True
    return position in _PLAYABLE.get(registered, frozenset())


def assign_fielders[T](
    players: Sequence[T],
    position_of: Callable[[T], Position],
    *,
    keep: Mapping[FieldingPosition, T] | None = None,
) -> dict[FieldingPosition, T]:
    """守備の8つの枠に、`can_play` で就ける選手を割り当てる（最大の割り当て）。

    `keep` は今の割り当て。就ける位置にいる選手は最初にそこへ置き、割り当ての無い選手だけを
    増加路で足す（動くのは必要な人だけ）。就ける選手のいない枠は結果に入らない。
    """
    owners: dict[FieldingPosition, T] = {}
    for slot, player in (keep or {}).items():
        if slot in FIELD_SLOTS and slot not in owners and can_play(position_of(player), slot):
            owners[slot] = player

    def place(player: T, seen: set[FieldingPosition]) -> bool:
        # 先に空いている位置を探す（座っている選手を動かすのは、空きが無いときだけ）
        for slot in FIELD_SLOTS:
            if slot not in owners and can_play(position_of(player), slot):
                owners[slot] = player
                return True
        for slot in FIELD_SLOTS:
            if slot in seen or not can_play(position_of(player), slot):
                continue
            seen.add(slot)
            if place(owners[slot], seen):
                owners[slot] = player
                return True
        return False

    seated = list(owners.values())
    for player in players:
        if not any(player is other for other in seated):
            place(player, set())
    return owners


def unfilled_slot(batters: Sequence[SimBatter]) -> FieldingPosition | None:
    """野手では埋められない守備の枠（最初のひとつ）。全部埋められれば None。"""
    owners = assign_fielders(batters, lambda b: b.position)
    return next((slot for slot in FIELD_SLOTS if slot not in owners), None)


def plays_position(batter: SimBatter, position: FieldingPosition) -> bool:
    """AI の起用で、その選手を守備位置に就かせてよいか。登録位置が就ける位置で、投げる手も合うこと。

    指名打者の枠は誰でも就ける。手動の編成（`can_play`）は投げる手を見ない。
    """
    return can_play(batter.position, position) and position.suits_thrower(batter.throws)


# --- 1軍登録 ---


def choose_active_roster(pool: ClubRoster, *, rules: ForeignPlayerRules) -> ClubRoster:
    """登録候補から1軍29人を選ぶ。捕手2人以上・投手14人。外国人はリーグの外国人枠（`rules`。計・投手・野手）まで。

    能力順に選び、外国人が枠を超えたら能力の低い外国人から外して、同じ区分（投手か野手か）の
    外国人でない選手で埋める（投手の枠・野手の枠・計の枠の順に見る）。候補が足りなければ人数を減らす。
    """
    pitcher_count = min(ACTIVE_PITCHERS, len(pool.pitchers))
    batter_count = min(ACTIVE_ROSTER_SIZE - pitcher_count, len(pool.batters))

    pitchers_by_value = sorted(pool.pitchers, key=_pitcher_roster_value, reverse=True)
    batters_by_value = sorted(pool.batters, key=regular_value, reverse=True)

    catchers = [b for b in batters_by_value if b.position is Position.CATCHER][:MIN_ACTIVE_CATCHERS]
    chosen_batters = list(catchers)
    for batter in batters_by_value:
        if len(chosen_batters) >= batter_count:
            break
        if batter not in chosen_batters:
            chosen_batters.append(batter)
    chosen_pitchers = pitchers_by_value[:pitcher_count]

    chosen_pitchers, chosen_batters = _within_foreign_rules(
        chosen_pitchers, chosen_batters, pitchers_by_value, batters_by_value, rules
    )
    foreign_pitchers = sum(p.counts_as_foreign for p in chosen_pitchers)
    chosen_batters = _with_minimum_catchers(chosen_batters, batters_by_value, rules, foreign_pitchers)
    chosen_batters = _with_fielding_coverage(chosen_batters, batters_by_value, rules, foreign_pitchers)

    return ClubRoster(
        team_id=pool.team_id,
        name=pool.name,
        batters=tuple(sorted(chosen_batters, key=regular_value, reverse=True)),
        pitchers=tuple(sorted(chosen_pitchers, key=_pitcher_roster_value, reverse=True)),
    )


def register_players(pool: ClubRoster, player_ids: Collection[int]) -> ClubRoster:
    """登録候補から、`player_ids` の選手だけを1軍にする（GM が決めた登録。並びは自動の登録と同じ良い順）。"""
    chosen = set(player_ids)
    return ClubRoster(
        team_id=pool.team_id,
        name=pool.name,
        batters=tuple(sorted((b for b in pool.batters if b.player_id in chosen), key=regular_value, reverse=True)),
        pitchers=tuple(
            sorted((p for p in pool.pitchers if p.player_id in chosen), key=_pitcher_roster_value, reverse=True)
        ),
    )


def _pitcher_roster_value(pitcher: SimPitcher) -> float:
    return max(pitcher.ratings.starter_value, pitcher.ratings.pitching_value)


def _within_foreign_rules(
    pitchers: list[SimPitcher],
    batters: list[SimBatter],
    pitchers_by_value: list[SimPitcher],
    batters_by_value: list[SimBatter],
    rules: ForeignPlayerRules,
) -> tuple[list[SimPitcher], list[SimBatter]]:
    """外国人の枠を超えているあいだ、能力の最も低い外国人を、同じ区分の外国人でない次点の選手に替える。

    投手の枠 → 野手の枠 → 計の枠の順に見る。計の枠では、能力の低いほうの区分の外国人から外す。
    """
    pitchers = list(pitchers)
    batters = list(batters)

    def foreign_pitchers() -> list[SimPitcher]:
        return [p for p in pitchers if p.counts_as_foreign]

    def foreign_batters() -> list[SimBatter]:
        return [b for b in batters if b.counts_as_foreign]

    def drop_pitcher(out: SimPitcher) -> None:
        pitchers.remove(out)
        substitute = next((p for p in pitchers_by_value if not p.counts_as_foreign and p not in pitchers), None)
        if substitute is not None:
            pitchers.append(substitute)

    def drop_batter(out: SimBatter) -> None:
        batters.remove(out)
        substitute = next((b for b in batters_by_value if not b.counts_as_foreign and b not in batters), None)
        if substitute is not None:
            batters.append(substitute)

    while rules.pitchers is not None and len(foreign_pitchers()) > rules.pitchers:
        drop_pitcher(min(foreign_pitchers(), key=_pitcher_roster_value))
    while rules.batters is not None and len(foreign_batters()) > rules.batters:
        drop_batter(min(foreign_batters(), key=regular_value))
    while rules.active is not None and len(foreign_pitchers()) + len(foreign_batters()) > rules.active:
        pitcher_out = min(foreign_pitchers(), key=_pitcher_roster_value, default=None)
        batter_out = min(foreign_batters(), key=regular_value, default=None)
        # 価値の単位が違うので、落とす候補は能力の低いほうの区分から（同点なら投手）
        pitcher_loss = _pitcher_roster_value(pitcher_out) if pitcher_out else float("inf")
        batter_loss = regular_value(batter_out) if batter_out else float("inf")
        if pitcher_out is not None and pitcher_loss <= batter_loss:
            drop_pitcher(pitcher_out)
        elif batter_out is not None:
            drop_batter(batter_out)
    return pitchers, batters


def _with_minimum_catchers(
    batters: list[SimBatter],
    batters_by_value: list[SimBatter],
    rules: ForeignPlayerRules,
    foreign_pitchers: int = 0,
) -> list[SimBatter]:
    """捕手が足りなければ、控えの捕手を入れて、捕手でない最も低い野手と替える。

    外国人の枠（計と野手）を超えないように、外国人の捕手は枠に空きがあるときだけ入れる。
    `foreign_pitchers` は1軍に入れる外国人の投手の数。
    """
    batters = list(batters)
    while sum(b.position is Position.CATCHER for b in batters) < MIN_ACTIVE_CATCHERS:
        foreign_batters = sum(b.counts_as_foreign for b in batters)
        spare = next(
            (
                b
                for b in batters_by_value
                if b.position is Position.CATCHER
                and b not in batters
                and (
                    not b.counts_as_foreign
                    or rules.has_room(pitchers=foreign_pitchers, batters=foreign_batters, is_pitcher=False)
                )
            ),
            None,
        )
        replaceable = [b for b in batters if b.position is not Position.CATCHER]
        if spare is None or not replaceable:
            break
        batters.remove(min(replaceable, key=regular_value))
        batters.append(spare)
    return batters


def _with_fielding_coverage(
    batters: list[SimBatter],
    batters_by_value: list[SimBatter],
    rules: ForeignPlayerRules,
    foreign_pitchers: int = 0,
) -> list[SimBatter]:
    """守備の8つの枠を野手で埋められなければ、足りない位置を守れる控えを入れて、替えても埋まる数が増える野手と替える。

    能力順に選ぶと、外野手が少ないときなどに手動のオーダー（登録位置が就ける位置だけ）を組めない1軍になる。
    埋まっている1軍は変えない。在籍選手を全員入れても埋まらないときは、今の結果のまま。
    外国人の枠は計（投手を含む）と野手の両方で数える。
    """
    batters = list(batters)

    def covered(group: list[SimBatter]) -> int:
        return len(assign_fielders(group, lambda b: b.position))

    while covered(batters) < len(FIELD_SLOTS):
        base = covered(batters)
        swapped = False
        for spare in (b for b in batters_by_value if b not in batters):
            for out in sorted(batters, key=regular_value):
                trial = [b for b in batters if b is not out] + [spare]
                if (
                    rules.violation(pitchers=foreign_pitchers, batters=sum(b.counts_as_foreign for b in trial))
                    is not None
                ):
                    continue
                if sum(b.position is Position.CATCHER for b in trial) < min(
                    MIN_ACTIVE_CATCHERS, sum(b.position is Position.CATCHER for b in batters)
                ):
                    continue
                if covered(trial) > base:
                    batters = trial
                    swapped = True
                    break
            if swapped:
                break
        if not swapped:
            break
    return batters


# --- スタメン ---


def choose_lineup(batters: Sequence[SimBatter], quota: ForeignQuota) -> list[LineupSlot]:
    """スタメンを決める。リストの位置が打順。

    1. 登録位置に合う枠（捕手1・内野4・外野3）を、総合値の高い順に埋める。内野・外野の中では
       守備力の高い順に重要な位置（遊・二・三・一、中・右・左）へ。
    2. 枠が空いたら（外野手が足りないなど）、残りの打撃の良い選手のうち守備力の高い順に空きへ回す。
    3. 指名打者は、残りの最強打者。
    4. 打順は 1・2番が出塁、3番が総合、4・5番が長打、残りは総合の順。

    **左投げは捕・二・三・遊に置かない**（`FieldingPosition.allows_left_handed_thrower`）。
    - 手順1: 左投げの就ける枠（一塁など）の数を超える左投げは枠の候補から外し、座れる選手だけを座らせる。
      外れた選手は、残りの選手として手順2・3に回る。
    - 手順2: 空いた枠に、投げる手の合う選手を守備力の順に当てる。合う選手が足りない枠があれば、
      外国人枠の数え方を戻して、枠ごとに選び直す（左投げの就けない枠を先に。座れなかった選手に
      外国人枠を使わせない）。
    - **規則を曲げるのは、外国人枠の範囲に投げる手の合う選手がもう1人もいないときだけ**
      （枠が埋まらないと試合が組めず、ペナントの進行が止まるため）。
    """
    if len(batters) < LINEUP_SIZE:
        raise InvalidRoster(f"打順を組むには野手が{LINEUP_SIZE}人要ります（{len(batters)}人）。")

    foreign_left = quota.batter_room

    def take(pool: list[SimBatter], count: int) -> list[SimBatter]:
        nonlocal foreign_left
        picked: list[SimBatter] = []
        for batter in pool:
            if len(picked) >= count:
                break
            if batter.counts_as_foreign and foreign_left is not None:
                if foreign_left <= 0:
                    continue
                foreign_left -= 1
            picked.append(batter)
        return picked

    chosen: dict[FieldingPosition, SimBatter] = {}
    used: set[int] = set()
    for registered, slots in _CLASS_SLOTS:
        pool = _without_unseatable_lefties(
            sorted(
                (b for b in batters if b.position is registered and b.player_id not in used),
                key=regular_value,
                reverse=True,
            ),
            slots,
        )
        group = take(pool, len(slots))
        used.update(b.player_id for b in group)
        seated, _ = _seat(sorted(group, key=lambda b: -b.ratings.fielding), slots)
        chosen.update(seated)

    # 登録位置に合う枠で左投げを座らせられなかった分は、空いた枠として下で埋め直す。
    # 座れなかった選手は枠から外れるだけで、残りの選手（rest）として一塁・指名打者などに回る
    seated_ids = {b.player_id for b in chosen.values()}
    used = {player_id for player_id in used if player_id in seated_ids}
    open_slots = [slot for slot in _FILL_PRIORITY if slot not in chosen]
    rest = sorted((b for b in batters if b.player_id not in used), key=lambda b: -b.ratings.batting_value)
    foreign_before_fill = foreign_left
    extra = take(rest, len(open_slots) + 1)
    if len(extra) < len(open_slots) + 1:
        raise InvalidRoster("外国人枠の範囲では、打順を組める野手が足りません。")
    designated_hitter = extra[0]
    fillers = sorted(extra[1:], key=lambda b: -b.ratings.fielding)
    filled, _ = _seat(fillers, open_slots)
    if len(filled) < len(open_slots):
        # 投げる手の合う選手が足りない枠がある（左投げしか残っていない）。外国人枠の数え方を
        # 戻して、枠ごとに選び直す（座れなかった選手に枠を使わせない）
        foreign_left = foreign_before_fill
        pool = list(rest)

        def claim(candidates: list[SimBatter]) -> SimBatter | None:
            for candidate in candidates:
                if take([candidate], 1):
                    pool.remove(candidate)
                    return candidate
            return None

        filled = {}
        for slot in sorted(open_slots, key=lambda s: s.allows_left_handed_thrower):  # 左投げの就けない枠を先に
            by_fielding = sorted(pool, key=lambda b: -b.ratings.fielding)
            pick = claim([b for b in by_fielding if slot.suits_thrower(b.throws)]) or claim(by_fielding)
            if pick is None:
                raise InvalidRoster("外国人枠の範囲では、打順を組める野手が足りません。")
            filled[slot] = pick
        picked_dh = claim(sorted(pool, key=lambda b: -b.ratings.batting_value))
        if picked_dh is None:
            raise InvalidRoster("外国人枠の範囲では、打順を組める野手が足りません。")
        designated_hitter = picked_dh
    chosen.update(filled)
    chosen[FP.DESIGNATED_HITTER] = designated_hitter

    return _batting_order([LineupSlot(batter, position) for position, batter in chosen.items()])


def _without_unseatable_lefties(pool: list[SimBatter], slots: Sequence[FieldingPosition]) -> list[SimBatter]:
    """左投げが就ける枠（`allows_left_handed_thrower`）の数を超える左投げを、候補から外す（並びは保つ）。

    外した選手は枠に入れないだけで、残りの選手として一塁・指名打者などに回る。
    """
    capacity = sum(slot.allows_left_handed_thrower for slot in slots)
    kept: list[SimBatter] = []
    for batter in pool:
        if batter.throws is Handedness.LEFT and capacity <= sum(b.throws is Handedness.LEFT for b in kept):
            continue
        kept.append(batter)
    return kept


def _seat(
    players: Sequence[SimBatter], slots: Sequence[FieldingPosition]
) -> tuple[dict[FieldingPosition, SimBatter], list[SimBatter]]:
    """枠の順に、残っている選手のうち先頭の（投げる手の合う）選手を座らせる。座れなかった選手も返す。"""
    remaining = list(players)
    seated: dict[FieldingPosition, SimBatter] = {}
    for slot in slots:
        pick = next((b for b in remaining if slot.suits_thrower(b.throws)), None)
        if pick is not None:
            seated[slot] = pick
            remaining.remove(pick)
    return seated, remaining


def _batting_order(slots: list[LineupSlot]) -> list[LineupSlot]:
    """打順の定型。1・2番に出塁、3番に総合、4・5番に長打、6〜9番は総合の順。"""
    remaining = list(slots)
    order: list[LineupSlot] = []

    def pick(key: Callable[[LineupSlot], float]) -> None:
        best = max(remaining, key=key)
        remaining.remove(best)
        order.append(best)

    pick(lambda s: s.batter.ratings.on_base_value + 0.2 * s.batter.ratings.speed)
    pick(lambda s: s.batter.ratings.on_base_value)
    pick(lambda s: s.batter.ratings.batting_value)
    pick(lambda s: s.batter.ratings.slugging_value)
    pick(lambda s: s.batter.ratings.slugging_value)
    order.extend(sorted(remaining, key=lambda s: -s.batter.ratings.batting_value))
    return order


# --- 投手陣 ---


def plan_pitching_staff(
    pitchers: Sequence[SimPitcher],
    *,
    rotation: Sequence[SimPitcher] | None = None,
    closer: SimPitcher | None = None,
) -> PitchingStaff:
    """ローテーション6人と、抑え・中継ぎの序列を決める。

    `rotation` / `closer` を渡すと、その区画は渡された投手で固定し、残りを AI が決める
    （`ClubPlan` の上書き）。渡さなければ全部 AI が決める。固定した抑えはローテーションから外す。
    """
    if not pitchers:
        raise InvalidRoster("投手がいません。")
    closer_id = closer.player_id if closer is not None else None
    if rotation is None:
        by_starter = sorted(
            (p for p in pitchers if p.player_id != closer_id), key=lambda p: p.ratings.starter_value, reverse=True
        )
        rotation = by_starter[:ROTATION_SIZE]
    in_rotation = {p.player_id for p in rotation}
    relievers = sorted(
        (p for p in pitchers if p.player_id not in in_rotation and p.player_id != closer_id),
        key=lambda p: p.ratings.pitching_value,
        reverse=True,
    )
    if closer is None:
        closer = relievers[0] if relievers else None
        relievers = relievers[1:]
    return PitchingStaff(
        rotation=tuple(rotation),
        closer=closer,
        setup=tuple(relievers[:SETUP_PITCHERS]),
        middle=tuple(relievers[SETUP_PITCHERS:]),
    )


def choose_starter(staff: PitchingStaff, history: PitchingHistory, today: date, quota: ForeignQuota) -> SimPitcher:
    """先発を決める。中5日以上空いた先発のうち、最も長く休んでいる投手。

    全員が空いていなければ、休養の最も長い投手に任せる（試合を組めないよりよい）。
    """
    candidates = [p for p in staff.rotation if quota.allows(p.counts_as_foreign, position=Position.PITCHER)] or list(
        staff.rotation
    )

    def rest_days(pitcher: SimPitcher) -> int:
        days = history.days_since_start(pitcher.player_id, today)
        return 1000 if days is None else days

    rested = [
        p
        for p in candidates
        if rest_days(p) >= MIN_DAYS_BETWEEN_STARTS and not history.pitched_on(p.player_id, today - timedelta(days=1))
    ]
    # 誰も休養が足りないときは、せめて昨日投げていない投手から
    pool = (
        rested
        or [p for p in candidates if not history.pitched_on(p.player_id, today - timedelta(days=1))]
        or candidates
    )
    # 同じ休養日数なら、ローテーションの序列が上の投手を先にする
    return max(pool, key=lambda p: (rest_days(p), -staff.rotation.index(p)))


def available_relievers(
    staff: PitchingStaff,
    used_ids: set[int],
    history: PitchingHistory,
    today: date,
    quota: ForeignQuota,
) -> list[SimPitcher]:
    """今日投げられる救援。この試合で投げていない・連投の上限（スタミナで決まる）に達していない・
    外国人の枠に収まる投手。

    **疲労が「重い」投手は、ほかに投げられる投手がいる限り外す**（抑えも同じ。抑えが重ければ、その回は
    抑えを出さない）。「軽い」は普段どおり残す。全員が重ければ重い投手から選ぶ（続投で目安を超えるより良い）。

    ブルペンが編成上いない小さなロスターに限り、まだ投げていない先発陣から出す。ブルペンがいて全員が
    使えないときは空を返し、呼び出し側は今の投手を続投させる（先発陣から出すと、中5日と連投の規則が崩れる）。
    """

    def usable(pitcher: SimPitcher) -> bool:
        return (
            pitcher.player_id not in used_ids
            and quota.allows(pitcher.counts_as_foreign, position=Position.PITCHER)
            and history.can_pitch_on(pitcher.player_id, today, stamina=pitcher.ratings.stamina)
        )

    # 全員が使えない日は空のまま返す（続投する）。先発陣から出すと、ローテーションと連投の規則が崩れる
    usable_ones = [p for p in (staff.bullpen or staff.rotation) if usable(p)]
    fresh_enough = [p for p in usable_ones if relief_fatigue(history, p, today) is not FatigueLevel.HEAVY]
    return fresh_enough or usable_ones


def relief_fatigue(history: PitchingHistory, pitcher: SimPitcher, today: date) -> FatigueLevel:
    """救援として今日投げるときの疲労の区分（登板前の点から。エンジンが率に効かせる点と同じ式）。"""
    points = pregame_fatigue_points(
        history.workload(pitcher.player_id, today), as_starter=False, stamina=pitcher.ratings.stamina
    )
    return FatigueLevel.from_points(points)


def starter_batter_target(rng: GameRandom, pitcher: SimPitcher) -> int:
    """先発が受け持つ打者数の目安。スタミナと抑える力で決まり、試合ごとにばらつく。"""
    mean = (
        STARTER_BATTERS
        + STARTER_BATTERS_PER_STAMINA * (pitcher.ratings.stamina - 50)
        + STARTER_BATTERS_PER_PITCHING_VALUE * (pitcher.ratings.pitching_value - 50)
    )
    return max(12, round(normal(rng, mean, STARTER_BATTERS_SD)))


def reliever_batter_target(rng: GameRandom, pitcher: SimPitcher) -> int:
    """救援が受け持つ打者数の目安。"""
    mean = RELIEVER_BATTERS + RELIEVER_BATTERS_PER_STAMINA * (pitcher.ratings.stamina - 50)
    return max(1, round(normal(rng, mean, RELIEVER_BATTERS_SD)))


def should_change_pitcher(
    *, is_starter: bool, faced: int, target: int, runs_allowed: int, at_inning_start: bool
) -> bool:
    """今の投手を代えるか。受け持ちの目安に達したら、失点が嵩んでいればそれより早く。

    回の区切りで代えたいので、回の途中は目安を少し超えてから（`MID_INNING_GRACE`）。
    大量に失点したら回の途中でも代える。
    """
    knockout = STARTER_KNOCKOUT_RUNS if is_starter else RELIEVER_KNOCKOUT_RUNS
    if runs_allowed >= knockout and faced >= (9 if is_starter else 1):
        return True
    effective = target
    if is_starter:
        if runs_allowed >= 4:
            effective -= 4
        elif runs_allowed == 3:
            effective -= 2
    if faced < effective:
        return False
    return at_inning_start or faced >= effective + MID_INNING_GRACE


def closer_should_enter(staff: PitchingStaff, current: SimPitcher, *, inning: int, lead: int) -> bool:
    """9回以降、セーブのつく点差でリードしている回の頭は、抑えに代える（今の投手の目安に関わらず）。

    抑えを固定して出さないと、セーブが数人に分散して「抑えが年30セーブ」という実際の形にならない。
    """
    return (
        staff.closer is not None
        and current.player_id != staff.closer.player_id
        and inning >= CLOSER_FROM_INNING
        and 0 < lead <= SAVE_LEAD_LIMIT
    )


def pick_reliever(
    rng: GameRandom,
    staff: PitchingStaff,
    available: Sequence[SimPitcher],
    *,
    inning: int,
    lead: int,
    is_home: bool,
) -> SimPitcher | None:
    """次に投げる救援。`lead` は守備側から見た点差（正なら勝っている）。

    - 9回以降でセーブのつく点差（`SAVE_LEAD_LIMIT` 以内のリード）なら抑え。ホームの同点の9回裏以降も抑え
    - 7回以降のセーブ状況、または接戦なら、抑え以外で最も良い投手
    - 大差なら序列の低い投手で消化する
    - それ以外は中継ぎの序列に沿って、上位ほど選ばれやすく
    """
    if not available:
        return None
    pool = list(available)
    closer = staff.closer

    in_save_lead = 0 < lead <= SAVE_LEAD_LIMIT
    if closer in pool and inning >= 9 and (in_save_lead or (lead == 0 and is_home)):
        return closer

    non_closer = [p for p in pool if p is not closer] or pool
    ordered = sorted(non_closer, key=lambda p: p.ratings.pitching_value, reverse=True)
    if (inning >= 7 and in_save_lead) or (inning >= 7 and abs(lead) <= 1):
        return ordered[0]
    if abs(lead) >= BLOWOUT_LEAD:
        return ordered[-1]

    # 序列（中継ぎの並び）に沿って減衰させる重み
    sequence = [p for p in staff.bullpen if p in ordered] or ordered
    weights = [BULLPEN_DECAY**index for index in range(len(sequence))]
    return sequence[weighted_index(rng, weights)]


# --- 代打 ---


@dataclass(frozen=True)
class PinchHit:
    """代打の決定。

    `fielding_position` は代打に出る選手の行に付ける位置。守備位置を継げる選手なら、その位置を
    継いで（次の守備からそこに就く）、`replacement` は無い。継げない選手なら `PINCH_HITTER`
    とし、`replacement`（その位置を守れる控え）が次の守備から入る。
    """

    batter: SimBatter
    fielding_position: FieldingPosition
    replacement: SimBatter | None = None


@dataclass
class PinchHitSituation:
    """代打の判断に必要な状況。"""

    inning: int
    lead: int  # 攻撃側から見た点差（負なら負けている）
    pinch_hitters_used: int
    current: SimBatter
    position: FieldingPosition
    bench: Sequence[SimBatter] = field(default_factory=tuple)
    quota: ForeignQuota = field(default_factory=ForeignQuota)


def choose_pinch_hitter(situation: PinchHitSituation) -> PinchHit | None:
    """代打を出すか。7回以降・負けている・控えに明確に上の打者がいる、の3つが揃ったときだけ。"""
    if (
        situation.inning < PINCH_HITTER_FROM_INNING
        or situation.lead >= 0
        or situation.pinch_hitters_used >= MAX_PINCH_HITTERS
    ):
        return None

    current_value = situation.current.ratings.batting_value
    by_batting = sorted(situation.bench, key=lambda b: -b.ratings.batting_value)
    by_regular = sorted(situation.bench, key=regular_value, reverse=True)
    for candidate in by_batting:
        if candidate.ratings.batting_value - current_value < PINCH_HITTER_MARGIN:
            break  # これより後の控えはさらに劣る
        if not situation.quota.allows(candidate.counts_as_foreign, position=candidate.position):
            continue
        if plays_position(candidate, situation.position):
            return PinchHit(candidate, situation.position)
        replacement = next(
            (
                other
                for other in by_regular
                if other is not candidate
                and plays_position(other, situation.position)
                and situation.quota.allows(
                    other.counts_as_foreign, position=other.position, extra=int(candidate.counts_as_foreign)
                )
            ),
            None,
        )
        if replacement is not None:
            return PinchHit(candidate, FP.PINCH_HITTER, replacement)
        if situation.position is not FP.CATCHER and situation.position.suits_thrower(candidate.throws):
            return PinchHit(candidate, situation.position)  # 守備位置は慣れないが、そのまま守る
    return None
