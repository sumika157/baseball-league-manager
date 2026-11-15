"""ペナントの首脳陣（#210 M1: 監督）の読み口と、画面向けの参照のサービス。

- 読み口（`StaffReader`）: 進める処理が、その年の球団ごとの采配の傾向を1回のクエリで読む。**監督のいない球団
  （この機能より前の世界）は標準の傾向（`Tactics.STANDARD`）で補う。補う場所はここ1つ**
- 参照のサービス（`PennantStaffViewService`）: GM ホームの「監督」カード・球団史の「監督」列・オフの結果の
  「監督の交代」。参照クエリ（`StaffQuery`）から DTO を作るだけで、書き込みの導線は持たない。
  球団の画面のサービス（`TeamViewService`）には足さない（対象が違う。CLAUDE.md「サービスは対象ごとに分ける」）

書くのは世界の作成（`PennantWorldService`）とシーズンを締める処理（`PennantOffseasonService`）だけ。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from ..domain.pennant.staff import ManagerStyle, StaffRole, age_in_year, tactics_of
from ..domain.repositories import StaffRepository
from ..domain.simulation.tactics import TacticAxis, Tactics
from .dto import ManagerCard, ManagerChangeRow, ManagerHistoryRow, StaffTenureRow, TacticCell
from .queries import StaffQuery


class StaffReader:
    """首脳陣の読み口（世界ひとつ。リポジトリは世界の範囲で組み立てて渡す）。"""

    def __init__(self, *, staff: StaffRepository) -> None:
        self._staff = staff

    def tactics_for(self, year: int, team_ids: Iterable[int]) -> dict[int, Tactics]:
        """その年の球団ごとの采配の傾向。監督のいない球団は標準の傾向で補う。"""
        managers = {
            item.tenure.team_id: tactics_of(item.member.style)
            for item in self._staff.find_serving(year)
            if item.tenure.role is StaffRole.MANAGER
        }
        return {team_id: managers.get(team_id, Tactics.STANDARD) for team_id in team_ids}


def tactic_cells(style: ManagerStyle) -> tuple[TacticCell, ...]:
    """監督の方針の4軸の表記（軸の順は `TacticAxis`）。"""
    tactics = tactics_of(style)
    return tuple(
        TacticCell(title=axis.title, label=axis.label(tactics.level(axis)), level=int(tactics.level(axis)))
        for axis in TacticAxis
    )


def style_summary(cells: Sequence[TacticCell]) -> str:
    """方針のうち標準でない軸の表記を「・」でつなぐ。すべて標準なら「標準」。"""
    return "・".join(cell.label for cell in cells if cell.level) or "標準"


def _age(row: StaffTenureRow, year: int) -> int:
    return age_in_year(row.birth_date, year)


def _managers(rows: Sequence[StaffTenureRow]) -> list[StaffTenureRow]:
    return [row for row in rows if row.role is StaffRole.MANAGER]


class PennantStaffViewService:
    """首脳陣の画面向けの参照（世界ひとつ）。"""

    def __init__(self, *, staff: StaffQuery) -> None:
        self._staff = staff

    def manager_card(self, team_id: int | None, year: int) -> ManagerCard | None:
        """その年に球団を率いる監督のカード。受け持つ球団が無い・監督のいない年は None。"""
        if team_id is None:
            return None
        row = next((row for row in _managers(self._staff.for_team(team_id)) if row.covers(year)), None)
        if row is None:
            return None
        return ManagerCard(
            year=year,
            name=row.name,
            name_kana=row.name_kana,
            age=_age(row, year),
            tenure_year=year - row.first_year + 1,
            tactics=tactic_cells(row.style),
        )

    def manager_history(self, team_id: int, years: Sequence[int]) -> list[ManagerHistoryRow]:
        """球団の年ごとの監督（`years` と同じ順）。監督のいない年は名前が空。"""
        managers = _managers(self._staff.for_team(team_id))
        return [
            ManagerHistoryRow(year=year, name=next((row.name for row in managers if row.covers(year)), ""))
            for year in years
        ]

    def manager_changes(self, year: int) -> list[ManagerChangeRow]:
        """`year` を締めたときの監督の交代（球団の id の順）。後任が就いた球団だけ。"""
        rows = _managers(self._staff.changed_at(year))
        retired = {row.team_id: row for row in rows if row.last_year == year}
        return [
            ManagerChangeRow(
                team_id=row.team_id,
                team_name=row.team_name,
                successor_name=row.name,
                successor_age=_age(row, year + 1),
                successor_style=style_summary(tactic_cells(row.style)),
                predecessor_name=retired[row.team_id].name if row.team_id in retired else "",
                predecessor_age=_age(retired[row.team_id], year) if row.team_id in retired else None,
            )
            for row in rows
            if row.first_year == year + 1
        ]
