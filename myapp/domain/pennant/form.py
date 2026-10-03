"""球団の「勢い」（直近の成績と連続）。保存せず、試合の結果から導く。Django に依存しない。

勝ち負けの判定は `winning_team_id`（`domain/entities.py`）が唯一の出典で、ここでは
その結果を「○ ● △」に読み替えて並べるだけにする。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from ..entities import winning_team_id

# 直近の成績に数える試合数
RECENT_GAMES = 10


class Outcome(Enum):
    """1試合の結果。値は画面に出す記号。"""

    WIN = "○"
    LOSS = "●"
    TIE = "△"


# 連続の言い方。1試合だけなら「1勝」、2試合以上なら「3連勝」
_WORD = {Outcome.WIN: "勝", Outcome.LOSS: "敗", Outcome.TIE: "分"}


def outcome_for(team_id: int, home_team_id: int, away_team_id: int, home_score: int, away_score: int) -> Outcome:
    """`team_id`（ホームかビジターのどちらか）から見た結果。"""
    winner = winning_team_id(home_team_id, away_team_id, home_score, away_score)
    if winner is None:
        return Outcome.TIE
    return Outcome.WIN if winner == team_id else Outcome.LOSS


@dataclass(frozen=True)
class RecentForm:
    """直近の成績と、いまの連続。"""

    wins: int
    losses: int
    ties: int
    # 連続している結果と長さ（試合が無ければ None・0）。引分も連続として数える
    streak: Outcome | None
    streak_length: int

    @property
    def record(self) -> str:
        """「6勝4敗」「5勝4敗1分」。引分が無ければ省く。"""
        label = f"{self.wins}勝{self.losses}敗"
        return f"{label}{self.ties}分" if self.ties else label

    @property
    def streak_label(self) -> str:
        """「3連勝」「2連敗」「3連分」。1試合だけなら「1勝」「1敗」「1分」。"""
        if self.streak is None:
            return ""
        word = _WORD[self.streak]
        return f"1{word}" if self.streak_length == 1 else f"{self.streak_length}連{word}"


def recent_form(newest_first: Sequence[Outcome], window: int = RECENT_GAMES) -> RecentForm:
    """新しい試合から順に並べた結果から、直近 `window` 試合の成績と連続を求める。"""
    recent = list(newest_first[:window])
    streak = recent[0] if recent else None
    length = 0
    for outcome in newest_first:
        if outcome is not streak:
            break
        length += 1
    return RecentForm(
        wins=recent.count(Outcome.WIN),
        losses=recent.count(Outcome.LOSS),
        ties=recent.count(Outcome.TIE),
        streak=streak,
        streak_length=length,
    )
