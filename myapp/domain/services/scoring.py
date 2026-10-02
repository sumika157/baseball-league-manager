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

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from ..entities import Game, GameBatting, PlateAppearance
from ..exceptions import InvalidGame, InvalidPlateAppearance
from ..value_objects import (
    AdvanceReason,
    Base,
    BattingLine,
    FieldingLine,
    FieldingPosition,
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
    （呼ぶ側は上書きを許す。README「打席を出典にした理由」を参照）。
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


# --- 守備成績 ---------------------------------------------------------------


@dataclass(frozen=True)
class FieldingCredits:
    """1打席の打球の処理で、どの守備位置に何が付くか。位置のままで、選手には直さない。"""

    putouts: tuple[FieldingPosition, ...] = ()
    assists: tuple[FieldingPosition, ...] = ()
    # 併殺に関わった位置。同じ位置が2度出ることがある（3-6-3 の一塁）ので、
    # 選手に直してから重複を除く
    double_play: tuple[FieldingPosition, ...] = ()


def _made_fielded_out(entry: PlateAppearance) -> bool:
    """打球の処理でアウトが取られた打席か。経路を読んでよいのはこの場合だけ。

    **打者がアウトになる結果**（凡打・三振・犠打・犠飛）と、**野選出塁**（打者は生きるが
    走者が封殺される。経路はそのアウトを取るまでの処理）が当たる。安打・四死球・
    本塁打・妨害は経路があっても付けない。**失策出塁も付けない**（アウトが無く、
    送球失策の補殺の扱いまで入り込むため）。盗塁刺・牽制死は打球の処理ではない。
    """
    if entry.result.retires_batter:
        return True
    if entry.result is PlateAppearanceResult.FIELDERS_CHOICE:
        return any(
            advance.is_out and not advance.is_batter and not advance.reason.is_baserunning_out
            for advance in entry.advances
        )
    return False


def fielding_credits(entry: PlateAppearance) -> FieldingCredits:
    """打球の処理経路を読んで、刺殺・補殺・併殺参加の位置を決める。

    - **最後の位置が刺殺、それより前の各位置が補殺**（6-3 なら遊が補殺・一が刺殺。
      外野フライ (8,) は刺殺だけ）。
    - **併殺**（`is_double_play`）は刺殺が2つ付く。1つは最後の位置、もう1つは**最後から2番目の
      位置**（6-4-3 は二、6-3 は遊。経路が1つだけの単独併殺 "3" は同じ位置に2つ）。
      6-4-3 なら遊が補殺、二が刺殺＋補殺、一が刺殺になる。併殺に関わった全員に併殺参加が付く。
    - **三振で経路が空なら捕手の刺殺**（公認野球規則 9.10）。入力させずに導く。
    """
    if not _made_fielded_out(entry):
        return FieldingCredits()

    path = entry.fielded_by
    if not path and entry.result.is_strikeout:
        path = (FieldingPosition.CATCHER,)
    if not path:
        return FieldingCredits()

    putouts = [path[-1]]
    if entry.is_double_play:
        putouts.append(path[-2] if len(path) >= 2 else path[-1])
    return FieldingCredits(
        putouts=tuple(putouts),
        assists=path[:-1],
        double_play=path if entry.is_double_play else (),
    )


@dataclass(frozen=True)
class _Tenure:
    """1人の選手が、ある守備位置に就いていた期間。打席の通し番号で区切る。"""

    player_id: int
    position: FieldingPosition
    from_sequence: int  # この番号の打席から（含む）
    until_sequence: int | None  # この番号の打席の手前まで。None なら試合の最後まで

    def covers(self, sequence: int) -> bool:
        return self.from_sequence <= sequence and (self.until_sequence is None or sequence < self.until_sequence)


def _tenures_by_side(game: Game) -> dict[bool, list[_Tenure]]:
    """ラインアップから、攻撃側ごとに「誰がいつからいつまでどの守備位置に就いたか」を作る。

    キーは「そのチームが裏に攻めるか」（`PlateAppearance.is_bottom` と同じ向き）。
    チームは `GameBatting.team_id` から引く（打席からの推測はしない）。

    打順の枠ごとに、在任は**入った打席（`entered_sequence`。スタメンは試合開始）から、同じ枠の
    次の選手が入った打席の手前まで**。**入った時点が不明な途中出場は、その選手を飛ばす**。
    次の選手の入った時点が不明なら、いつまで居たかも分からないので、前任者も飛ばす
    （推測しない）。守備に就かない位置（代打・代走・指名打者）は期間を作らない。
    """
    slots: dict[tuple[int, int], list[GameBatting]] = {}
    for batting in game.batting:
        if batting.batting_order is not None:
            slots.setdefault((batting.team_id, batting.batting_order), []).append(batting)

    tenures: dict[bool, list[_Tenure]] = {False: [], True: []}
    for (team_id, _order), group in slots.items():
        group.sort(key=lambda row: row.slot_sequence)
        for index, row in enumerate(group):
            position = row.fielding_position
            if position is None or not position.takes_the_field:
                continue
            start = 0 if row.slot_sequence == 0 else row.entered_sequence
            if start is None:
                continue
            until: int | None = None
            if index + 1 < len(group):
                until = group[index + 1].entered_sequence
                if until is None:
                    continue
            tenures[team_id == game.home_team_id].append(_Tenure(row.player_id, position, start, until))
    return tenures


def fielders_by_plate_appearance(game: Game) -> dict[int, dict[FieldingPosition, int]]:
    """打席の通し番号 → その時点で守備側チームの各守備位置に就いている選手。

    **位置を選手に解決する規則はここだけ**にある。刺殺・補殺・併殺参加は、
    経路の位置（`fielded_by`）をこの結果で選手に引き直して付ける。

    - 投手の位置は**その打席の `pitcher_id`**（指名打者制では投手が打順にいない）。
    - 代打・代走・指名打者は守備に就かない。その位置を継ぐ守備交代が記録されていなければ、
      その位置は空く。
    - **解決できない位置は辞書に入れない。** 呼ぶ側は黙って飛ばす（例外にしない）。
      入った時点が不明な選手の位置、同じ時点で2人以上が同じ位置にいる場合が当たる
      （後勝ちで選ばない）。
    """
    tenures = _tenures_by_side(game)
    alignments: dict[int, dict[FieldingPosition, int]] = {}
    for entry in game.plate_appearances:
        # 攻めている側の反対が守っている
        holders: dict[FieldingPosition, set[int]] = {}
        for tenure in tenures[not entry.is_bottom]:
            if tenure.covers(entry.sequence):
                holders.setdefault(tenure.position, set()).add(tenure.player_id)
        alignment = {position: next(iter(ids)) for position, ids in holders.items() if len(ids) == 1}
        alignment[FieldingPosition.PITCHER] = entry.pitcher_id
        alignments[entry.sequence] = alignment
    return alignments


def fielding_lines_for(
    game: Game, alignments: dict[int, dict[FieldingPosition, int]] | None = None
) -> dict[int, FieldingLine]:
    """1試合の守備成績を、守備に就いた全選手ぶん打席から導く。

    守備機会が無くても、守備に就いていた選手は 0 の行で現れる（試合数に数えるため）。
    打席の記録が無い試合では空。失策は `errors`（守備者の id）から数えるので、
    経路の位置が解決できなくても失策は数える。
    """
    putouts: Counter[int] = Counter()
    assists: Counter[int] = Counter()
    errors: Counter[int] = Counter()
    double_plays: Counter[int] = Counter()
    present: set[int] = set()

    if alignments is None:
        alignments = fielders_by_plate_appearance(game)
    for entry in game.plate_appearances:
        alignment = alignments[entry.sequence]
        present.update(alignment.values())
        credits = fielding_credits(entry)
        for position in credits.putouts:
            if position in alignment:
                putouts[alignment[position]] += 1
        for position in credits.assists:
            if position in alignment:
                assists[alignment[position]] += 1
        for player_id in {alignment[position] for position in credits.double_play if position in alignment}:
            double_plays[player_id] += 1
        for error in entry.errors:
            errors[error.player_id] += 1
            present.add(error.player_id)

    return {
        player_id: FieldingLine(
            putouts=putouts[player_id],
            assists=assists[player_id],
            errors=errors[player_id],
            double_plays_turned=double_plays[player_id],
        )
        for player_id in sorted(present)
    }


def fielding_line_for(game: Game, player_id: int) -> FieldingLine:
    """1人の守備成績。守備に就かなかった選手は 0 の行。"""
    return fielding_lines_for(game).get(player_id, FieldingLine())


def record_derived_fielding(game: Game) -> None:
    """守備成績を打席から導いて集約に載せる。ラインアップ（`game.batting`）を載せた後に呼ぶ。

    保存する側（スコアブックの保存・投入コマンド・導き直し）が呼び忘れると、
    集約の照合が弾く（打席があるのに守備成績が空の集約は保存できない）。
    """
    game.fielding = []
    for player_id, line in fielding_lines_for(game).items():
        game.record_fielding(player_id, line)


def hits_by_inning(plate_appearances: Iterable[PlateAppearance], *, home: bool) -> dict[int, int]:
    """チームの回ごとの安打。スコアボードの H 欄の出典。

    ホームは裏に、ビジターは表に打つ。打席が無い回は載せない。
    """
    counts: dict[int, int] = {}
    for entry in plate_appearances:
        if entry.is_bottom == home and entry.result.is_hit:
            counts[entry.inning] = counts.get(entry.inning, 0) + 1
    return counts


def errors_by_inning(plate_appearances: Iterable[PlateAppearance], *, home: bool) -> dict[int, int]:
    """チームの回ごとの失策。スコアボードの E 欄の出典。

    **失策は守備側のチームに付く。** ホームが守るのは表なので、ホームの失策は
    表の打席に記録された失策を数える（失策を犯したのが誰かではなく、どちらの
    半回かで決まる。選手の所属チームを引き直さずに済む）。
    """
    counts: dict[int, int] = {}
    for entry in plate_appearances:
        if entry.is_bottom != home:
            counts[entry.inning] = counts.get(entry.inning, 0) + len(entry.errors)
    return counts


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
    （3,480試合を再生すると約68秒かかる。詳細は README「打席を出典にした理由」）。

    同じ事実を2か所に持つことになるので、**集約が照合する**。イニングスコアと
    最終得点を突き合わせる `ensure_line_score_matches()` と同じ形で、片方だけを
    書き換えた記録が保存されるのを防ぐ。打席の記録が無い試合では何もしない。
    """
    if not game.plate_appearances:
        return

    game.ensure_lineup_consistent()
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

    _ensure_fielding_matches_plate_appearances(game)


def _ensure_fielding_matches_plate_appearances(game: Game) -> None:
    """守備成績も打席から導いた値と一致することを確かめる。

    **打席があるのに守備成績が空の集約は弾く。** 呼び忘れたまま保存すると、既存の守備行が
    全部消える（エラーにならない）。載せるなら**守備に就いた全員ぶん**が要る。1人でも欠けると、
    その選手の試合数が通算から静かに落ちる。

    失策の守備者が、その位置に就いている選手と食い違う記録も弾く（解決できない位置は比べない）。
    """
    alignments = fielders_by_plate_appearance(game)
    for entry in game.plate_appearances:
        for error in entry.errors:
            holder = alignments[entry.sequence].get(error.position)
            if holder is not None and holder != error.player_id:
                half = "裏" if entry.is_bottom else "表"
                raise InvalidGame(
                    f"{entry.inning}回{half}の失策の守備者（選手id={error.player_id}）が、"
                    f"{error.position.label}を守っている選手（選手id={holder}）と食い違っています。"
                )

    counted = fielding_lines_for(game, alignments)
    if not game.fielding:
        raise InvalidPlateAppearance("打席の記録がある試合には、守備成績（打席から導いた値）が要ります。")
    for fielder in game.fielding:
        expected = counted.get(fielder.player_id, FieldingLine())
        if fielder.line != expected:
            raise InvalidPlateAppearance(
                f"守備成績が打席の記録と一致しません（選手id={fielder.player_id}）。"
                f"打席から数え直すと 刺殺{expected.putouts}・補殺{expected.assists}・失策{expected.errors} です。"
                "この試合は打席が出典なので、成績だけを書き換えることはできません。"
            )

    absent = set(counted) - {fielder.player_id for fielder in game.fielding}
    if absent:
        raise InvalidPlateAppearance(f"守備に就いた選手の守備成績がありません（選手id={sorted(absent)}）。")
