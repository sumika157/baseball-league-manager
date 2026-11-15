"""ペナントの選手の年俸の契約（#40 K1a）。**純粋な値と規則**で、DB にも Django にも触れない。

契約は「選手 × 期間（最初の年〜最後の年）× 年額」の**決めた事実**として保存する（相場・総額・減額の下限は
保存せず導く）。
球団は契約に持たせず、在籍から引く（K1a の契約はすべて1年。複数年契約は #40 K3a）。

- 書くのは世界の作成（開幕年）・シーズンを締める処理（翌年の契約と、行の無い選手の締める年の補った値）・
  期中の外国人の獲得（その年）だけ。締めた年の契約は書き換えない
- 同じ選手の契約は期間が重ならない（`ensure_no_overlap`。能力の行と同じく、リポジトリの `add_all` が
  保存済みの契約と合わせてトランザクションの中で通す）
- 金額は円の整数で、万円刻み。表記（「440万円」「1億2,000万円」）は `salary_label` が唯一の出典
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ..exceptions import InvalidSalaryContract
from ..value_objects import Season

# 1万円・1億円（円）
MAN = 10_000
OKU = 100_000_000


def salary_label(yen: int) -> str:
    """年俸の表記。**年俸の表記の唯一の出典**。

    億と万の位で書く（「440万円」「1億円」「1億2,000万円」「12億円」）。万円未満の端数は持たない（`Salary` が断る）。
    0 は「0円」。負の額（前年との差など）は先頭に「-」を付ける。
    """
    if yen < 0:
        return f"-{salary_label(-yen)}"
    oku, rest = divmod(yen, OKU)
    man = rest // MAN
    if oku and man:
        return f"{oku:,}億{man:,}万円"
    if oku:
        return f"{oku:,}億円"
    if man:
        return f"{man:,}万円"
    return "0円"


def salary_change_label(before: int, after: int) -> str:
    """年俸の増減の表記（「+1,000万円」「-500万円」「±0」）。"""
    change = after - before
    if change == 0:
        return "±0"
    return f"+{salary_label(change)}" if change > 0 else salary_label(change)


@dataclass(frozen=True, order=True)
class Salary:
    """年俸（年額。円の整数・万円刻み・正の値）。"""

    yen: int

    def __post_init__(self) -> None:
        if isinstance(self.yen, bool) or not isinstance(self.yen, int):
            raise InvalidSalaryContract("年俸は整数（円）で指定してください。")
        if self.yen <= 0:
            raise InvalidSalaryContract("年俸は正の額にしてください。")
        if self.yen % MAN:
            raise InvalidSalaryContract("年俸は万円単位にしてください。")

    @property
    def label(self) -> str:
        return salary_label(self.yen)


@dataclass(frozen=True)
class SalaryContract:
    """選手ひとりの年俸の契約（最初の年〜最後の年。年額 `salary`）。球団は持たない（在籍から引く）。"""

    player_id: int
    first_year: int
    last_year: int
    salary: Salary

    def __post_init__(self) -> None:
        object.__setattr__(self, "first_year", Season(self.first_year).year)
        object.__setattr__(self, "last_year", Season(self.last_year).year)
        if self.last_year < self.first_year:
            raise InvalidSalaryContract("契約の最後の年が最初の年より前になっています。")

    @classmethod
    def for_year(cls, player_id: int, year: int, salary: Salary) -> SalaryContract:
        """1年の契約（K1a の契約はすべてこれ）。"""
        return cls(player_id=player_id, first_year=year, last_year=year, salary=salary)

    def covers(self, year: int) -> bool:
        """`year` 年がこの契約の期間に入るか。"""
        return self.first_year <= year <= self.last_year

    def overlaps(self, other: SalaryContract) -> bool:
        """同じ選手の契約として期間が重なるか（別の選手なら重ならない）。"""
        return (
            self.player_id == other.player_id
            and self.first_year <= other.last_year
            and other.first_year <= self.last_year
        )


def ensure_no_overlap(contracts: Iterable[SalaryContract]) -> None:
    """同じ選手の契約の期間が重ならないこと（並びによらない）。重なれば `InvalidSalaryContract`。"""
    by_player: dict[int, list[SalaryContract]] = {}
    for contract in contracts:
        by_player.setdefault(contract.player_id, []).append(contract)
    for player_id, items in sorted(by_player.items()):
        ordered = sorted(items, key=lambda item: (item.first_year, item.last_year))
        for before, after in zip(ordered, ordered[1:], strict=False):
            if before.overlaps(after):
                raise InvalidSalaryContract(
                    f"同じ選手の契約の期間が重なっています（選手 id={player_id}・{after.first_year}年）。"
                )
