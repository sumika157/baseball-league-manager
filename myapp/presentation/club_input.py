"""編成画面のフォームの読み取り。

画面はフォームの値から**選手の id と守備位置を取り出すだけ**で、編成として成り立つかの検査は
ドメイン（`ClubPlan`）が行う。ここが扱うのは「送られた値が読めるか」だけで、キーの欠落を
「空」と見なさない（欠落したまま保存すると、既存の編成が空で上書きされる）。

エラーのときに入力を残して再表示できるよう、検査前の値を読み出す関数（`read_*`）と、
欠落や食い違いを弾く関数（`parse_*`）に分けている。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from django.http import QueryDict

from ..application.dto import ClubPlanView, ClubPlayerDetail
from ..domain.exceptions import DomainError
from ..domain.pennant.club_plan import LINEUP_POSITIONS, LineupChoice, PlanSection
from ..domain.value_objects import FieldingPosition

# 画面で区画を指す名前（hidden の `section`）と、区画の対応
SECTIONS: Mapping[str, PlanSection] = {
    "active": PlanSection.ACTIVE,
    "lineup": PlanSection.LINEUP,
    "rotation": PlanSection.ROTATION,
    "closer": PlanSection.CLOSER,
}
# 区画があるタブ
TAB_OF_SECTION: Mapping[PlanSection, str] = {
    PlanSection.ACTIVE: "active",
    PlanSection.LINEUP: "lineup",
    PlanSection.ROTATION: "pitching",
    PlanSection.CLOSER: "pitching",
}
TABS = ("active", "lineup", "pitching")
DEFAULT_TAB = "active"

ACTION_MANUAL = "manual"
ACTION_SAVE = "save"
ACTION_AUTO = "auto"
ACTIONS = (ACTION_MANUAL, ACTION_SAVE, ACTION_AUTO)

REOPEN = "画面が古くなっています。開き直してからもう一度お試しください。"


class ClubInputError(Exception):
    """フォームの値が読めない（欠落・数でない値・古い画面）。メッセージは画面にそのまま出せる日本語。"""


@dataclass(frozen=True)
class LineupSlotInput:
    """オーダーの1行の入力（検査前）。選ばれていなければ None。"""

    order: int
    player_id: int | None
    position: FieldingPosition | None


@dataclass(frozen=True)
class RotationSlotInput:
    """ローテーションの1枠の入力（検査前）。空なら None。"""

    order: int
    player_id: int | None


@dataclass(frozen=True)
class PlayerOption:
    """編成の select に並べる選手1人。1軍外の選手（保存済みの上書きに残っているもの）には印を付ける。"""

    player_id: int
    label: str


def player_option(detail: ClubPlayerDetail) -> PlayerOption:
    """選手の選択肢。選ぶ材料として、能力の総合と年齢を添える（能力・生年月日が無ければ省く）。"""
    row = detail.player
    notes = [row.position.value] + (["外国人"] if row.is_foreign else []) + ([] if row.is_active else ["1軍外"])
    if detail.overall is not None:
        notes.append(f"総合{detail.overall.grade} {detail.overall.value}")
    if detail.age is not None:
        notes.append(f"{detail.age}歳")
    return PlayerOption(row.player_id, f"{row.name}（{'・'.join(notes)}）")


@dataclass(frozen=True)
class NoticeLink:
    """ホームの「注意」に出す1件。どのタブを開けばよいかを添える。"""

    section: str
    reason: str
    tab: str
    # False なら区画は自動に落ちず、効かない理由を知らせるだけ
    falls_back: bool = True


def tab_of(value: str | None) -> str:
    """URL の `?tab=`。不正な値は既定のタブに落とす（エラーにしない）。"""
    return value if value in TABS else DEFAULT_TAB


def to_int(value: str | None) -> int | None:
    """数として読めなければ None。"""
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


def _ints(values: Sequence[str]) -> list[int]:
    result = []
    for value in values:
        number = to_int(value)
        if number is None:
            raise ClubInputError(REOPEN)
        result.append(number)
    return result


# --- 検査前の値（エラーのとき、入力を残して再表示するために使う） ---


def read_active(post: QueryDict) -> frozenset[int]:
    """チェックされた選手（読めない値は無視する）。"""
    return frozenset(number for number in map(to_int, post.getlist("active")) if number is not None)


def read_lineup(post: QueryDict, size: int) -> list[LineupSlotInput]:
    """打順 1〜size の入力。欠けている・読めない行は player_id / position が None。"""
    slots = []
    for order in range(1, size + 1):
        label = post.get(f"lineup_position_{order}", "")
        try:
            position = FieldingPosition.from_label(label)
        except DomainError:
            # 守備位置として読めない値は未選択に落とす（検査前の読み取りなので止めない。保存では parse_lineup が弾く）
            position = None
        if position not in LINEUP_POSITIONS:
            position = None
        slots.append(LineupSlotInput(order, to_int(post.get(f"lineup_player_{order}")), position))
    return slots


def read_rotation(post: QueryDict, size: int) -> list[RotationSlotInput]:
    return [RotationSlotInput(order, to_int(post.get(f"rotation_{order}"))) for order in range(1, size + 1)]


# --- 欠落と食い違いを弾いて、サービスに渡す値にする ---


def parse_active(post: QueryDict, current: ClubPlanView) -> list[int]:
    """1軍登録。表示した全選手の id（`shown`）が今の在籍と食い違えば古い画面として弾く。

    チェックボックスは外すと何も送られないので、`shown` と突き合わせないと「外した」と「欠けた」を区別できない。
    """
    shown = _ints(post.getlist("shown"))
    if not shown or set(shown) != {row.player_id for row in current.players}:
        raise ClubInputError(REOPEN)
    checked = _ints(post.getlist("active"))
    if not set(checked) <= set(shown):
        raise ClubInputError(REOPEN)
    return [row.player_id for row in current.players if row.player_id in checked]


def parse_lineup(post: QueryDict, size: int) -> list[LineupChoice]:
    """オーダー。どの打順も選手と守備位置が必須（キーが欠けた行を空として扱わない）。"""
    choices = []
    for slot in read_lineup(post, size):
        if (
            f"lineup_player_{slot.order}" not in post
            or f"lineup_position_{slot.order}" not in post
            or slot.player_id is None
            or slot.position is None
        ):
            raise ClubInputError(f"{slot.order}番の選手と守備位置を選んでください。")
        choices.append(LineupChoice(slot.player_id, slot.position))
    return choices


def parse_rotation(post: QueryDict, size: int) -> list[int]:
    """ローテーション。枠のキーがすべて揃っているときだけ読む（空の枠は使わない枠として飛ばす）。"""
    if any(f"rotation_{order}" not in post for order in range(1, size + 1)):
        raise ClubInputError(REOPEN)
    return [slot.player_id for slot in read_rotation(post, size) if slot.player_id is not None]


def parse_closer(post: QueryDict) -> int:
    closer = to_int(post.get("closer"))
    if closer is None:
        raise ClubInputError("抑えの投手を選んでください。")
    return closer


@dataclass(frozen=True)
class ClubFormState:
    """保存に失敗したときの、送られた入力。画面に残して再表示する（区画の種類のものだけ入る）。"""

    section: PlanSection
    error: str
    active_checked: frozenset[int] | None = None
    lineup: list[LineupSlotInput] | None = None
    rotation: list[RotationSlotInput] | None = None
    closer_id: int | None = None


def failed_state(
    post: QueryDict, section: PlanSection, error: str, *, lineup_size: int, rotation_size: int
) -> ClubFormState:
    """失敗した区画の入力を、検査前の値のまま取り出す。"""
    if section is PlanSection.ACTIVE:
        return ClubFormState(section, error, active_checked=read_active(post))
    if section is PlanSection.LINEUP:
        return ClubFormState(section, error, lineup=read_lineup(post, lineup_size))
    if section is PlanSection.ROTATION:
        return ClubFormState(section, error, rotation=read_rotation(post, rotation_size))
    return ClubFormState(section, error, closer_id=to_int(post.get("closer")))
