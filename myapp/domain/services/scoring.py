"""打席の記録から成績を導く。

スコアラーが試合後に縦計・横計を取る作業にあたる。打数・安打・打点・投球回・
失点はすべて打席（`PlateAppearance`）から導かれ、手入力しない。

導出をエンティティのメソッドではなくここに置いているのは、値オブジェクト
（`BattingLine` など）が `PlateAppearance` を知らずに済むようにするため
（`entities` が `value_objects` を import する向きを保つ）。

勝敗・セーブ・ホールドはここでは決めない。イニングスコアと継投から決まる別の
関心事で、`decisions` が担う。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ..entities import PlateAppearance
from ..value_objects import (
    Base,
    BattingLine,
    InningsPitched,
    PitchingLine,
    PlateAppearanceResult,
)


def _in_order(plate_appearances: Iterable[PlateAppearance]) -> list[PlateAppearance]:
    """打席を進行順に並べる。塁の状態を再生する処理はすべてこれを通る。"""
    return sorted(plate_appearances, key=lambda entry: entry.sequence)


def _count_results(entries: list[PlateAppearance], result: PlateAppearanceResult) -> int:
    return sum(1 for entry in entries if entry.result is result)


def batting_line_for(plate_appearances: Iterable[PlateAppearance], batter_id: int) -> BattingLine:
    """1人の打者の打撃成績を打席から組み立てる。

    打数に数えるか・安打か・何塁打かは `PlateAppearanceResult` が知っており、
    打点は打席が自分で数える。ここは振り分けるだけで判定は持たない。
    """
    own = [entry for entry in plate_appearances if entry.batter_id == batter_id]
    return BattingLine(
        at_bats=sum(1 for entry in own if entry.result.counts_as_at_bat),
        singles=_count_results(own, PlateAppearanceResult.SINGLE),
        doubles=_count_results(own, PlateAppearanceResult.DOUBLE),
        triples=_count_results(own, PlateAppearanceResult.TRIPLE),
        home_runs=_count_results(own, PlateAppearanceResult.HOME_RUN),
        runs_batted_in=sum(entry.runs_batted_in for entry in own),
        walks=sum(1 for entry in own if entry.result.is_walk),
        hit_by_pitch=_count_results(own, PlateAppearanceResult.HIT_BY_PITCH),
        sacrifice_flies=_count_results(own, PlateAppearanceResult.SACRIFICE_FLY),
    )


def pitching_line_for(plate_appearances: Iterable[PlateAppearance], pitcher_id: int) -> PitchingLine:
    """1人の投手の投球成績を打席から組み立てる。

    勝敗・セーブ・ホールド・先発登板は含めない（`decisions` が継投とイニングスコアから
    決める別の関心事で、ここで 0 のまま返すと二重の出典になる）。呼ぶ側が
    `decisions` の結果と合わせて使う。
    """
    ordered = _in_order(plate_appearances)
    faced = [entry for entry in ordered if entry.pitcher_id == pitcher_id]
    return PitchingLine(
        innings=InningsPitched(outs=sum(entry.outs_recorded for entry in faced)),
        earned_runs=earned_runs_for(ordered, pitcher_id),
        strikeouts=sum(1 for entry in faced if entry.result.is_strikeout),
        hits_allowed=sum(1 for entry in faced if entry.result.is_hit),
        walks_allowed=sum(1 for entry in faced if entry.result.is_walk),
        home_runs_allowed=_count_results(faced, PlateAppearanceResult.HOME_RUN),
        hit_by_pitch_allowed=_count_results(faced, PlateAppearanceResult.HIT_BY_PITCH),
    )


@dataclass(frozen=True)
class RunScored:
    """還った1点と、その責任を負う投手。

    失点は**その走者を塁に出した投手**に記録する（還った時にマウンドにいた投手
    ではない）。救援投手が前任の走者を還してしまっても、失点は前任に付く。
    """

    runner_id: int
    responsible_pitcher_id: int
    inning: int
    is_bottom: bool
    is_earned: bool


def runs_scored_in(plate_appearances: Iterable[PlateAppearance]) -> list[RunScored]:
    """還った得点を、責任投手と自責点かどうかを添えて列挙する。

    塁ごとに「誰の責任か」「失策が絡んでいるか」を持たせて進塁を追う。責任は塁に
    ついて回るため、**代走が出ても自動的に引き継がれる**（走者の id だけが変わる）。

    自責点は規則 9.16 の「失策・捕逸が無かったものと仮定した再構成」を、
    **走者ごとの経路に失策・捕逸が絡んだか**で近似する。「その失策が無ければ
    イニングが終わっていた」までは追えないため、確定値ではない
    （呼ぶ側は上書きを許す。設計書の「自責点は推定＋上書き」を参照）。
    """
    scored: list[RunScored] = []
    # 塁 → (責任投手, 失策・捕逸が絡んでいるか)
    runners: dict[Base, tuple[int, bool]] = {}
    current: tuple[int, bool] | None = None

    for entry in _in_order(plate_appearances):
        if entry.half_inning != current:
            current = entry.half_inning
            runners = {}

        for advance in entry.advances_lead_runner_first():
            if advance.is_batter:
                tainted = advance.reason.is_unearned_cause or entry.result is PlateAppearanceResult.REACHED_ON_ERROR
                carried = (entry.pitcher_id, tainted)
            else:
                # 記録が欠けている塁は、その打席の投手の責任として扱う（安全側）
                pitcher_id, tainted = runners.pop(advance.from_base, (entry.pitcher_id, False))
                carried = (pitcher_id, tainted or advance.reason.is_unearned_cause)

            if advance.has_scored:
                scored.append(
                    RunScored(
                        runner_id=advance.runner_id,
                        responsible_pitcher_id=carried[0],
                        inning=entry.inning,
                        is_bottom=entry.is_bottom,
                        is_earned=not carried[1],
                    )
                )
            elif advance.to_base.occupies_base:
                runners[advance.to_base] = carried

    return scored


def runs_allowed_for(plate_appearances: Iterable[PlateAppearance], pitcher_id: int) -> int:
    """失点。自責点と違い、失策が絡んだ得点も数える。"""
    return sum(1 for run in runs_scored_in(plate_appearances) if run.responsible_pitcher_id == pitcher_id)


def earned_runs_for(plate_appearances: Iterable[PlateAppearance], pitcher_id: int) -> int:
    """自責点（推定）。失策・捕逸が絡んだ得点を除く。"""
    return sum(
        1 for run in runs_scored_in(plate_appearances) if run.responsible_pitcher_id == pitcher_id and run.is_earned
    )


def left_on_base(plate_appearances: Iterable[PlateAppearance], *, is_bottom: bool) -> int:
    """残塁。半回が終わった時点で塁上に残っていた走者を数え、試合ぶんを合計する。

    最後の半回が途中で終わっていても（サヨナラなど）、その時点の走者を数える。
    """
    total = 0
    occupied = 0
    current: tuple[int, bool] | None = None

    for entry in _in_order(plate_appearances):
        if entry.is_bottom != is_bottom:
            continue
        if entry.half_inning != current:
            total += occupied
            occupied = 0
            current = entry.half_inning
        for advance in entry.advances:
            if advance.to_base.occupies_base:
                occupied += 1
            if not advance.is_batter:
                occupied -= 1

    return total + occupied


def errors_for(plate_appearances: Iterable[PlateAppearance], player_id: int) -> int:
    """失策の数。守備成績の出典。"""
    return sum(1 for entry in plate_appearances for error in entry.errors if error.player_id == player_id)
