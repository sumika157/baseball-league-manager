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

from ..entities import Game, PlateAppearance
from ..exceptions import InvalidPlateAppearance
from ..value_objects import (
    AdvanceReason,
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

    **得点と盗塁は自分の打席の外で起きる。** 走者としての動きなので、
    自分が打者でない打席の進塁も見る必要がある。
    """
    entries = list(plate_appearances)
    own = [entry for entry in entries if entry.batter_id == batter_id]
    moves = [advance for entry in entries for advance in entry.advances if advance.runner_id == batter_id]
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
        runs=sum(1 for advance in moves if advance.has_scored),
        strikeouts=sum(1 for entry in own if entry.result.is_strikeout),
        sacrifice_bunts=_count_results(own, PlateAppearanceResult.SACRIFICE_BUNT),
        intentional_walks=_count_results(own, PlateAppearanceResult.INTENTIONAL_WALK),
        stolen_bases=sum(1 for advance in moves if advance.reason is AdvanceReason.STOLEN_BASE),
        caught_stealing=sum(1 for advance in moves if advance.reason is AdvanceReason.CAUGHT_STEALING),
        # 併殺打は「自分の打席でアウトが2つ記録された」こと。種別では持たない
        double_plays=sum(1 for entry in own if entry.is_double_play),
    )


def pitching_line_for(plate_appearances: Iterable[PlateAppearance], pitcher_id: int) -> PitchingLine:
    """1人の投手の投球成績を打席から組み立てる。

    勝敗・セーブ・ホールド・先発登板は含めない（`decisions` が継投とイニングスコアから
    決める別の関心事で、ここで 0 のまま返すと二重の出典になる）。呼ぶ側が
    `decisions` の結果と合わせて使う。
    """
    ordered = _in_order(plate_appearances)
    faced = [entry for entry in ordered if entry.pitcher_id == pitcher_id]
    # 失点と自責点は同じ再生から出る。2度呼ぶと1試合ぶんを2回たどることになる
    charged = [run for run in runs_scored_in(ordered) if run.responsible_pitcher_id == pitcher_id]
    return PitchingLine(
        innings=InningsPitched(outs=sum(entry.outs_recorded for entry in faced)),
        runs_allowed=len(charged),
        earned_runs=sum(1 for run in charged if run.is_earned),
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


# 打席から導ける投球成績の項目。勝敗・セーブ・ホールド・先発登板は打席からは
# 決まらない（イニングスコアと継投から決まる別の関心事）ので照合しない。
_PITCHING_FIELDS_FROM_PLATE_APPEARANCES = (
    "runs_allowed",
    "earned_runs",
    "strikeouts",
    "hits_allowed",
    "walks_allowed",
    "home_runs_allowed",
    "hit_by_pitch_allowed",
)


def ensure_lines_match_plate_appearances(game: Game) -> None:
    """保存する明細が、打席の記録から導ける値と一致することを確かめる。

    打撃・投球の明細は打席から導出できるが、**通算成績の集計のために保存もしている。**
    自責点は走者ごとの経路を再生しないと出ず、SQL で集計できないため
    （3,480試合を再生すると約68秒かかる。詳細は `docs/design/plate-appearance-scoring.md`）。

    同じ事実を2か所に持つことになるので、**集約が照合する**。イニングスコアと
    最終得点を突き合わせる `ensure_line_score_matches()` と同じ形で、片方だけを
    書き換えた記録が保存されるのを防ぐ。打席の記録が無い試合では何もしない。
    """
    if not game.plate_appearances:
        return

    for entry in game.batting:
        counted = batting_line_for(game.plate_appearances, entry.player_id)
        if entry.line != counted:
            raise InvalidPlateAppearance(
                f"打撃成績が打席の記録と一致しません（選手id={entry.player_id}）。"
                f"打席から数え直すと 打数{counted.at_bats}・安打{counted.hits}・打点{counted.runs_batted_in} です。"
                "この試合は打席が出典なので、成績だけを書き換えることはできません。"
            )

    # 打撃と型が違うので変数名を分ける（同じ名前だと mypy が最初の型で固定してしまう）
    for outing in game.pitching:
        pitched = pitching_line_for(game.plate_appearances, outing.player_id)
        if outing.line.innings != pitched.innings:
            raise InvalidPlateAppearance(
                f"投球回が打席の記録と一致しません（選手id={outing.player_id}）。"
                f"打席から数え直すと{pitched.innings.to_notation()}回です。"
            )
        for name in _PITCHING_FIELDS_FROM_PLATE_APPEARANCES:
            if getattr(outing.line, name) != getattr(pitched, name):
                raise InvalidPlateAppearance(
                    f"投球成績が打席の記録と一致しません（選手id={outing.player_id}・{name}）。"
                    f"打席から数え直すと{getattr(pitched, name)}です。"
                )

    recorded = {entry.player_id for entry in game.batting}
    missing = {entry.batter_id for entry in game.plate_appearances} - recorded
    if recorded and missing:
        raise InvalidPlateAppearance(f"打席に立った選手の打撃成績がありません（選手id={sorted(missing)}）。")
