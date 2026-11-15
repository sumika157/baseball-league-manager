"""守備のテストで使う組み立て（打席の帳面・試合・ホームのラインアップ）。

`test_fielding`・`test_fielding_lines` が使う。

ビジターが表、ホームが裏に攻める。守備を見るのは主にホーム（表の間に守る）で、
ホームの打者は id 21〜29、投手は 100・101。ビジターの打者は 1〜9、投手は 200。

**ラインアップの行にはチームが無い**ので、選手が打席に現れて初めてどちらの側か分かる。
そのためホームの打者を一巡させる（`_Book.home_bats_around`）ところから始める。
"""

from datetime import date

from myapp.domain import services
from myapp.domain.entities import Game, PlateAppearance, RunnerAdvance
from myapp.domain.value_objects import (
    AdvanceReason,
    Base,
    BattingLine,
    FieldingPosition,
    GameStage,
    PlateAppearanceResult,
    Season,
)

P = PlateAppearanceResult
R = AdvanceReason
FP = FieldingPosition

HOME, AWAY = 1, 2
STARTER, RELIEVER, AWAY_PITCHER = 100, 101, 200

# 打順 → (選手 id, 守備位置)。指名打者制で、投手は打順にいない
HOME_LINEUP = [
    (21, FP.SHORTSTOP),
    (22, FP.SECOND_BASE),
    (23, FP.FIRST_BASE),
    (24, FP.THIRD_BASE),
    (25, FP.LEFT_FIELD),
    (26, FP.CENTER_FIELD),
    (27, FP.RIGHT_FIELD),
    (28, FP.CATCHER),
    (29, FP.DESIGNATED_HITTER),
]
SHORT, SECOND, FIRST, THIRD, LEFT, CENTER, RIGHT, CATCHER, DH = (player for player, _ in HOME_LINEUP)


def _move(runner_id, frm, to, reason) -> RunnerAdvance:
    return RunnerAdvance(runner_id=runner_id, from_base=frm, to_base=to, reason=reason)


class _Book:
    """打席を積んでいく。通し番号は積んだ順に振り、`add` が返す。"""

    def __init__(self) -> None:
        self.entries: list[PlateAppearance] = []

    def add(
        self,
        *,
        bottom=False,
        order=1,
        result=P.GROUND_OUT,
        batter=None,
        pitcher=None,
        slot=0,
        fielded_by=(),
        advances=None,
        errors=(),
    ) -> int:
        sequence = len(self.entries) + 1
        batter_id = batter if batter is not None else (20 if bottom else 0) + order
        if advances is None:
            advances = [_move(batter_id, Base.BATTER, result.default_batter_base, result.default_batter_reason)]
        self.entries.append(
            PlateAppearance(
                sequence=sequence,
                inning=1,
                is_bottom=bottom,
                batter_id=batter_id,
                pitcher_id=pitcher if pitcher is not None else (AWAY_PITCHER if bottom else STARTER),
                batting_order=order,
                slot_sequence=slot,
                result=result,
                fielded_by=tuple(fielded_by),
                advances=list(advances),
                errors=list(errors),
            )
        )
        return sequence

    def home_bats_around(self) -> None:
        """ホームの打者を一巡させる。これでホームの全員が「裏に攻める側」だと分かる。"""
        for order in range(1, 10):
            self.add(bottom=True, order=order, result=P.STRIKEOUT_SWINGING)


def _game(book: _Book, *, extra=(), lineup=None) -> Game:
    """記録から試合を作る。

    extra は途中出場の行 (選手, 打順, 交代の順, 守備位置, 出場した打席)。ホームの行として足す。
    lineup はホームのスタメンを差し替えるとき（打順 → (選手 id, 守備位置)）。
    """
    game = Game(
        season=Season(2026),
        played_on=date(2026, 4, 1),
        home_team_id=HOME,
        away_team_id=AWAY,
        stage=GameStage.REGULAR,
        plate_appearances=list(book.entries),
    )
    for order, (player, position) in enumerate(lineup or HOME_LINEUP, start=1):
        game.record_batting(player, BattingLine(), team_id=HOME, batting_order=order, fielding_position=position)
    for player, order, slot, position, entered in extra:
        game.record_batting(
            player,
            BattingLine(),
            team_id=HOME,
            batting_order=order,
            slot_sequence=slot,
            fielding_position=position,
            entered_sequence=entered,
        )
    # 打席に立った選手は打撃成績の行が要る（照合が見る）。ビジターの打者は id が 20 未満
    recorded = {entry.player_id for entry in game.batting}
    for batter in {entry.batter_id for entry in book.entries} - recorded:
        game.record_batting(batter, BattingLine(), team_id=HOME if batter >= 20 else AWAY)
    for entry in game.batting:
        entry.line = services.batting_line_for(game.plate_appearances, entry.player_id)
    return game
