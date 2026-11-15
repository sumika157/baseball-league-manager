"""ペナントのチケットの値段の読み口（#208 E2）。

球団 × 年の値段は、GM が手動にした行があればそれ、無ければ（お任せ・AI の球団）その年の人気から domain の規則
（`ticket_price.resolved_ticket_price`）で決める。**値段を決める読み方はここ1つ**で、進める処理（観客数）・
締める処理（人気への効き）・収支（`pennant_finance`）・画面が共有する。手動の値段は翌年に引き継がない。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ..domain.pennant.popularity import Popularity
from ..domain.pennant.ticket_price import TicketPrice, resolved_ticket_price
from ..domain.repositories import TicketPriceRepository


@dataclass(frozen=True)
class TicketPrices:
    """ある年の球団ごとの値段（`prices`）と、そのうち GM が手動にしたもの（`manual`）。"""

    prices: Mapping[int, TicketPrice]
    manual: Mapping[int, TicketPrice]


class TicketPriceReader:
    """チケットの値段の読み口（世界ひとつ。リポジトリは世界の範囲で組み立てて渡す）。"""

    def __init__(self, *, prices: TicketPriceRepository) -> None:
        self._prices = prices

    def manual(self, year: int) -> dict[int, TicketPrice]:
        """その年の、GM が手動にした値段だけ（球団 → 値段）。"""
        return {row.team_id: row.price for row in self._prices.find_by_year(year)}

    def manual_history(self, team_id: int) -> dict[int, TicketPrice]:
        """ひとつの球団の、手動にした値段（年 → 値段）。"""
        return {row.year: row.price for row in self._prices.find_by_team(team_id)}

    def for_year(self, year: int, popularity: Mapping[int, Popularity]) -> TicketPrices:
        """その年の球団ごとの値段。`popularity` はその年の球団ごとの人気（決め方は `resolve`）。"""
        manual = self.manual(year)
        return TicketPrices(prices=resolve(manual, popularity), manual=manual)


def resolve(manual: Mapping[int, TicketPrice], popularity: Mapping[int, Popularity]) -> dict[int, TicketPrice]:
    """球団ごとの値段。手動の値段があればそれ、無ければその年の人気から（domain の `resolved_ticket_price`）。"""
    return {team_id: resolved_ticket_price(manual.get(team_id), value.value) for team_id, value in popularity.items()}
