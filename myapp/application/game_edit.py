"""試合の編集画面の材料の組み立て（`Game` 集約 → DTO）。

編集画面（React）が要る形は、集約の持つ形と少し違う。集約から画面の材料を取り出す
判断（保存済みの出場時点を「何回・表裏・何人目」に戻す、代打・代走・投手の出場時点は
入力欄に出さない、など）は業務の判断なので、presentation ではなくここに置く。
presentation はできあがった DTO を JSON に写すだけにする。
"""

from __future__ import annotations

from ..domain.entities import Game, PlateAppearance
from .dto import (
    GameEditError,
    GameEditHeader,
    GameEditLineupSlot,
    GameEditPlateAppearance,
    GameEditRunnerAdvance,
)


def build_header(game_id: int, game: Game) -> GameEditHeader:
    """試合の基本項目。得点は打席から導かれた値で、画面では読み取り専用。"""
    return GameEditHeader(
        id=game_id,
        year=game.season.year,
        played_on=game.played_on,
        home_team_id=game.home_team_id,
        away_team_id=game.away_team_id,
        home_score=game.home_score,
        away_score=game.away_score,
    )


def build_lineup_slots(game: Game) -> dict[int, GameEditLineupSlot]:
    """選手 id → 打順の1行。打順に入っている選手だけ。

    保存済みの出場した打席を、入力と同じ（回・表裏・その半回の何人目）に戻す。
    """
    located: dict[int, tuple[int, bool, int]] = {}
    counts: dict[tuple[int, bool], int] = {}
    for entry in game.plate_appearances_in_order():
        half = (entry.inning, entry.is_bottom)
        counts[half] = counts.get(half, 0) + 1
        located[entry.sequence] = (entry.inning, entry.is_bottom, counts[half])

    slots = {}
    for slot in game.batting:
        if slot.batting_order is None:
            continue
        position = slot.fielding_position
        # 代打・代走・投手は入った時点を打席から導くので、入力欄には出さない（返して再保存すると、
        # 打席より前に入ったことになりうる）。守備固めなどは、保存した値を返して直せるようにする
        derived = position is not None and position.entry_is_derived
        inning, is_bottom, batter = located.get(slot.entered_sequence or 0, (None, False, 1))
        slots[slot.player_id] = GameEditLineupSlot(
            player_id=slot.player_id,
            batting_order=slot.batting_order,
            slot_sequence=slot.slot_sequence,
            fielding_position=position.value if position else "",
            entered_inning=None if derived else inning,
            entered_is_bottom=False if derived else is_bottom,
            entered_batter=1 if derived else batter,
        )
    return slots


def build_plate_appearances(game: Game) -> list[GameEditPlateAppearance]:
    """打席を記録の順に。保存 API に送り返す形と同じ粒度の値だけを持つ。"""
    return [_to_row(entry) for entry in game.plate_appearances_in_order()]


def _to_row(entry: PlateAppearance) -> GameEditPlateAppearance:
    return GameEditPlateAppearance(
        sequence=entry.sequence,
        inning=entry.inning,
        is_bottom=entry.is_bottom,
        batter_id=entry.batter_id,
        pitcher_id=entry.pitcher_id,
        batting_order=entry.batting_order,
        slot_sequence=entry.slot_sequence,
        result=entry.result.value,
        fielded_by=tuple(position.value for position in entry.fielded_by),
        advances=[
            GameEditRunnerAdvance(
                runner_id=advance.runner_id,
                from_base=advance.from_base.value,
                to_base=advance.to_base.value,
                reason=advance.reason.value,
                error_index=advance.error_index,
            )
            for advance in entry.advances
        ],
        errors=[
            GameEditError(player_id=error.player_id, position=error.position.value, kind=error.kind.value)
            for error in entry.errors
        ],
    )
