"""試合シミュレーションエンジン。1試合を打席単位で進める。

**得点・イニングスコア・勝敗は引かない。** 打席を積み上げた結果として決まる。出力は
打席の列（`PlateAppearance`）と打順の記録（`LineupEntry`）で、これを `assemble_game()` に
渡して `Game` を組み立てる（スコアブックの保存と同じ関数。勝敗・セーブ・ホールドは手入力の
試合と同じ規則で決まる）。

延長は12回まで行い、決着しなければ引分（NPB の規定）。ホームがリードしていれば9回裏は行わず、
9回以降の裏に逆転すればその時点で終わる（サヨナラ）。

1打席の判定の順は「死球 → 四球 → 犠打・犠飛 → 三振 → 本塁打 → インプレー（安打 → 長打の内訳、
凡打 → 失策出塁・野選・ゴロ・フライ・ライナー・邪飛）」。各段の確率は `odds.matchup()`。

**投手の疲労**（先発の休養不足・救援の連投と詰まった登板・目安を超えた続投）は、
能力を目減りさせてから `matchup()` に渡す（`fatigue.py`）。
疲労は保存せず、`PitchingHistory` とその登板の対戦打者数から導く。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

from ..entities import Game, PlateAppearance
from ..exceptions import InvalidGame
from ..services.scoring import assemble_game
from ..value_objects import (
    Base,
    FieldingPosition,
    ForeignPlayerRules,
    GameHeader,
    GameStage,
    InningsPitched,
    LineupEntry,
    PlateAppearanceResult,
    Position,
    Season,
)
from .baseline import NPB, LeagueBaseline
from .baserunning import DEFAULT_RULES, BaserunningRules, build_advances
from .fatigue import (
    MAX_FATIGUE_POINTS,
    PitcherWorkload,
    PitchingHistory,
    fatigued,
    in_game_fatigue_points,
    pregame_fatigue_points,
)
from .fielding import draw_error, fielded_path, team_defense
from .manager import (
    LINEUP_SIZE,
    ClubOrders,
    ClubRoster,
    ForeignQuota,
    LineupSlot,
    PinchHitSituation,
    PitchingStaff,
    SimBatter,
    SimPitcher,
    available_relievers,
    choose_lineup,
    choose_pinch_hitter,
    choose_starter,
    closer_should_enter,
    pick_reliever,
    plan_pitching_staff,
    reliever_batter_target,
    should_change_pitcher,
    starter_batter_target,
)
from .odds import DEFAULT_SENSITIVITY, MatchupOdds, RatingSensitivity, matchup
from .randomness import GameRandom, weighted_index

P = PlateAppearanceResult
FP = FieldingPosition

INNINGS_PER_GAME = 9
OUTS_PER_INNING = InningsPitched.OUTS_PER_INNING
# 延長の上限（公式戦の既定）。NPB は12回を終えて同点なら引分。
MAX_INNINGS = 12
# 1つの半回の打席数の上限。乱数源が壊れていても無限ループにならないための安全装置
_MAX_PLATE_APPEARANCES_PER_HALF = 100
# 延長の制限が無い試合（日本シリーズの第8戦以降）の回数の上限。点の入らない乱数源でも
# 無限ループにならないための安全装置で、実際の試合がここに届くことはない
_MAX_INNINGS_WITHOUT_LIMIT = 50


@dataclass
class _Slot:
    """打順の1枠に、いま入っている選手。交代すると slot_sequence が1つ増える。"""

    batter: SimBatter
    position: FieldingPosition
    batting_order: int
    slot_sequence: int


@dataclass
class _Mound:
    """マウンドに上がっている投手の、この登板の状態。"""

    pitcher: SimPitcher
    target: int  # 受け持つ打者数の目安（ブルペンが尽きると延ばす）
    is_starter: bool
    workload: PitcherWorkload
    fatigue_before: int = 0  # 登板前の疲労点
    faced: int = 0
    runs: int = 0
    # 当初の目安。試合中の疲労はこれで数える（延ばした目安で数えると、続投の疲労が消える）
    planned_target: int = field(init=False)

    def __post_init__(self) -> None:
        self.planned_target = self.target

    @property
    def fatigue(self) -> int:
        """次の打者と対戦するときの疲労点（登板前＋試合中。上限で頭打ち）。"""
        return min(MAX_FATIGUE_POINTS, self.fatigue_before + in_game_fatigue_points(self.faced, self.planned_target))

    def outing(self, team_id: int) -> Outing:
        return Outing(
            team_id=team_id,
            pitcher_id=self.pitcher.player_id,
            is_starter=self.is_starter,
            workload=self.workload,
            fatigue_before=self.fatigue_before,
            planned_target=self.planned_target,
            faced=self.faced,
        )


@dataclass(frozen=True)
class Outing:
    """1登板の負荷（測定用。保存しない）。`UsageTally` が起用の偏りを数える。"""

    team_id: int
    pitcher_id: int
    is_starter: bool
    workload: PitcherWorkload
    fatigue_before: int
    planned_target: int
    faced: int


@dataclass
class _Side:
    """1チームの、その試合の状態。攻撃と守備の両方を持つ。"""

    roster: ClubRoster
    is_home: bool
    staff: PitchingStaff
    quota: ForeignQuota
    slots: list[_Slot]
    bench: list[SimBatter]
    speeds: dict[int, int]
    entries: list[LineupEntry] = field(default_factory=list)
    mounds: list[_Mound] = field(default_factory=list)
    used_pitchers: set[int] = field(default_factory=set)
    # 代打を出した枠の番号 → 次の守備から入る守備固め
    pending_replacements: dict[int, tuple[SimBatter, FieldingPosition]] = field(default_factory=dict)
    pinch_hitters: int = 0
    order_index: int = 0
    score: int = 0
    defense: float = 50.0

    @property
    def mound(self) -> _Mound:
        return self.mounds[-1]

    @property
    def team_id(self) -> int:
        return self.roster.team_id


@dataclass
class SimulatedGame:
    """シミュレーションした1試合。`game` は `assemble_game()` で成績まで組み立て済み。"""

    game: Game
    plate_appearances: list[PlateAppearance]
    lineup: list[LineupEntry]
    # 選手 id → 名前。ボックススコアを文字で出すためのもの
    names: dict[int, str]
    # 測定用（保存しない）: 登板ごとの負荷（ビジター、ホームの順に登板順）と、打席ごとの投手の疲労点（打席と同じ並び）
    outings: list[Outing] = field(default_factory=list)
    pitcher_fatigue: list[int] = field(default_factory=list)

    @property
    def innings_played(self) -> int:
        return self.game.line_score.innings


def simulate_game(
    rng: GameRandom,
    home: ClubRoster,
    away: ClubRoster,
    *,
    played_on: date,
    stage: GameStage,
    history: PitchingHistory | None = None,
    foreign_rules: ForeignPlayerRules,
    home_orders: ClubOrders | None = None,
    away_orders: ClubOrders | None = None,
    baseline: LeagueBaseline = NPB,
    sensitivity: RatingSensitivity = DEFAULT_SENSITIVITY,
    rules: BaserunningRules = DEFAULT_RULES,
    max_innings: int | None = MAX_INNINGS,
) -> SimulatedGame:
    """1試合を打席単位で進め、`assemble_game()` で `Game` を組み立てる。

    `stage` は試合の区分（公式戦の日程を消化するなら `REGULAR`、ポストシーズンなら `POSTSEASON`）。既定値は持たない
    （`Game.stage` と同じく、呼ぶ側が必ず明示する）。

    `max_innings` は延長の上限（その回を終えて同点なら引分）。`None` は制限なし（日本シリーズの第8戦以降。
    決着するまで続ける）。既定値（公式戦の12回）のままなら、引数を足す前と同じ打席列になる。

    `home` / `away` は1軍に登録された選手だけを入れた `ClubRoster`。先発は `history`（直近の登板の
    記録）から中5日を空けて決め、試合が終わったらその日の登板を `history` に書き足す。
    `history` を渡さなければ、直近の登板の疲労（先発の休養・救援の連投）を考えない（毎回、最も良い先発が投げる）。

    `home_orders` / `away_orders` は GM の編成の上書き（オーダー・投手陣）。渡さなければ（または
    区画が `None` なら）AI 監督が決めるので、渡さないときの結果は上書きを知らない版と同じ。
    """
    if max_innings is not None and not INNINGS_PER_GAME <= max_innings <= _MAX_INNINGS_WITHOUT_LIMIT:
        raise ValueError(
            f"延長の上限は{INNINGS_PER_GAME}〜{_MAX_INNINGS_WITHOUT_LIMIT}回にしてください（{max_innings}）。"
            "制限なしは None。"
        )
    engine = _Engine(rng, baseline, sensitivity, rules, max_innings)
    history = history if history is not None else PitchingHistory()
    sides = {
        True: engine.make_side(home, True, played_on, history, foreign_rules, home_orders),
        False: engine.make_side(away, False, played_on, history, foreign_rules, away_orders),
    }
    plate_appearances = engine.play(sides, played_on, history)

    header = GameHeader(
        season=Season(played_on.year),
        played_on=played_on,
        home_team_id=home.team_id,
        away_team_id=away.team_id,
        stage=stage,
    )
    lineup = [entry for side in sides.values() for entry in side.entries]
    game = assemble_game(header, lineup, plate_appearances)
    # 打席を積み上げながら数えた得点と、打席から導いた得点が一致すること
    if (game.home_score, game.away_score) != (sides[True].score, sides[False].score):
        raise InvalidGame(
            "打席から導いた得点がシミュレーションの得点と一致しません"
            f"（ビジター {game.away_score}/{sides[False].score}、ホーム {game.home_score}/{sides[True].score}）。"
        )

    history.record(played_on, [m.pitcher.player_id for m in sides[False].mounds])
    history.record(played_on, [m.pitcher.player_id for m in sides[True].mounds])
    names = {b.player_id: b.name for r in (home, away) for b in r.batters}
    names.update({p.player_id: p.name for r in (home, away) for p in r.pitchers})
    return SimulatedGame(
        game=game,
        plate_appearances=plate_appearances,
        lineup=lineup,
        names=names,
        outings=[m.outing(side.team_id) for side in (sides[False], sides[True]) for m in side.mounds],
        pitcher_fatigue=engine.pitcher_fatigue,
    )


class _Engine:
    def __init__(
        self,
        rng: GameRandom,
        baseline: LeagueBaseline,
        sensitivity: RatingSensitivity,
        rules: BaserunningRules,
        max_innings: int | None,
    ) -> None:
        self.rng = rng
        self.baseline = baseline
        self.sensitivity = sensitivity
        self.rules = rules
        self.max_innings = max_innings
        # 打席ごとの投手の疲労点（測定用）
        self.pitcher_fatigue: list[int] = []

    # --- 試合の準備 ---

    def make_side(
        self,
        roster: ClubRoster,
        is_home: bool,
        played_on: date,
        history: PitchingHistory,
        foreign_rules: ForeignPlayerRules,
        orders: ClubOrders | None = None,
    ) -> _Side:
        staff = orders.staff if orders and orders.staff else plan_pitching_staff(roster.pitchers)
        quota = ForeignQuota(rules=foreign_rules)
        if orders is not None and orders.lineup is not None:
            # 決めたオーダーの外国人を先に数え、その枠で先発を選ぶ（先発が枠を押し出さないように）
            lineup: Sequence[LineupSlot] = orders.lineup
            for item in lineup:
                quota.register(item.batter.is_foreign, position=item.batter.position)
            starter = choose_starter(staff, history, played_on, quota)
            quota.register(starter.is_foreign, position=Position.PITCHER)
        else:
            starter = choose_starter(staff, history, played_on, quota)
            quota.register(starter.is_foreign, position=Position.PITCHER)
            lineup = choose_lineup(roster.batters, quota)
            for item in lineup:
                quota.register(item.batter.is_foreign, position=item.batter.position)
        slots = [
            _Slot(batter=item.batter, position=item.position, batting_order=order, slot_sequence=0)
            for order, item in enumerate(lineup, start=1)
        ]
        started = {slot.batter.player_id for slot in slots}

        side = _Side(
            roster=roster,
            is_home=is_home,
            staff=staff,
            quota=quota,
            slots=slots,
            bench=[b for b in roster.batters if b.player_id not in started],
            speeds={b.player_id: b.ratings.speed for b in roster.batters},
        )
        side.entries = [
            LineupEntry(
                team_id=roster.team_id,
                player_id=slot.batter.player_id,
                batting_order=slot.batting_order,
                slot_sequence=0,
                fielding_position=slot.position,
            )
            for slot in slots
        ]
        workload = history.workload(starter.player_id, played_on)
        side.mounds.append(
            _Mound(
                starter,
                starter_batter_target(self.rng, starter),
                is_starter=True,
                workload=workload,
                fatigue_before=pregame_fatigue_points(workload, as_starter=True, stamina=starter.ratings.stamina),
            )
        )
        side.used_pitchers.add(starter.player_id)
        return side

    # --- 試合の進行 ---

    def play(
        self,
        sides: dict[bool, _Side],
        played_on: date,
        history: PitchingHistory,
    ) -> list[PlateAppearance]:
        """打席を積んでいく。通し番号が時系列の唯一の出典。"""
        plate_appearances: list[PlateAppearance] = []
        inning = 1
        while True:
            for is_bottom in (False, True):
                if is_bottom and inning >= INNINGS_PER_GAME and sides[True].score > sides[False].score:
                    continue  # ホームがリードしていれば9回以降の裏は行わない
                self._half_inning(plate_appearances, sides, inning, is_bottom, played_on, history)
            if inning >= INNINGS_PER_GAME and sides[True].score != sides[False].score:
                break
            if self.max_innings is not None and inning >= self.max_innings:
                break
            if inning >= _MAX_INNINGS_WITHOUT_LIMIT:
                raise InvalidGame("延長が終わりません（乱数源が壊れている可能性があります）。")
            inning += 1
        return plate_appearances

    def _half_inning(
        self,
        plate_appearances: list[PlateAppearance],
        sides: dict[bool, _Side],
        inning: int,
        is_bottom: bool,
        played_on: date,
        history: PitchingHistory,
    ) -> None:
        """半回ぶんの打席を積む。3アウトか、サヨナラで終わる。"""
        batting, fielding = sides[is_bottom], sides[not is_bottom]
        self._take_the_field(fielding, len(plate_appearances) + 1)
        occupied: dict[Base, int] = {}
        outs = 0
        started_at = len(plate_appearances)

        while outs < OUTS_PER_INNING:
            if len(plate_appearances) - started_at >= _MAX_PLATE_APPEARANCES_PER_HALF:
                raise InvalidGame("半回が終わりません（乱数源が壊れている可能性があります）。")
            self._maybe_change_pitcher(
                fielding, batting, inning, played_on, history, at_inning_start=len(plate_appearances) == started_at
            )
            slot = self._next_slot(batting, fielding, inning, occupied, len(plate_appearances) + 1)
            entry = self._plate_appearance(
                sequence=len(plate_appearances) + 1,
                inning=inning,
                is_bottom=is_bottom,
                slot=slot,
                batting=batting,
                fielding=fielding,
                occupied=occupied,
                outs=outs,
            )
            plate_appearances.append(entry)

            outs += entry.outs_recorded
            fielding.mound.faced += 1
            fielding.mound.runs += entry.runs_scored
            batting.score += entry.runs_scored

            # サヨナラ。決勝点が入った時点で終わる
            if is_bottom and inning >= INNINGS_PER_GAME and batting.score > fielding.score:
                return

    def _take_the_field(self, side: _Side, next_sequence: int) -> None:
        """守備につく前に、代打の後を継ぐ守備固めを入れ、守備力を計算し直す。

        守備固めが入る打席は、その半回の最初の打席。**入った打席を記録する**
        （守備位置の持ち主は、この打席の番号で切り替わる）。
        """
        for index, (replacement, covered) in sorted(side.pending_replacements.items()):
            slot = side.slots[index]
            replaced = _Slot(
                batter=replacement,
                position=covered,
                batting_order=slot.batting_order,
                slot_sequence=slot.slot_sequence + 1,
            )
            side.slots[index] = replaced
            side.entries.append(
                LineupEntry(
                    team_id=side.team_id,
                    player_id=replacement.player_id,
                    batting_order=replaced.batting_order,
                    slot_sequence=replaced.slot_sequence,
                    fielding_position=replaced.position,
                    entered_sequence=next_sequence,
                )
            )
        side.pending_replacements.clear()
        side.defense = team_defense(
            [(slot.position, slot.batter.ratings.fielding) for slot in side.slots if slot.position.takes_the_field]
        )

    # --- 継投 ---

    def _maybe_change_pitcher(
        self,
        fielding: _Side,
        batting: _Side,
        inning: int,
        played_on: date,
        history: PitchingHistory,
        *,
        at_inning_start: bool,
    ) -> None:
        mound = fielding.mound
        lead = fielding.score - batting.score
        change = should_change_pitcher(
            is_starter=mound.is_starter,
            faced=mound.faced,
            target=mound.target,
            runs_allowed=mound.runs,
            at_inning_start=at_inning_start,
        )
        closer_turn = at_inning_start and closer_should_enter(fielding.staff, mound.pitcher, inning=inning, lead=lead)
        if not (change or closer_turn):
            return
        available = available_relievers(fielding.staff, fielding.used_pitchers, history, played_on, fielding.quota)
        closer = fielding.staff.closer
        if closer_turn and closer in available:
            chosen: SimPitcher | None = closer
        elif change:
            chosen = pick_reliever(
                self.rng, fielding.staff, available, inning=inning, lead=lead, is_home=fielding.is_home
            )
        else:
            return  # 抑えを出したい場面だが、投げられない。今の投手のまま
        if chosen is None:
            # 投げられる投手がいなければ続投する（投手のいない回を作らない）
            mound.target += 3
            return
        fielding.quota.register(chosen.is_foreign, position=Position.PITCHER)
        fielding.used_pitchers.add(chosen.player_id)
        workload = history.workload(chosen.player_id, played_on)
        fielding.mounds.append(
            _Mound(
                chosen,
                reliever_batter_target(self.rng, chosen),
                is_starter=False,
                workload=workload,
                fatigue_before=pregame_fatigue_points(workload, as_starter=False, stamina=chosen.ratings.stamina),
            )
        )

    # --- 打順 ---

    def _next_slot(
        self, batting: _Side, fielding: _Side, inning: int, occupied: dict[Base, int], next_sequence: int
    ) -> _Slot:
        """次の打者の枠。打順は1〜9を巡回する（スコアブックを横に読む性質そのもの）。"""
        index = batting.order_index
        batting.order_index = (index + 1) % LINEUP_SIZE
        self._maybe_pinch_hit(batting, fielding, index, inning, occupied, next_sequence)
        return batting.slots[index]

    def _maybe_pinch_hit(
        self,
        side: _Side,
        opponent: _Side,
        index: int,
        inning: int,
        occupied: dict[Base, int],
        next_sequence: int,
    ) -> None:
        """終盤に代打を送る。

        送らない枠: 塁上にいる選手の枠（交代した選手が塁上に残ってしまう）、すでに交代した枠
        （代打の代打は出さない。守備固めの予約が、元の守備位置を見失う）。
        """
        slot = side.slots[index]
        if slot.slot_sequence > 0 or slot.batter.player_id in occupied.values():
            return
        decision = choose_pinch_hitter(
            PinchHitSituation(
                inning=inning,
                lead=side.score - opponent.score,
                pinch_hitters_used=side.pinch_hitters,
                current=slot.batter,
                position=slot.position,
                bench=side.bench,
                quota=side.quota,
            )
        )
        if decision is None:
            return

        side.pinch_hitters += 1
        side.bench.remove(decision.batter)
        side.quota.register(decision.batter.is_foreign, position=decision.batter.position)
        covered = slot.position
        replaced = _Slot(decision.batter, decision.fielding_position, slot.batting_order, slot.slot_sequence + 1)
        side.slots[index] = replaced
        side.entries.append(
            LineupEntry(
                team_id=side.team_id,
                player_id=decision.batter.player_id,
                batting_order=replaced.batting_order,
                slot_sequence=replaced.slot_sequence,
                fielding_position=replaced.position,
                # 代打は初めて打席に立った打席から入る
                entered_sequence=next_sequence,
            )
        )
        if decision.replacement is not None:
            side.bench.remove(decision.replacement)
            side.quota.register(decision.replacement.is_foreign, position=decision.replacement.position)
            side.pending_replacements[index] = (decision.replacement, covered)

    # --- 1打席 ---

    def _plate_appearance(
        self,
        *,
        sequence: int,
        inning: int,
        is_bottom: bool,
        slot: _Slot,
        batting: _Side,
        fielding: _Side,
        occupied: dict[Base, int],
        outs: int,
    ) -> PlateAppearance:
        """1打席ぶんの記録を作り、塁の状態を進める。"""
        pitcher = fielding.mound.pitcher
        # 疲労は能力の目減りとして効かせる（率の式は matchup のまま）。SimPitcher は差し替えない
        fatigue = fielding.mound.fatigue
        self.pitcher_fatigue.append(fatigue)
        odds = matchup(
            slot.batter.ratings, fatigued(pitcher.ratings, fatigue), fielding.defense, self.baseline, self.sensitivity
        )
        result = self._draw_result(odds, occupied, outs)
        batter_id = slot.batter.player_id

        advances = build_advances(self.rng, self.rules, result, batter_id, occupied, outs, batting.speeds)
        errors = [draw_error(self.rng, self._fielders(fielding, pitcher))] if result is P.REACHED_ON_ERROR else []
        # 併殺は、打者への守備で2つ以上のアウトが取られた打席（盗塁刺は含めない）
        double_play = sum(1 for a in advances if a.is_out and not a.reason.is_baserunning_out) >= 2

        return PlateAppearance(
            sequence=sequence,
            inning=inning,
            is_bottom=is_bottom,
            batter_id=batter_id,
            pitcher_id=pitcher.player_id,
            batting_order=slot.batting_order,
            slot_sequence=slot.slot_sequence,
            result=result,
            fielded_by=fielded_path(self.rng, result, double_play=double_play),
            advances=advances,
            errors=errors,
        )

    def _draw_result(self, odds: MatchupOdds, occupied: dict[Base, int], outs: int) -> PlateAppearanceResult:
        """1打席の結果を引く。

        判定の順は「死球 → 四球 → 犠打 → 犠飛 → 三振 → 本塁打 → インプレー」。打数に数えない結果を
        先に落とすことで、残りが打数になる。
        """
        rng, baseline = self.rng, self.baseline
        if rng.random() < odds.hit_by_pitch:
            return P.HIT_BY_PITCH
        if rng.random() < odds.walk:
            return P.INTENTIONAL_WALK if rng.random() < baseline.intentional_walk_share else P.WALK

        if outs < OUTS_PER_INNING - 1:
            on_first_or_second = Base.FIRST in occupied or Base.SECOND in occupied
            if on_first_or_second and Base.THIRD not in occupied and rng.random() < baseline.sacrifice_bunt_rate:
                return P.SACRIFICE_BUNT
            if Base.THIRD in occupied and rng.random() < baseline.sacrifice_fly_rate:
                return P.SACRIFICE_FLY

        # ここからが打数
        if rng.random() < odds.strikeout:
            return P.STRIKEOUT_LOOKING if rng.random() < baseline.looking_strikeout_share else P.STRIKEOUT_SWINGING
        if rng.random() < odds.home_run:
            return P.HOME_RUN
        if rng.random() < odds.in_play_hit:
            if rng.random() < odds.double:
                return P.DOUBLE
            return P.TRIPLE if rng.random() < odds.triple else P.SINGLE
        return self._draw_in_play_out(odds, occupied)

    def _draw_in_play_out(self, odds: MatchupOdds, occupied: dict[Base, int]) -> PlateAppearanceResult:
        """安打にならなかった打球の結果。"""
        rng, baseline = self.rng, self.baseline
        if rng.random() < odds.error:
            return P.REACHED_ON_ERROR
        # 野選は封殺できる走者がいるときだけ。三塁に走者がいると得点が絡んで
        # 打点の扱いが分かれるため、その場合は選ばない
        if Base.FIRST in occupied and Base.THIRD not in occupied and rng.random() < baseline.fielders_choice_share:
            return P.FIELDERS_CHOICE
        batted = weighted_index(
            rng,
            [
                baseline.ground_out_share,
                baseline.fly_out_share,
                baseline.line_out_share,
                max(0.0, 1.0 - baseline.ground_out_share - baseline.fly_out_share - baseline.line_out_share),
            ],
        )
        return (P.GROUND_OUT, P.FLY_OUT, P.LINE_OUT, P.FOUL_FLY_OUT)[batted]

    @staticmethod
    def _fielders(side: _Side, pitcher: SimPitcher) -> list[tuple[FieldingPosition, int, int]]:
        """いま守備に就いている選手。(守備位置, 選手 id, 守備力)。投手は能力の守備力を持たないので平均とする。"""
        fielders = [
            (slot.position, slot.batter.player_id, slot.batter.ratings.fielding)
            for slot in side.slots
            if slot.position.takes_the_field
        ]
        fielders.append((FP.PITCHER, pitcher.player_id, 50))
        return fielders
