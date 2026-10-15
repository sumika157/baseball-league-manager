"""ペナントの歴代の記録と球団史の材料を作る。終わったシーズンだけを読み、何も保存しない。

- 年ごとの最終順位と優勝: `final_standings`（世界の終了と同じ関数。優勝の決め方は domain の `league_champion`）
- タイトルの受賞者: 世界の範囲で組み立てた `TeamApplicationService` の `get_league_titles`
  （在籍の期間と、その年の明細の SQL 集計。タイトルの画面と同じ出典）。**選んだリーグだけ**を読む

載せるのは終わったシーズン（domain の `completed_years`）だけ。いまのシーズンは優勝もタイトルも確定していない。
受賞者は成績だけで決まる部門に限る（MVP などの選考で決まる賞は持たない）。
"""

from __future__ import annotations

from ..domain.entities import League, Team
from ..domain.exceptions import TeamNotFound
from ..domain.pennant.season import completed_years
from ..domain.repositories import LeagueRepository, TeamRepository
from .dto import (
    ClubHistory,
    ClubHistorySeason,
    ClubTitle,
    LeagueChampionCell,
    LeagueOption,
    LeagueTitles,
    TitleColumn,
    TitleDepartment,
    TitleHolderCell,
    WorldContext,
    WorldHistory,
    WorldHistoryYear,
)
from .pennant_race import SeasonFinal, final_standings, team_order
from .queries import GameListQuery
from .services import TeamApplicationService
from .standings import record_label


class PennantHistoryService:
    """歴代の記録と球団史。`teams` は世界の範囲で組み立てたサービス。"""

    def __init__(
        self,
        *,
        teams: TeamApplicationService,
        games: GameListQuery,
        team_records: TeamRepository,
        leagues: LeagueRepository,
    ) -> None:
        self._teams = teams
        self._games = games
        # 年ごとの順位を作るために読む（順位の規則は domain の `standings`）
        self._team_records = team_records
        self._leagues = leagues

    def get_world_history(self, world: WorldContext, *, league_id: int | None) -> WorldHistory:
        """歴代の記録。`league_id` は受賞者を見るリーグ（無効な値は既定のリーグ、無ければ先頭に落とす）。"""
        leagues = self._leagues.find_all()
        options = [LeagueOption(id=league.id, name=league.name) for league in leagues if league.id is not None]
        selected = _selected_league(options, league_id, world.default_league_id)
        finals = self._finals(world, teams=self._team_records.find_all(), leagues=leagues)

        titles = {final.year: self._titles(selected.id, final.year) for final in finals} if selected else {}
        template = next((t for t in titles.values() if t.departments), None)
        batting = template.batting_departments if template else []
        pitching = template.pitching_departments if template else []

        years = []
        for final in finals:
            year_titles = titles.get(final.year)
            by_key = {d.key: d for d in year_titles.departments} if year_titles else {}
            years.append(
                WorldHistoryYear(
                    year=final.year,
                    champions=[self._champion_cell(final, option.id, world.managed_team_id) for option in options],
                    batting=[_holders(by_key, d.key) for d in batting],
                    pitching=[_holders(by_key, d.key) for d in pitching],
                )
            )
        return WorldHistory(
            leagues=options,
            selected_league_id=selected.id if selected else None,
            selected_league_name=selected.name if selected else "",
            batting_columns=[TitleColumn(key=d.key, label=d.label, note=d.note) for d in batting],
            pitching_columns=[TitleColumn(key=d.key, label=d.label, note=d.note) for d in pitching],
            years=years,
        )

    def get_club_history(self, world: WorldContext, team_id: int) -> ClubHistory:
        """球団史。世界に無い球団は `TeamNotFound`（他の世界の球団も、範囲の外なので同じ）。

        タイトルは球団の**いまの所属リーグ**で引く（ペナントの球団はリーグを移らないので、過去の年も同じリーグ）。
        """
        teams = self._team_records.find_all()
        leagues = self._leagues.find_all()
        team = next((t for t in teams if t.id == team_id), None)
        if team is None:
            raise TeamNotFound(f"チームが見つかりません（id={team_id}）。")
        league_name = next((league.name for league in leagues if league.id == team.league_id), "")

        seasons: list[ClubHistorySeason] = []
        wins = losses = ties = 0
        for final in self._finals(world, teams=teams, leagues=leagues):
            league = final.league_of(team_id)
            result = final.result_of(team_id)
            if league is None or result is None:
                continue
            row, is_champion = result
            seasons.append(
                ClubHistorySeason(
                    year=final.year,
                    rank=row.rank,
                    team_count=len(league.rows),
                    wins=row.wins,
                    losses=row.losses,
                    ties=row.ties,
                    winning_percentage=row.winning_percentage,
                    games_behind=row.games_behind,
                    is_champion=is_champion,
                    titles=self._club_titles(team.league_id, final.year, team_id),
                )
            )
            wins += row.wins
            losses += row.losses
            ties += row.ties
        return ClubHistory(
            team_id=team_id,
            team_name=team.name,
            league_name=league_name,
            seasons=seasons,
            record=record_label(wins, losses, ties),
            titles=sum(1 for season in seasons if season.is_champion),
        )

    def _finals(self, world: WorldContext, *, teams: list[Team], leagues: list[League]) -> list[SeasonFinal]:
        """終わったシーズンの最終順位と優勝（古い順）。"""
        years = completed_years(
            self._games.list_seasons(recorded_only=True), phase=world.phase, current_year=world.season_year
        )
        return final_standings(
            years,
            teams=teams,
            leagues=leagues,
            games_of=lambda year: self._games.list_for_standings(year=year),
            order=team_order(self._teams),
        )

    def _titles(self, league_id: int, year: int) -> LeagueTitles:
        return self._teams.get_league_titles(league_id, year)

    def _club_titles(self, league_id: int | None, year: int, team_id: int) -> list[ClubTitle]:
        """その年に、球団の選手が取ったタイトル（部門の1位。同率は全員）。"""
        if league_id is None:
            return []
        return [
            ClubTitle(
                label=department.label,
                player_id=entry.player_id,
                player_name=entry.player_name,
                team_id=entry.team_id,
                value=entry.value,
            )
            for department in self._titles(league_id, year).departments
            for entry in department.entries or []
            if entry.rank == 1 and entry.team_id == team_id
        ]

    @staticmethod
    def _champion_cell(final: SeasonFinal, league_id: int, own_team_id: int | None) -> LeagueChampionCell:
        champion = final.champions.get(league_id)
        league = next((league for league in final.standings if league.league_id == league_id), None)
        row = next((row for row in league.rows if row.team_id == champion), None) if league else None
        if champion is None or row is None:
            return LeagueChampionCell(league_id=league_id, team_id=None, team_name="", record="", is_own=False)
        return LeagueChampionCell(
            league_id=league_id,
            team_id=champion,
            team_name=row.team_name,
            record=record_label(row.wins, row.losses, row.ties),
            is_own=champion == own_team_id,
        )


def _selected_league(options: list[LeagueOption], requested: int | None, default: int | None) -> LeagueOption | None:
    """選ばれたリーグ。無効な指定は既定のリーグ、それも無ければ先頭に落とす（エラーにしない）。"""
    by_id = {option.id: option for option in options}
    for candidate in (requested, default):
        if candidate is not None and candidate in by_id:
            return by_id[candidate]
    return options[0] if options else None


def _holders(by_key: dict[str, TitleDepartment], key: str) -> TitleHolderCell:
    department = by_key.get(key)
    entries = department.entries if department is not None else None
    return TitleHolderCell(department_key=key, holders=[entry for entry in entries or [] if entry.rank == 1])
