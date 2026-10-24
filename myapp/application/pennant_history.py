"""ペナントの歴代の記録・球団史・選手の獲得タイトルの材料を作る。終わったシーズンだけを読み、何も書かない。

- 年ごとの最終順位と優勝: `final_standings`（世界の終了と同じ関数。優勝の決め方は domain の `league_champion`）
- タイトルの受賞者（#248）: **締めた年は表彰**（`AwardQuery`。締める処理が保存した受賞者）を1本で読み、
  **終わったが締めていない年**（domain の `unclosed_completed_year`。シーズン終了のいまの年度）だけを、締める処理と
  同じ関数（`pennant_awards.derive_award_rows`。タイトルの画面と同じ `get_league_titles`）で導く。
  同じ年を両方から読まない

載せるのは終わったシーズン（domain の `completed_years`）だけ。いまのシーズンは優勝もタイトルも確定していない。
受賞者は成績だけで決まる部門（domain の `Title`）の1位で、同率は全員（MVP などの選考で決まる賞は持たない）。
"""

from __future__ import annotations

from collections.abc import Collection, Iterable

from ..domain.entities import League, Team
from ..domain.exceptions import TeamNotFound
from ..domain.pennant.season import completed_years, unclosed_completed_year
from ..domain.repositories import LeagueRepository, TeamRepository
from ..domain.services.rankings import Title
from .dto import (
    AwardRow,
    ClubHistory,
    ClubHistorySeason,
    ClubTitle,
    LeagueChampionCell,
    LeagueOption,
    TitleColumn,
    TitleHolderCell,
    WorldContext,
    WorldHistory,
    WorldHistoryYear,
)
from .league_view import LeagueViewService
from .pennant_awards import derive_award_rows
from .pennant_race import SeasonFinal, final_standings, team_order
from .queries import AwardQuery, GameListQuery
from .standings import record_label


class PennantHistoryService:
    """歴代の記録・球団史・選手の獲得タイトル。`league_view` は世界の範囲で組み立てたサービス。"""

    def __init__(
        self,
        *,
        league_view: LeagueViewService,
        games: GameListQuery,
        team_records: TeamRepository,
        leagues: LeagueRepository,
        awards: AwardQuery,
    ) -> None:
        self._league_view = league_view
        self._games = games
        # 年ごとの順位を作るために読む（順位の規則は domain の `standings`）
        self._team_records = team_records
        self._leagues = leagues
        self._awards = awards

    def get_world_history(self, world: WorldContext, *, league_id: int | None) -> WorldHistory:
        """歴代の記録。`league_id` は受賞者を見るリーグ（無効な値は既定のリーグ、無ければ先頭に落とす）。"""
        leagues = self._leagues.find_all()
        options = [LeagueOption(id=league.id, name=league.name) for league in leagues if league.id is not None]
        selected = _selected_league(options, league_id, world.default_league_id)
        finals = self._finals(world, teams=self._team_records.find_all(), leagues=leagues)

        holders = self._holders(world, [final.year for final in finals], selected.id) if selected else {}
        # 列は部門の定義（`Title`）の並び。載せる年が無ければ表を出さないので、列も作らない
        shown = list(Title) if selected and finals else []
        batting = [title for title in shown if not title.is_pitching]
        pitching = [title for title in shown if title.is_pitching]

        years = []
        for final in finals:
            year_holders = holders.get(final.year, [])
            years.append(
                WorldHistoryYear(
                    year=final.year,
                    champions=[self._champion_cell(final, option.id, world.managed_team_id) for option in options],
                    batting=[_cell(year_holders, title) for title in batting],
                    pitching=[_cell(year_holders, title) for title in pitching],
                )
            )
        return WorldHistory(
            leagues=options,
            selected_league_id=selected.id if selected else None,
            selected_league_name=selected.name if selected else "",
            batting_columns=[_column(title) for title in batting],
            pitching_columns=[_column(title) for title in pitching],
            years=years,
        )

    def get_club_history(self, world: WorldContext, team_id: int) -> ClubHistory:
        """球団史。世界に無い球団は `TeamNotFound`（他の世界の球団も、範囲の外なので同じ）。

        タイトルは、その年にその球団に所属して取ったもの（締めた年は表彰の球団、締めていない年は
        球団の**いまの所属リーグ**で導く。ペナントの球団はリーグを移らない）。
        """
        teams = self._team_records.find_all()
        leagues = self._leagues.find_all()
        team = next((t for t in teams if t.id == team_id), None)
        if team is None:
            raise TeamNotFound(f"チームが見つかりません（id={team_id}）。")
        league_name = next((league.name for league in leagues if league.id == team.league_id), "")

        finals = self._finals(world, teams=teams, leagues=leagues)
        holders = (
            self._holders(world, [final.year for final in finals], team.league_id, team_id=team_id)
            if team.league_id is not None
            else {}
        )
        seasons: list[ClubHistorySeason] = []
        wins = losses = ties = 0
        for final in finals:
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
                    titles=[_club_title(award) for award in holders.get(final.year, [])],
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

    def get_player_awards(
        self, world: WorldContext, team_id: int, player_id: int, *, played_years: Collection[int]
    ) -> list[AwardRow]:
        """選手ページの「獲得タイトル」（古い順）。締めた年は表彰、終わったが締めていない年は導く。

        締めていない年は、選手ページを開いた球団（`team_id`）のリーグで導く（世界では選手が球団を移らない）。
        `played_years` は選手が出場した年（選手ページが既に読んだ年度別成績の年）。締めていない年に出場していなければ、
        受賞はありえないので導かない（リーグ1つぶんのタイトルの集計を省く）。出場した年はシーズンの年と同じ
        （ペナントの日程は暦年の中で完結する）ことが前提。
        """
        recorded = self._awards.list_awards(player_id=player_id)
        open_year = unclosed_completed_year(phase=world.phase, current_year=world.season_year)
        if open_year is None or open_year not in played_years:
            return recorded
        league_id = next((team.league_id for team in self._league_view.list_teams().rows if team.id == team_id), None)
        derived = (
            [row for row in derive_award_rows(self._league_view, open_year, league_id) if row.player_id == player_id]
            if league_id is not None
            else []
        )
        return [*(row for row in recorded if row.year != open_year), *derived]

    def _holders(
        self, world: WorldContext, years: Iterable[int], league_id: int, *, team_id: int | None = None
    ) -> dict[int, list[AwardRow]]:
        """終わった年ごとの受賞者（`team_id` を渡すと、その球団の選手の受賞だけ）。

        締めた年は表彰を1本で読み、終わったが締めていない年だけを導く（年ごとにタイトルを集計しない）。
        """
        shown = set(years)
        open_year = unclosed_completed_year(phase=world.phase, current_year=world.season_year)
        rows = [
            row
            for row in self._awards.list_awards(league_id=league_id, team_id=team_id)
            if row.year in shown and row.year != open_year
        ]
        if open_year in shown:
            assert open_year is not None
            rows.extend(
                row
                for row in derive_award_rows(self._league_view, open_year, league_id)
                if team_id is None or row.team_id == team_id
            )
        by_year: dict[int, list[AwardRow]] = {}
        for row in rows:
            by_year.setdefault(row.year, []).append(row)
        return by_year

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
            order=team_order(self._league_view),
        )

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


def _column(title: Title) -> TitleColumn:
    return TitleColumn(key=title.value, label=title.label, note=title.note)


def _cell(holders: list[AwardRow], title: Title) -> TitleHolderCell:
    """その年のある部門の受賞者（同率は全員。並びは名前順）。"""
    return TitleHolderCell(department_key=title.value, holders=[row.entry() for row in holders if row.title is title])


def _club_title(award: AwardRow) -> ClubTitle:
    return ClubTitle(
        label=award.label,
        player_id=award.player_id,
        player_name=award.player_name,
        team_id=award.team_id,
        value=award.value,
    )
