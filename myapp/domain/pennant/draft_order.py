"""ドラフト会議の指名順の規則（世界ごとの設定）。**純粋な値と関数**で、DB にも Django にも触れない。

規則は4つ（`DraftOrderRule`）。既定の NPB 方式は、NPB「選択手順」（ https://draft.npb.jp/draft/2025/schedule.html ）の
とおり、1巡目は全球団の同時入札（重複は抽選・外れは再入札）、2巡目は「球団順位の逆順」、3巡目は「球団順位」、
以後は折り返す。「球団順位の逆順」は**その年度の公式戦の球団順位**の逆順（CS・日本シリーズの結果は使わない）で、
優先権のあるリーグの最下位 → 他方の最下位 → 優先リーグの5位 → … と並べる。

1〜8リーグへの写し方（一般化）: 各リーグの**下からの順位**で段を作り（最下位の段 → 下から2番目の段 → …）、
段の中はその年のリーグの優先順（`world.league_priority`。年ごとに回す）で並べる。球団の少ないリーグは上の段で抜ける。
同率は優勝と同じ規定（`race.final_order`）で決めた全順序（`pennant_race.league_finishes`）をそのまま使う。

**どの球団が次に動くか**は規則ごとの戦略（`DraftOrderStrategy`）が返し、会議の進め方（`draft_meeting.py`）は
規則を知らない。乱数は `game_seed(世界のシード, 年, "draft-order")`（くじ順）で、同じ世界・同じ年なら同じ順。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from ..simulation.randomness import game_seed, make_random


class DraftOrderRule(Enum):
    """ドラフト会議の指名順の規則。**指名順の選択肢の唯一の出典**（作成フォーム・設定の画面・永続化の choices は
    ここから作る）。値は画面に出す名前。規則を足したら `strategy_for` にも足す（足し忘れはテストが見張る）。"""

    NPB = "NPB 方式"
    WAIVER = "完全ウェーバー"
    RANDOM_SNAKE = "くじ順"
    ALL_LOTTERY = "全巡入札"

    @property
    def label(self) -> str:
        return self.value

    @property
    def description(self) -> str:
        """画面に出す1行の説明（新人の質が前年の順位にどう効くか。判断21）。"""
        return _DESCRIPTIONS[self]


_DESCRIPTIONS = {
    DraftOrderRule.NPB: (
        "1巡目は全球団の入札と抽選、2巡目からは公式戦の下位の球団から折り返して指名します（NPB の現行の方式）。"
    ),
    DraftOrderRule.WAIVER: (
        "どの巡も公式戦の下位の球団から指名します。抽選はありません（戦力がいちばん均等になります）。"
    ),
    DraftOrderRule.RANDOM_SNAKE: "毎年くじで決めた順に、巡ごとに折り返して指名します。前年の順位に左右されません。",
    DraftOrderRule.ALL_LOTTERY: "どの巡も全球団の入札と抽選で決めます。前年の順位に左右されず、いちばん運に寄ります。",
}


@dataclass(frozen=True)
class DraftStandings:
    """会議の年の公式戦の最終順位。`leagues` はリーグごとの球団（1位から順）で、リーグはその年の優先順に並べる。

    `unranked` は順位の無い球団（その年に公式戦をしていない。ふつうは無い）で、最下位の段より前に id の順で置く。
    """

    leagues: tuple[tuple[int, ...], ...]
    unranked: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "leagues", tuple(tuple(league) for league in self.leagues))
        object.__setattr__(self, "unranked", tuple(sorted(self.unranked)))
        ids = [team_id for league in self.leagues for team_id in league] + list(self.unranked)
        if len(ids) != len(set(ids)):
            raise ValueError("最終順位に同じ球団が2回あります。")

    @property
    def team_ids(self) -> frozenset[int]:
        return frozenset(team_id for league in self.leagues for team_id in league) | frozenset(self.unranked)

    def reverse_order(self) -> tuple[int, ...]:
        """「球団順位の逆順」。下からの順位の段ごとに、段の中はリーグの優先順。球団の少ないリーグは上の段で抜ける。"""
        depth = max((len(league) for league in self.leagues), default=0)
        ordered = list(self.unranked)
        for level in range(depth):
            ordered.extend(league[len(league) - 1 - level] for league in self.leagues if level < len(league))
        return tuple(ordered)

    def standing_order(self) -> tuple[int, ...]:
        """「球団順位」（逆順をひっくり返した並び。NPB の3巡目）。"""
        return tuple(reversed(self.reverse_order()))


class DraftOrderStrategy(Protocol):
    """規則ごとの指名順。巡（1から）ごとに、入札の巡か・球団が動く順を返す。

    入札の巡の順は、入札を出す順（結果には効かない。画面の並び）。選択を終えた球団・人数を満たした球団を
    飛ばすのは会議の側（`draft_meeting.DraftState`）。
    """

    def is_bidding(self, round_number: int) -> bool: ...

    def order(self, round_number: int) -> tuple[int, ...]: ...


@dataclass(frozen=True)
class NpbOrder:
    """NPB 方式: 1巡目は入札、偶数巡は逆順、3巡目からの奇数巡は順位順。"""

    standings: DraftStandings

    def is_bidding(self, round_number: int) -> bool:
        return round_number == 1

    def order(self, round_number: int) -> tuple[int, ...]:
        if round_number == 1 or round_number % 2 == 0:
            return self.standings.reverse_order()
        return self.standings.standing_order()


@dataclass(frozen=True)
class WaiverOrder:
    """完全ウェーバー: どの巡も逆順（折り返さない）。抽選なし。"""

    standings: DraftStandings

    def is_bidding(self, round_number: int) -> bool:
        return False

    def order(self, round_number: int) -> tuple[int, ...]:
        return self.standings.reverse_order()


@dataclass(frozen=True)
class SnakeOrder:
    """くじ順: 毎年くじで決めた順を、巡ごとに折り返す。抽選なし。"""

    lottery_order: tuple[int, ...]

    def is_bidding(self, round_number: int) -> bool:
        return False

    def order(self, round_number: int) -> tuple[int, ...]:
        return self.lottery_order if round_number % 2 == 1 else tuple(reversed(self.lottery_order))


@dataclass(frozen=True)
class AllLotteryOrder:
    """全巡入札: どの巡も入札と抽選。入札を出す順は逆順（結果には効かない）。"""

    standings: DraftStandings

    def is_bidding(self, round_number: int) -> bool:
        return True

    def order(self, round_number: int) -> tuple[int, ...]:
        return self.standings.reverse_order()


def lottery_order(team_ids: Sequence[int], *, world_seed: int, year: int) -> tuple[int, ...]:
    """くじ順の、その年の順（球団 id の順から決定的に並べ替える）。同じ世界・同じ年なら同じ順。"""
    rng = make_random(game_seed(world_seed, year, "draft-order"))
    remaining = sorted(team_ids)
    ordered: list[int] = []
    while remaining:
        ordered.append(remaining.pop(min(len(remaining) - 1, int(rng.random() * len(remaining)))))
    return tuple(ordered)


def strategy_for(rule: DraftOrderRule, standings: DraftStandings, *, world_seed: int, year: int) -> DraftOrderStrategy:
    """規則の戦略。規則を足して戦略を足し忘れると KeyError（テストが見張る）。"""
    strategies: dict[DraftOrderRule, Callable[[], DraftOrderStrategy]] = {
        DraftOrderRule.NPB: lambda: NpbOrder(standings),
        DraftOrderRule.WAIVER: lambda: WaiverOrder(standings),
        DraftOrderRule.RANDOM_SNAKE: lambda: SnakeOrder(
            lottery_order(sorted(standings.team_ids), world_seed=world_seed, year=year)
        ),
        DraftOrderRule.ALL_LOTTERY: lambda: AllLotteryOrder(standings),
    }
    return strategies[rule]()
