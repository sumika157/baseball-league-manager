"""ドラフト会議（本指名）。**純粋な関数と値**で、DB にも Django にも触れない。

会議は世界で1回、全リーグの球団が同じ会議で、その年の候補（`ProspectPool`）を取り合う。指名の順番は世界の規則の
戦略（`draft_order.py`）が決め、ここは規則を知らない。

- **候補**は会議の年ごとに、世界のシード・年・世界の全選手の名前から決定的に作る（`draw_pool`。保存しない）
- 候補の本当の能力は見せない。各球団は**スカウトの推定**（`scouting_report`。項目ごとに誤差を足して5刻み・成長型なし）
  で見比べる。誤差は「世界・年・候補・見る球団」で決まるので、球団ごとに見立てが違い、全球団が同じ1人に集まらない
- 会議の状態（`DraftState`）は、行動（入札・指名・選択終了。`DraftAction`）の並びと設定（規則・人数の幅・候補）から
  **導く**。「いまの巡」「次の番」は持たない（同じ行動の並びなら、何度作り直しても同じ状態になる）
- 入札の巡は、入札がそろったところで抽選まで解く（重複は1球団だけ当たり、外れた球団は同じ巡で再入札）。
  抽選の乱数は `game_seed(世界のシード, 年, "draft-lottery-main-<巡>-<回>-<候補>")`
- AI の指名（`ai_action`）は、自分のスカウトの推定の**将来値**（`future_value`）に、位置の残りの重みを足した評価が
  最大の候補。位置は、その球団の新人の位置の計画（`draft.plan_team_draft`。今までの自動ドラフトと同じ）の中から選ぶ
- 位置の候補が尽きたら、その場で1人足す（`ProspectPool.with_extra`。締める処理を失敗させない）

GM の指名（D3。#198）は同じ `DraftState.apply` を通る。会議を始めなかった年は、締める処理が `auto_complete` で
全球団を AI として最後まで進める。外国人は会議の外（`draft.club_draftees`）。
"""

from __future__ import annotations

import bisect
import hashlib
import math
import struct
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from enum import Enum
from functools import cache

from ..exceptions import InvalidDraftAction
from ..simulation.randomness import GameRandom, game_seed, make_random, weighted_index
from ..simulation.ratings import RATING_MAX, RATING_MIN, BatterRatings, GrowthType, PitcherRatings
from ..simulation.samples import BATTER_MEAN, PITCHER_MEAN, mean_ratings
from ..value_objects import DraftPick, Position
from ..virtual_players import generator
from ..virtual_players.generator import AmateurPath
from .aging import BATTER_CURVES, PITCHER_CURVES, expected_change
from .draft import (
    MAX_DRAFTEES,
    MIN_BY_POSITION,
    Draftee,
    DraftRoute,
    DraftTeam,
    Rookie,
    TeamDraftPlan,
    club_draftees,
    draw_rookie,
    plan_team_draft,
)
from .draft_order import DraftOrderRule, DraftOrderStrategy, DraftStandings, strategy_for
from .initial_ratings import SEASON_START_MONTH
from .offseason_player import Ratings

# 候補の人数（指名の枠1つあたり）。位置ごとに、全球団がその位置に計画した指名の数 × この数の候補を作る。
# 会議が候補の上から取る割合（約4人に1人）を、指名の人数と位置の偏りによらず一定にする（帯の下の球団が
# 投手を10人ずつ取る年も、新人の分布が変わらない）
POOL_PER_PICK = 4
# 位置ごとの候補の最少（どの球団も計画していない位置にも、GM が選べる候補を置く。D3）
POOL_MIN_PER_POSITION = 4
# 候補の経路の割合と、能力の平均のずらし（経路の平均 `DraftRoute.rating_mean` からの点）。
# **指名された日本人の新人**の錨の前の平均が経路ごとに今までの値（35/40/43）、経路の割合が今までの割合（30/50/20）、
# 標準偏差が今までの値（6）に近くなるよう、定常状態の実測で決めた値（`test_offseason_population.py` が見張る。
# 実測は Wiki「ペナントモードの仕組み」の「実測値」）。会議は候補の上の方から取るので、候補の平均は新人より低い
POOL_ROUTE_WEIGHTS = {AmateurPath.HIGH_SCHOOL: 12.5, AmateurPath.UNIVERSITY: 79.5, AmateurPath.CORPORATE: 59.5}
# 候補の能力の標準偏差（新人の `prospects.PROSPECT_RATING_SD` 6 より広い。会議が上から取ると散らばりが狭くなるため）
POOL_RATING_SD = 6.75
# 投手の候補だけに足すずらし（投手は見比べる項目が3つで野手の5つより総合が散らばり、上から取ると野手より高く出るため）
POOL_PITCHER_SHIFT = -1.4
POOL_RATING_SHIFT = {AmateurPath.HIGH_SCHOOL: -0.1, AmateurPath.UNIVERSITY: -4.3, AmateurPath.CORPORATE: -5.4}
# スカウトの推定の誤差（項目ごとの標準偏差。点）と、推定を丸める刻み。誤差の大きさは球団ごとに変えられる形にしておく
# （#208 のスカウトへの投資でつなぐ。`ScoutingAccuracy`）
SCOUT_SD = 4.0
SCOUT_STEP = 5
# AI の評価: 将来値 + NEED_WEIGHT × その位置の残りの人数（計画した位置のうち、まだ指名していない数）
NEED_WEIGHT = 2.0
# 将来値は、この年齢になるまでの普通型の伸び（`aging.expected_change` の積み上げ）を足した総合値
PEAK_AGE = 27


class DraftActionKind(Enum):
    """会議の行動の種類。値は画面に出す名前。"""

    BID = "入札"
    PICK = "指名"
    PASS = "選択終了"


@dataclass(frozen=True)
class DraftAction:
    """会議の行動ひとつ。巡・入札の回は行動の並びから導く（持たない）。

    `extra_position` は、位置の候補が尽きたときにその場で足した候補（`prospect_key` は足す前の候補の数）の位置。
    """

    kind: DraftActionKind
    team_id: int
    prospect_key: int | None = None
    extra_position: Position | None = None


@dataclass(frozen=True)
class Prospect:
    """候補ひとり。`key` は候補の番号（会議の中で一意）、`age` は入団の年の4月1日時点の満年齢。

    能力は錨の前で、本当の値（球団にはスカウトの推定 `scouting_report` しか見せない）。
    """

    key: int
    rookie: Rookie
    age: int

    @property
    def position(self) -> Position:
        return self.rookie.position

    @property
    def route(self) -> DraftRoute:
        return self.rookie.route


@dataclass(frozen=True)
class ProspectPool:
    """その年の候補。`taken_names` は世界の選手と候補の名前（足す候補の名前が重ならないように）。"""

    prospects: tuple[Prospect, ...]
    taken_names: frozenset[str]
    world_seed: int
    year: int

    def __len__(self) -> int:
        return len(self.prospects)

    def get(self, key: int) -> Prospect:
        if not 0 <= key < len(self.prospects):
            raise InvalidDraftAction("その選手は候補にいません。")
        return self.prospects[key]

    @property
    def signature(self) -> str:
        """候補の指紋（コードが変わって候補が変わったことを検出する。D3 で会議の行に写す）。"""
        digest = hashlib.blake2b(digest_size=16)
        for prospect in self.prospects:
            digest.update(f"{prospect.key}:{prospect.rookie.name}:{prospect.rookie.ratings!r};".encode())
        return digest.hexdigest()

    def with_extra(self, position: Position) -> ProspectPool:
        """位置 `position` の候補を1人足した候補（乱数は `draft-extra-<番号>`。同じ並びなら同じ人）。"""
        key = len(self.prospects)
        rng = make_random(game_seed(self.world_seed, self.year, f"draft-extra-{key}"))
        names = set(self.taken_names)
        prospect = _draw_prospect(rng, key, position, path=None, year=self.year, used_names=names)
        return replace(self, prospects=(*self.prospects, prospect), taken_names=frozenset(names))


def _draw_prospect(
    rng: GameRandom, key: int, position: Position, *, path: AmateurPath | None, year: int, used_names: set[str]
) -> Prospect:
    if path is None:
        paths = list(POOL_ROUTE_WEIGHTS)
        path = paths[weighted_index(rng, [POOL_ROUTE_WEIGHTS[item] for item in paths])]
    rookie = draw_rookie(
        rng,
        position,
        foreign=False,
        year=year,
        used_names=used_names,
        path=path,
        rating_shift=POOL_RATING_SHIFT[path] + (POOL_PITCHER_SHIFT if position.is_pitcher else 0.0),
        rating_sd=POOL_RATING_SD,
    )
    age = rookie.profile.age(date(year + 1, SEASON_START_MONTH, 1))
    assert age is not None, "候補は生年月日を持つ"
    return Prospect(key=key, rookie=rookie, age=age)


def draw_pool(
    planned: Mapping[Position, int], *, world_seed: int, year: int, used_names: Collection[str]
) -> ProspectPool:
    """締める年 `year` の会議の候補。乱数は `draft-pool`。

    位置ごとの人数は、全球団がその位置に計画した指名の数（`planned`）× `POOL_PER_PICK`
    （最少 `POOL_MIN_PER_POSITION`。指名が1つも無い会議は0人）。経路は `POOL_ROUTE_WEIGHTS` で1人ずつ引く。
    名前は `used_names`（世界の全選手）と重ならない。
    """
    rng = make_random(game_seed(world_seed, year, "draft-pool"))
    names = set(used_names)
    total = sum(planned.values())
    positions = [
        position
        for position in generator.POSITION_RATIOS
        for _ in range(max(POOL_PER_PICK * planned.get(position, 0), POOL_MIN_PER_POSITION) if total else 0)
    ]
    prospects = tuple(
        _draw_prospect(rng, key, position, path=None, year=year, used_names=names)
        for key, position in enumerate(positions)
    )
    return ProspectPool(prospects=prospects, taken_names=frozenset(names), world_seed=world_seed, year=year)


@dataclass(frozen=True)
class ScoutingAccuracy:
    """スカウトの推定の精度（誤差の標準偏差）。いまは全球団 `SCOUT_SD`。#208 で球団ごとの値をここに渡す。"""

    sd: float = SCOUT_SD


_NOISE_WORDS = struct.Struct(">16I")
_TWO_PI = 2.0 * math.pi


def _scout_noise(world_seed: int, year: int, team_id: int, key: int, count: int) -> list[float]:
    """推定の誤差（標準正規。Box–Muller 法）を `count` 個（8個まで）。種は「世界・年・候補・見る球団」。

    乱数源を作らずに hash から引く（1会議で 球団数 × 候補 ＝ 8リーグで約4.6万回引くため）。
    """
    digest = hashlib.blake2b(f"{world_seed}:{year}:draft-scout-{team_id}-{key}".encode(), digest_size=64).digest()
    words = _NOISE_WORDS.unpack(digest)
    scale = 2.0**-32
    return [
        math.sqrt(-2.0 * math.log(1.0 - words[2 * index] * scale)) * math.cos(_TWO_PI * words[2 * index + 1] * scale)
        for index in range(count)
    ]


def _rounded(value: float) -> int:
    return min(RATING_MAX, max(RATING_MIN, SCOUT_STEP * round(value / SCOUT_STEP)))


def scouting_report(
    prospect: Prospect,
    team_id: int,
    *,
    world_seed: int,
    year: int,
    accuracy: ScoutingAccuracy = ScoutingAccuracy(),  # noqa: B008  不変の値（frozen）なので既定値に置いてよい
) -> Ratings:
    """球団 `team_id` のスカウトが見た候補の能力。項目ごとに真の値 + N(0, 精度) を5刻みに丸める。

    成長型は普通（見せない）。
    """
    ratings = prospect.rookie.ratings
    labels = ratings.LABELS
    noise = _scout_noise(world_seed, year, team_id, prospect.key, len(labels))
    values = {
        name: _rounded(getattr(ratings, name) + accuracy.sd * error) for name, error in zip(labels, noise, strict=True)
    }
    # 成長型は既定の普通（見せない）
    if isinstance(ratings, PitcherRatings):
        return PitcherRatings(
            stuff=values["stuff"],
            control=values["control"],
            home_run_avoidance=values["home_run_avoidance"],
            stamina=values["stamina"],
        )
    return BatterRatings(
        contact=values["contact"],
        power=values["power"],
        eye=values["eye"],
        speed=values["speed"],
        fielding=values["fielding"],
    )


def scouting_value(ratings: Ratings) -> float:
    """AI が候補を見比べる総合: スタミナを除く項目の平均（野手は守備・走力も、投手は球威・制球・一発回避）。

    打撃だけ（`overall_value`）で選ぶと、指名された新人の守備・走力だけが候補の平均のまま残り、項目の間の
    つり合いが崩れる（錨の前の分布がずれる）。項目をそろえて見るので、合わせ込み（`POOL_RATING_SHIFT`）は全項目に同じだけ効く。
    """
    names = _judged_items(ratings)
    return sum(getattr(ratings, name) for name in names) / len(names)


def _judged_items(ratings: Ratings) -> list[str]:
    """`scouting_value` が見る項目（スタミナを除く）。"""
    return [name for name in ratings.LABELS if name != "stamina"]


@cache
def growth_to_peak(age: int, position: Position) -> float:
    """`age` 歳の新人が `PEAK_AGE` 歳になるまでに見込める総合（`scouting_value`）の伸び。

    普通型・ぶれなしで、平均的な能力で測る。
    """
    pitcher = position.is_pitcher
    current: Ratings = mean_ratings(
        mean=PITCHER_MEAN if pitcher else BATTER_MEAN, position=position, stamina_mean=45.0
    )
    curves = PITCHER_CURVES if pitcher else BATTER_CURVES
    names = _judged_items(current)
    values = {name: float(getattr(current, name)) for name in names}
    for year_age in range(age, PEAK_AGE):
        for name in names:
            values[name] += expected_change(
                curves[name], age=year_age, growth=GrowthType.NORMAL, value=round(values[name])
            )
    return sum(_clamp(values[name]) - getattr(current, name) for name in names) / len(names)


def _clamp(value: float) -> int:
    return min(RATING_MAX, max(RATING_MIN, round(value)))


def future_value(prospect: Prospect, estimate: Ratings) -> float:
    """将来値: 推定の総合（`scouting_value`）に、`PEAK_AGE` 歳までの伸びを足したもの。"""
    return scouting_value(estimate) + growth_to_peak(prospect.age, prospect.position)


@dataclass(frozen=True)
class DraftSlot:
    """球団の本指名の人数の幅と位置の計画（会議の外で決まる。会議を始めたときに写す＝D3）。

    - `minimum`・`maximum`: 指名する日本人の人数の幅。AI は最多まで指名する。最少に届けば自分の番で選択終了できる
    - `planned`: AI が埋める位置（`draft.plan_team_draft` の日本人の位置）
    - `base_counts`: 会議の前の支配下の位置ごとの人数に、外国人の新人の位置を足したもの（位置の最低人数の検査に使う）
    """

    team_id: int
    minimum: int
    maximum: int
    planned: tuple[Position, ...]
    base_counts: Mapping[Position, int]

    def __post_init__(self) -> None:
        if not 0 <= self.minimum <= self.maximum <= MAX_DRAFTEES:
            raise ValueError("指名の人数の幅が正しくありません。")

    @classmethod
    def of(cls, team: DraftTeam, plan: TeamDraftPlan) -> DraftSlot:
        base = Counter({position: team.position_counts.get(position, 0) for position in MIN_BY_POSITION})
        base.update(plan.foreign_positions)
        return cls(
            team_id=team.team_id,
            minimum=min(plan.japanese_minimum, plan.japanese_count),
            maximum=plan.japanese_count,
            planned=plan.japanese_positions,
            base_counts=dict(base),
        )


@dataclass(frozen=True)
class DraftContext:
    """締める処理に渡す会議の設定（世界の規則と、その年の公式戦の最終順位）。"""

    rule: DraftOrderRule
    standings: DraftStandings


@dataclass(frozen=True)
class DraftTurn:
    """次に動く球団。`is_bid` は入札の巡か、`attempt` は入札の回（1から。指名の巡は1）。"""

    team_id: int
    round_number: int
    is_bid: bool
    attempt: int


@dataclass(frozen=True)
class Lottery:
    """入札の重複と抽選の結果。`bidders` は入札した球団（id の順）。"""

    round_number: int
    attempt: int
    prospect_key: int
    bidders: tuple[int, ...]
    winner: int


@dataclass(frozen=True)
class DraftSelection:
    """指名の結果ひとつ（巡は本指名の巡。1から）。"""

    team_id: int
    prospect_key: int
    round_number: int


@dataclass
class DraftState:
    """会議の状態。行動の並び（`actions`）から導く。`apply` で1手ずつ進める（不変条件は `InvalidDraftAction`）。

    中で覚えるのは導いた値だけ（作り直せば同じ）。保存するのは行動の並びだけ（D3）。
    """

    strategy: DraftOrderStrategy
    slots: Mapping[int, DraftSlot]
    pool: ProspectPool
    accuracy: ScoutingAccuracy = field(default_factory=ScoutingAccuracy)
    actions: list[DraftAction] = field(default_factory=list)
    selections: list[DraftSelection] = field(default_factory=list)
    lotteries: list[Lottery] = field(default_factory=list)
    round_number: int = 1
    attempt: int = 1
    _bids: dict[int, int] = field(default_factory=dict)
    _settled: set[int] = field(default_factory=set)
    _passed: set[int] = field(default_factory=set)
    _taken: set[int] = field(default_factory=set)
    _picked: Counter[int] = field(default_factory=Counter)
    _positions: dict[int, Counter[Position]] = field(default_factory=dict)
    # 球団 × 位置ごとの、候補の将来値の高い順（AI が使う。候補を足したら差し込む）
    _rankings: dict[tuple[int, Position], list[tuple[float, int]]] = field(default_factory=dict)

    @classmethod
    def open(
        cls,
        strategy: DraftOrderStrategy,
        slots: Sequence[DraftSlot],
        pool: ProspectPool,
        actions: Sequence[DraftAction] = (),
        *,
        accuracy: ScoutingAccuracy | None = None,
    ) -> DraftState:
        """設定と行動の並びから状態を作る（行動は先頭から当て直す。当てられない行動があれば `InvalidDraftAction`）。"""
        state = cls(
            strategy=strategy,
            slots={slot.team_id: slot for slot in slots},
            pool=pool,
            accuracy=accuracy or ScoutingAccuracy(),
        )
        state._normalize()
        for action in actions:
            state.apply(action)
        return state

    # --- 導く値 ---

    def _active(self, team_id: int) -> bool:
        slot = self.slots[team_id]
        return team_id not in self._passed and self._picked[team_id] < slot.maximum

    def _participants(self) -> list[int]:
        return [
            team_id
            for team_id in self.strategy.order(self.round_number)
            if team_id in self.slots and self._active(team_id) and team_id not in self._settled
        ]

    @property
    def is_complete(self) -> bool:
        return self.round_number > MAX_DRAFTEES or not any(self._active(team_id) for team_id in self.slots)

    def next_turn(self) -> DraftTurn | None:
        """次に動く球団。会議が終わっていれば None。"""
        if self.is_complete:
            return None
        bidding = self.strategy.is_bidding(self.round_number)
        waiting = [team_id for team_id in self._participants() if not (bidding and team_id in self._bids)]
        if not waiting:  # `_normalize` が巡を進めるので来ない
            return None
        return DraftTurn(
            team_id=waiting[0], round_number=self.round_number, is_bid=bidding, attempt=self.attempt if bidding else 1
        )

    def picked_positions(self, team_id: int) -> Counter[Position]:
        return Counter(self._positions.get(team_id, Counter()))

    def drafted(self) -> dict[int, list[tuple[Rookie, DraftPick]]]:
        """球団ごとの指名（巡の順）。指名は締める年のドラフトの本指名で、順位は巡。"""
        result: dict[int, list[tuple[Rookie, DraftPick]]] = {team_id: [] for team_id in self.slots}
        for selection in sorted(self.selections, key=lambda item: (item.team_id, item.round_number)):
            prospect = self.pool.get(selection.prospect_key)
            result[selection.team_id].append(
                (prospect.rookie, DraftPick(year=self.pool.year, round=selection.round_number))
            )
        return result

    def shortfall_after(self, team_id: int, position: Position | None) -> int:
        """`position` を1人指名した後（None は指名しないまま）の、位置の最低人数までの不足の合計。"""
        counts = Counter(self.slots[team_id].base_counts)
        counts.update(self._positions.get(team_id, Counter()))
        if position is not None:
            counts[position] += 1
        return sum(max(0, minimum - counts[pos]) for pos, minimum in MIN_BY_POSITION.items())

    def position_allowed(self, team_id: int, position: Position) -> bool:
        """位置の最低人数の硬い制約（AI も GM も）。足りない位置はいつでも選べ、ほかの位置は、残りの指名で
        最低人数に届く間だけ選べる（今までの自動ドラフト `choose_positions` が守っていた保証）。"""
        before = self.shortfall_after(team_id, None)
        after = self.shortfall_after(team_id, position)
        remaining = self.slots[team_id].maximum - self._picked[team_id] - 1
        return after < before or after <= remaining

    # --- 1手ずつ進める ---

    def apply(self, action: DraftAction) -> None:
        """行動を1つ当てる。成り立たなければ `InvalidDraftAction`（何も変えない）。"""
        turn = self.next_turn()
        if turn is None:
            raise InvalidDraftAction("ドラフト会議は終わっています。")
        if action.team_id != turn.team_id:
            raise InvalidDraftAction("この球団の番ではありません。")
        slot = self.slots[action.team_id]
        if action.kind is DraftActionKind.PASS:
            if self._picked[action.team_id] < slot.minimum:
                raise InvalidDraftAction(f"少なくとも{slot.minimum}人を指名するまで、選択を終えられません。")
            if self.shortfall_after(action.team_id, None) > 0:
                raise InvalidDraftAction("位置の最低人数に届くまで、選択を終えられません。")
            self.actions.append(action)
            self._passed.add(action.team_id)
            # 入札の巡で最後に残った球団が選択を終えたら、そろった入札をここで解く
            self._resolve_if_ready()
            self._normalize()
            return
        if (action.kind is DraftActionKind.BID) != turn.is_bid:
            raise InvalidDraftAction("この巡は入札です。" if turn.is_bid else "この巡は入札ではありません。")
        pool = self._pool_for(action)
        if action.prospect_key is None:
            raise InvalidDraftAction("指名する選手を選んでください。")
        prospect = pool.get(action.prospect_key)
        if prospect.key in self._taken:
            raise InvalidDraftAction("その選手は既に指名されています。")
        if not self.position_allowed(action.team_id, prospect.position):
            raise InvalidDraftAction("位置の最低人数に届かなくなるので、その位置の選手は指名できません。")
        if pool is not self.pool:
            self._add_to_rankings(prospect)
        self.pool = pool
        self.actions.append(action)
        if action.kind is DraftActionKind.PICK:
            self._select(action.team_id, prospect)
        else:
            self._bids[action.team_id] = prospect.key
            self._resolve_if_ready()
        self._normalize()

    def _resolve_if_ready(self) -> None:
        """入札の巡で、まだ決まっていない全球団の入札がそろっていれば解く。"""
        if not self.strategy.is_bidding(self.round_number) or not self._bids:
            return
        if all(team_id in self._bids for team_id in self._participants()):
            self._resolve_bids()

    def _pool_for(self, action: DraftAction) -> ProspectPool:
        if action.extra_position is None:
            return self.pool
        if action.prospect_key != len(self.pool):
            raise InvalidDraftAction("足す候補の番号が正しくありません。")
        return self.pool.with_extra(action.extra_position)

    def _select(self, team_id: int, prospect: Prospect) -> None:
        self.selections.append(DraftSelection(team_id, prospect.key, self.round_number))
        self._taken.add(prospect.key)
        self._picked[team_id] += 1
        self._positions.setdefault(team_id, Counter())[prospect.position] += 1
        self._settled.add(team_id)

    def _resolve_bids(self) -> None:
        """入札がそろった回を解く。単独は確定、重複は抽選で1球団。外れた球団は次の回に再入札する。"""
        by_prospect: dict[int, list[int]] = {}
        for team_id, key in self._bids.items():
            by_prospect.setdefault(key, []).append(team_id)
        for key in sorted(by_prospect):
            bidders = tuple(sorted(by_prospect[key]))
            if len(bidders) == 1:
                winner = bidders[0]
            else:
                rng = make_random(
                    game_seed(
                        self.pool.world_seed,
                        self.pool.year,
                        f"draft-lottery-main-{self.round_number}-{self.attempt}-{key}",
                    )
                )
                winner = bidders[min(len(bidders) - 1, int(rng.random() * len(bidders)))]
                self.lotteries.append(Lottery(self.round_number, self.attempt, key, bidders, winner))
            self._select(winner, self.pool.get(key))
        self._bids = {}
        self.attempt += 1

    def _normalize(self) -> None:
        """動ける球団が残っていない巡を進める。"""
        while not self.is_complete and not self._participants():
            self.round_number += 1
            self.attempt = 1
            self._bids = {}
            self._settled = set()

    # --- AI ---

    def evaluation(self, team_id: int, prospect: Prospect) -> float:
        """球団 `team_id` から見た候補の将来値（スカウトの推定から）。"""
        estimate = scouting_report(
            prospect, team_id, world_seed=self.pool.world_seed, year=self.pool.year, accuracy=self.accuracy
        )
        return future_value(prospect, estimate)

    def _ranking(self, team_id: int, position: Position) -> list[tuple[float, int]]:
        """球団から見た、位置の候補の将来値の高い順（同値は番号の順）。初めて要るときに作って覚える。"""
        ranking = self._rankings.get((team_id, position))
        if ranking is None:
            ranking = sorted(
                (
                    (-self.evaluation(team_id, prospect), prospect.key)
                    for prospect in self.pool.prospects
                    if prospect.position is position
                ),
            )
            self._rankings[(team_id, position)] = ranking
        return ranking

    def _add_to_rankings(self, prospect: Prospect) -> None:
        """足した候補を、作り済みの順位に差し込む（いつ作った順位でも、同じ候補の並びになる）。"""
        for (team_id, position), ranking in self._rankings.items():
            if position is prospect.position:
                bisect.insort(ranking, (-self.evaluation(team_id, prospect), prospect.key))

    def ai_action(self) -> DraftAction:
        """次の番の球団を AI として動かす行動。計画した位置の残りの中から、評価がいちばん高い候補を選ぶ。

        評価は `将来値 + NEED_WEIGHT × 位置の残り`。計画した位置を指名し尽くしていれば（GM が別の位置を選んだ後の
        お任せ）、位置の最低人数を割らない候補から将来値で選ぶ。その位置の候補が尽きていれば、その場で1人足す。
        """
        turn = self.next_turn()
        if turn is None:
            raise InvalidDraftAction("ドラフト会議は終わっています。")
        team_id = turn.team_id
        kind = DraftActionKind.BID if turn.is_bid else DraftActionKind.PICK
        slot = self.slots[team_id]
        needed = Counter(slot.planned)
        needed.subtract(self._positions.get(team_id, Counter()))
        order = list(generator.POSITION_RATIOS)
        any_needed = any(count > 0 for count in needed.values())
        allowed = [
            position
            for position in order
            if self.position_allowed(team_id, position) and (needed[position] > 0 or not any_needed)
        ]
        best: tuple[float, int] | None = None
        for position in allowed:
            top = next((item for item in self._ranking(team_id, position) if item[1] not in self._taken), None)
            if top is None:
                continue
            score = -top[0] + NEED_WEIGHT * max(0, needed[position])
            if best is None or (score, -top[1]) > (best[0], -best[1]):
                best = (score, top[1])
        if best is not None:
            return DraftAction(kind, team_id, best[1])
        position = max(allowed or order, key=lambda item: (needed[item], -order.index(item)))
        return DraftAction(kind, team_id, len(self.pool), extra_position=position)

    def advance_ai(self, *, stop_at: int | None = None) -> list[DraftAction]:
        """`stop_at` の球団の番か、会議の終わりまで AI を進める。当てた行動を返す。"""
        applied: list[DraftAction] = []
        while (turn := self.next_turn()) is not None and turn.team_id != stop_at:
            action = self.ai_action()
            self.apply(action)
            applied.append(action)
        return applied

    def auto_complete(self) -> list[DraftAction]:
        """残りを全部 AI で進める（会議を始めなかった年の締める処理・管理コマンド・お任せ）。"""
        return self.advance_ai(stop_at=None)


@dataclass(frozen=True)
class DraftResult:
    """会議の結果（新人と、抽選の経過）。新人の能力は錨の前。"""

    draftees: tuple[Draftee, ...]
    lotteries: tuple[Lottery, ...]
    state: DraftState


def run_draft(
    teams: Sequence[DraftTeam],
    context: DraftContext,
    *,
    year: int,
    world_seed: int,
    used_names: set[str],
    actions: Sequence[DraftAction] = (),
) -> DraftResult:
    """締める年 `year` の会議を開き、`actions`（D3 で保存した行動）を当ててから残りを AI で進め、新人を作る。

    `teams` は引退・戦力外の後の球団（球団 id の昇順に処理する）。`used_names` は世界の全選手の名前で、
    候補と外国人の新人の名前を足していく。外国人は球団ごとの乱数（`draft-<球団 id>`、年は翌年）で会議の外で作る。
    """
    ordered = sorted(teams, key=lambda team: team.team_id)
    missing = {team.team_id for team in ordered} - context.standings.team_ids
    if missing:
        raise ValueError(f"最終順位に無い球団があります（{sorted(missing)}）。")
    plans = {team.team_id: plan_team_draft(team) for team in ordered}
    slots = [DraftSlot.of(team, plans[team.team_id]) for team in ordered]
    planned = Counter(position for slot in slots for position in slot.planned)
    pool = draw_pool(planned, world_seed=world_seed, year=year, used_names=used_names)
    used_names.update(pool.taken_names)
    strategy = strategy_for(context.rule, context.standings, world_seed=world_seed, year=year)
    state = DraftState.open(strategy, slots, pool, actions)
    state.auto_complete()
    used_names.update(state.pool.taken_names)
    drafted = state.drafted()
    draftees: list[Draftee] = []
    for team in ordered:
        rng = make_random(game_seed(world_seed, year + 1, f"draft-{team.team_id}"))
        draftees.extend(
            club_draftees(
                rng, team, plans[team.team_id], drafted=drafted[team.team_id], year=year, used_names=used_names
            )
        )
    return DraftResult(draftees=tuple(draftees), lotteries=tuple(state.lotteries), state=state)
