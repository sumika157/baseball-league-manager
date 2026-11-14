"""ペナントの球団の人気（#208 E1）の読み口と、シーズンを締めるときの翌年の人気の材料。

- 読み口（`PopularityReader`）: 進める処理（観客数）と GM ホームは、行の無い球団（この機能より前の世界）を
  初期値で補って読む（補いの規則は domain の `popularity.with_initial`。補う読み方はここ1つ）。
  球団史・オフの結果は保存した行だけを読む（人気を足す前の年に、作っていない値を出さない）
- 締める処理の材料（`popularity_basis`）: 締める年の公式戦の最終順位（勝率・リーグ優勝）と日本一、在籍する選手の
  実績（スター選手）、締める年のチケットの値段（#208 E2）から、翌年の人気を domain の `next_popularities` で導く。
  行の無い世界は、締める年の補った値も一緒に書く（締めた年の値を後から作らない）

人気は年ごとの状態として保存する（2026-11-14 ユーザー決定）。書くのは世界の作成とシーズンを締める処理だけ。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace

from ..domain.pennant.offseason import OffseasonClub
from ..domain.pennant.popularity import (
    ClubPopularity,
    Popularity,
    PopularityFactors,
    next_popularities,
    star_power,
    with_initial,
)
from ..domain.pennant.postseason import LeagueFinish
from ..domain.pennant.ticket_price import TicketPrice
from ..domain.repositories import PopularityRepository
from .pennant_ticket_price import resolve


class PopularityReader:
    """球団の人気の読み口（世界ひとつ。リポジトリは世界の範囲で組み立てて渡す）。"""

    def __init__(self, *, popularities: PopularityRepository) -> None:
        self._popularities = popularities

    def for_year(self, year: int, team_ids: Iterable[int]) -> dict[int, Popularity]:
        """その年の球団ごとの人気。行の無い球団は初期値で補う（進める処理と GM ホームが使う）。"""
        return with_initial(team_ids, self.recorded(year))

    def recorded(self, year: int) -> dict[int, Popularity]:
        """その年の、保存した人気だけ（補わない）。"""
        return {row.team_id: row.popularity for row in self._popularities.find_by_year(year)}

    def history(self, team_id: int) -> dict[int, Popularity]:
        """ひとつの球団の、保存した人気（年 → 人気。補わない）。"""
        return {row.year: row.popularity for row in self._popularities.find_by_team(team_id)}


def change_label(before: Popularity | None, after: Popularity) -> str:
    """人気の変化の表記（「+3」「-2」「+0」）。前の値が無ければ空。"""
    return "" if before is None else f"{after.value - before.value:+d}"


@dataclass(frozen=True)
class PopularityBasis:
    """締める年 `year` の人気の材料（締める処理がロックの外で読んで作る）。

    - `current`: 締める年の球団ごとの人気（行の無い球団は初期値で補った値）
    - `unrecorded_ids`: 締める年の行が無い球団（この機能より前の世界）
    - `factors`: 勝率・リーグ優勝・スター選手・チケットの値段（日本一は勝ち上がりから `following` で足す）
    """

    year: int
    current: Mapping[int, Popularity]
    unrecorded_ids: tuple[int, ...]
    factors: Mapping[int, PopularityFactors]

    def unrecorded(self) -> list[ClubPopularity]:
        """締める年の、行の無い球団の補った人気（締めた年の値を後から作らないよう、締めるときに一緒に書く）。"""
        return [ClubPopularity(team_id, self.year, self.current[team_id]) for team_id in self.unrecorded_ids]

    def following(self, *, japan_champion: int | None, world_seed: int) -> list[ClubPopularity]:
        """翌年の全球団の人気（domain の `next_popularities`）。`japan_champion` は締める年の日本一か None。"""
        factors = {
            team_id: replace(item, japan_champion=team_id == japan_champion) for team_id, item in self.factors.items()
        }
        following = next_popularities(self.current, factors, world_seed=world_seed, year=self.year)
        return [ClubPopularity(team_id, self.year + 1, value) for team_id, value in following.items()]


def popularity_basis(
    *,
    year: int,
    team_ids: Iterable[int],
    recorded: Mapping[int, Popularity],
    finishes: Sequence[LeagueFinish],
    clubs: Sequence[OffseasonClub],
    manual_prices: Mapping[int, TicketPrice],
) -> PopularityBasis:
    """締める年の人気の材料。勝率とリーグ優勝は公式戦の最終順位（全順序の先頭が優勝）から、スター選手は
    いま在籍している選手の実績から導く。最終順位に無い球団（その年に試合をしていない）は材料を持たない
    （domain が平凡な成績として扱う）。

    チケットの値段（#208 E2）は、締める年の手動の値段（`manual_prices`）か、補った人気からのお任せの規則
    （`pennant_ticket_price.resolve`。進める処理が観客数に使った値段と同じ）。
    """
    ordered = sorted(team_ids)
    current = with_initial(ordered, recorded)
    prices = resolve(manual_prices, current)
    stars = {club.team_id: star_power(player.track_record for player in club.players) for club in clubs}
    factors: dict[int, PopularityFactors] = {}
    for finish in finishes:
        for rank, row in enumerate(finish.standings, start=1):
            factors[row.team_id] = PopularityFactors(
                winning_percentage=row.record.winning_percentage,
                league_champion=rank == 1,
                star_power=stars.get(row.team_id, 0.0),
                ticket_price=prices.get(row.team_id, TicketPrice.STANDARD),
            )
    return PopularityBasis(
        year=year,
        current=current,
        unrecorded_ids=tuple(team_id for team_id in ordered if team_id not in recorded),
        factors=factors,
    )
