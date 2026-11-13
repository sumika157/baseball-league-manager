"""ドラフト会議の記録（#297 D3）。**純粋な値**で、DB にも Django にも触れない。

GM が1巡ずつ進める会議は、締める前の**入力**として保存する（戦力外の指名 `ReleasePlan` と同じ種類）。
保存するのは会議を始めたときに決まった事実と、行動の並びだけ:

- 候補の指紋（`ProspectPool.signature`。候補の身元だけ）。候補そのものは保存せず、決定的に作り直して指紋で照合する
- 人数の幅（`DraftSlot`。`draft_meeting.meeting_slots` が出典）。毎回の指名で全選手の引退を計算し直さないため、
  会議を始めたときに写す（「会議が何人の枠で行われたか」という会議の入力の事実）
- 行動（入札・指名・選択終了。`DraftAction`）の並び。巡・入札の回・抽選の当たり外れ・次の番は持たない
  （行動の並びから `DraftState` が導く）

会議の状態（`DraftMeetingStatus`）も保存しない（記録の有無と、再生した状態から導く）。範囲は世界 × 年で、
`Team` にも `Game` にも入れない（指名の重複・番の順序は会議全体を見ないと検査できないので、会議を範囲にする）。
締めた時点で消す（指名の事実は新人の `Profile.draft` と在籍の経路に移る）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import Enum

from ..exceptions import DraftChanged, DraftConflict, InvalidDraftAction
from .draft_meeting import DraftAction, DraftSlot, DraftState, ProspectPool
from .draft_order import DraftOrderStrategy
from .outlook import ScoutingAccuracy


class DraftMeetingStatus(Enum):
    """その年のドラフト会議の状態。導くだけで保存しない。値は画面に出す名前。"""

    NOT_STARTED = "未開始"
    IN_PROGRESS = "途中"
    FINISHED = "終了"

    @classmethod
    def of(cls, record: DraftRecord | None, state: DraftState | None) -> DraftMeetingStatus:
        """記録が無ければ未開始。記録があれば、再生した状態（`DraftRecord.state`）が終わっているかで途中か終了。"""
        if record is None:
            return cls.NOT_STARTED
        if state is None:
            raise ValueError("会議の記録があるときは、再生した状態を渡してください。")
        return cls.FINISHED if state.is_complete else cls.IN_PROGRESS


def pre_draft_input_blocker(*, meeting_started: bool) -> str | None:
    """会議の前に決める入力（戦力外の指名など）を変えられない理由。変えられるなら None。
    **会議の前に決める入力を変えられる条件の唯一の出典**（#297。#169 F2 の外国人の指名・#170 P2 の昇格も
    同じ関数を通す）。

    会議を始めると人数の幅（引退・戦力外・外国人の獲得から決まる）を写すので、その後に入力を変えると、写した幅と
    締めるときに計算し直す幅が食い違う。
    """
    if meeting_started:
        return "今年のドラフト会議が始まっているので、会議の前に決めることは変えられません。"
    return None


@dataclass(frozen=True)
class DraftRecord:
    """その年のドラフト会議の記録（世界 × 年）。

    - `year`: 締める年（会議を開く年）
    - `signature`: 会議を始めたときの候補の指紋（`ProspectPool.signature`）
    - `slots`: 会議を始めたときに写した人数の幅（球団 id の昇順）
    - `actions`: 行動の並び。`seq`（保存の番号）は位置 + 1
    """

    year: int
    signature: str
    slots: tuple[DraftSlot, ...]
    actions: tuple[DraftAction, ...] = ()

    def __post_init__(self) -> None:
        team_ids = [slot.team_id for slot in self.slots]
        if not team_ids:
            raise ValueError("会議に参加する球団がありません。")
        if team_ids != sorted(set(team_ids)):
            raise ValueError("人数の幅は球団 id の昇順に1つずつ並べてください。")
        if any(action.team_id not in self.team_ids for action in self.actions):
            raise InvalidDraftAction("この会議に参加していない球団の行動があります。")

    @classmethod
    def start(
        cls, year: int, pool: ProspectPool, slots: Sequence[DraftSlot], actions: Sequence[DraftAction] = ()
    ) -> DraftRecord:
        """会議を始めたときの記録。`pool` は作り直したばかりの候補（足した候補の無いもの）、`actions` は始めたときに
        進めた行動（GM の番まで AI を進めた分）。"""
        if pool.year != year:
            raise ValueError("候補の年と会議の年が違います。")
        ordered = tuple(sorted(slots, key=lambda slot: slot.team_id))
        return cls(year=year, signature=pool.signature, slots=ordered, actions=tuple(actions))

    @property
    def team_ids(self) -> frozenset[int]:
        return frozenset(slot.team_id for slot in self.slots)

    @property
    def last_seq(self) -> int:
        """最後の行動の番号（行動が無ければ 0）。画面を開いたときの値を `expected_seq` として送り、書く前に比べる。"""
        return len(self.actions)

    def state(
        self, strategy: DraftOrderStrategy, pool: ProspectPool, *, accuracy: ScoutingAccuracy | None = None
    ) -> DraftState:
        """記録を再生した会議の状態。`pool` は作り直した候補（`draw_pool` の結果。足した候補は再生が足し直す）。

        候補の指紋が違う・保存した行動が当てられない（コードが変わって候補か規則が変わった）ときは `DraftChanged`。
        """
        if pool.signature != self.signature:
            raise DraftChanged()
        try:
            return DraftState.open(strategy, self.slots, pool, self.actions, accuracy=accuracy)
        except InvalidDraftAction as error:
            raise DraftChanged() from error

    def ensure_same_slots(self, slots: Sequence[DraftSlot]) -> None:
        """締めるときに計算し直した人数の幅（`meeting_slots`）が、写した幅と同じか。違えば `DraftChanged`。"""
        if tuple(sorted(slots, key=lambda slot: slot.team_id)) != self.slots:
            raise DraftChanged()

    def appended(self, actions: Sequence[DraftAction], *, expected_seq: int) -> DraftRecord:
        """行動を足した記録。`expected_seq` が今の最後の番号と違えば（画面が古い）`DraftConflict`。"""
        if expected_seq != self.last_seq:
            raise DraftConflict()
        return replace(self, actions=(*self.actions, *actions))
