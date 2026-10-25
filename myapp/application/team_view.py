"""球団と選手の画面の参照（選手の一覧・チームの合計と月別・選手の個人ページ）。

選手の一覧とチームの合計は集約を組み立てず、参照クエリ（`PlayerStatsQuery`）から直接読む。
選手の個人ページだけは、経歴・プロフィール・FA 宣言を出すために `Team` 集約を読む。
どれも読むだけで、ロスターの更新は `RosterService` が受け持つ。

選手の一覧（`list_batters`）とチームの合計（`get_team_totals`）は、同じ画面で同じインスタンスから呼ばれ、
リーグの基準値の覚え（`LeagueContexts`）を共有する。別のサービスに分けると、リーグの成績の読み込みが1回増える。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

from ..domain import services as domain_services
from ..domain.entities import Player, Team
from ..domain.exceptions import TeamNotFound
from ..domain.repositories import GameRepository, TeamRepository
from ..domain.value_objects import ContractStatus, FreeAgencyOutcome, format_average
from .dto import (
    ActivePlayerStats,
    CareerRow,
    FreeAgentDeclarationRow,
    Listing,
    MonthlyRow,
    PlayerDetail,
    PlayerGameRow,
    PlayerProfile,
    TeamMonthlyRow,
    TeamTotals,
    YearlyRow,
)
from .ids import saved_id, team_names
from .league_context import EMPTY_LEAGUE_CONTEXT, LeagueContext, LeagueContexts
from .player_stats_view import played_counts, stats_player
from .queries import GameListQuery, PlayerFieldingQuery, PlayerStatsQuery
from .stat_rows import compact_record_label, decision_label, to_batter_row, to_pitcher_row


def _is_developmental(team: Team, player: Player) -> bool:
    """このチームで今、育成契約の選手か。画面の「育成」バッジの出典。"""
    return team.contract_of(player) is ContractStatus.DEVELOPMENTAL


class TeamViewService:
    """球団と選手の画面を読むユースケース。"""

    def __init__(
        self,
        *,
        teams: TeamRepository,
        games: GameRepository,
        game_list_query: GameListQuery,
        player_fielding_query: PlayerFieldingQuery,
        player_stats_query: PlayerStatsQuery,
        today: Callable[[], date],
    ) -> None:
        # 省略可能にすると、一部だけ渡した半端なサービスが作れてしまい、呼ぶ経路によって
        # 「None に find_all は無い」で落ちる。そのため全部を必須にしている。
        # 球団の名前と、個人ページの集約（経歴・プロフィール・FA 宣言）を読む
        self._teams = teams
        # チームの月別成績の材料（その球団の試合）
        self._games = games
        # 選手の試合ごとの成績と、規定の基準になる球団の試合数
        self._game_list_query = game_list_query
        # 守備成績は選手ページで通算・年度別を合計で出すだけなので、集約を組み立てずに読む
        self._player_fielding_query = player_fielding_query
        # 選手の一覧とチームの合計は、集約を組み立てずに参照クエリから読む
        self._player_stats_query = player_stats_query
        # 年齢の基準日。実データは暦の今日、ペナントの世界はその世界の「今日」（組み立ての1か所が渡す）
        self._today = today
        # リーグの基準値（FIP 定数・OPS+/ERA+ の平均）は何度も引くため覚えておく
        self._league_contexts = LeagueContexts(player_stats_query)

    def get_team_name(self, team_id: int) -> str:
        """球団の名前。画面が球団の存在を確かめるのにも使うので、ロスターと成績を読まない（一覧用の読み方で探す）。"""
        for team in self._teams.find_all():
            if team.id == team_id:
                return team.name
        raise TeamNotFound(f"チームが見つかりません（id={team_id}）。")

    def _roster(
        self, team_id: int, year: int | None
    ) -> tuple[list[ActivePlayerStats], list[Player], set[int], set[int]]:
        """球団の選手の一覧の材料。参照クエリから直接読み、集約（`Team`）を組み立てない。

        `year` が None なら在籍中の選手の通算成績、年を渡せばその年に在籍した選手のその年の成績。
        戻り値は (読んだ行, 一覧に渡す選手, 主将の選手 id, 育成の選手 id)。
        """
        rows = self._player_stats_query.list_roster(year=year, team_id=team_id, with_profile=True, with_games=True)
        return (
            rows,
            [stats_player(row) for row in rows],
            {row.player_id for row in rows if row.is_captain},
            {row.player_id for row in rows if row.is_developmental},
        )

    def list_batters(
        self, team_id: int, *, sort: str | None = None, descending: bool | None = None, year: int | None = None
    ) -> Listing:
        """球団の野手の成績の一覧。`year` を渡すとその年の成績（ペナントの「今季」）、省くと通算。"""
        rows, players, captain_ids, developmental_ids = self._roster(team_id, year)
        batters = [p for p in players if not p.is_pitcher]
        games = played_counts(rows, pitchers=False)
        ordered, key, desc = domain_services.sort_batters(
            batters, sort, descending, default_key=domain_services.DEFAULT_ROSTER_SORT, games=games
        )
        context = self._league_contexts.get(rows[0].league_id if rows else None, year)
        today = self._today()
        return Listing(
            rows=[
                to_batter_row(
                    p,
                    is_captain=p.id in captain_ids,
                    is_developmental=p.id in developmental_ids,
                    played=games.get(p.id or 0, 0),
                    league_context=context,
                    today=today,
                )
                for p in ordered
            ],
            sort=key,
            descending=desc,
        )

    def list_pitchers(
        self, team_id: int, *, sort: str | None = None, descending: bool | None = None, year: int | None = None
    ) -> Listing:
        """球団の投手の成績の一覧。`year` の意味は `list_batters` と同じ。"""
        rows, players, captain_ids, developmental_ids = self._roster(team_id, year)
        pitchers = [p for p in players if p.is_pitcher]
        appearances = played_counts(rows, pitchers=True)
        ordered, key, desc = domain_services.sort_pitchers(
            pitchers, sort, descending, default_key=domain_services.DEFAULT_ROSTER_SORT, appearances=appearances
        )
        context = self._league_contexts.get(rows[0].league_id if rows else None, year)
        today = self._today()
        return Listing(
            rows=[
                to_pitcher_row(
                    p,
                    is_captain=p.id in captain_ids,
                    is_developmental=p.id in developmental_ids,
                    played=appearances.get(p.id or 0, 0),
                    league_context=context,
                    today=today,
                )
                for p in ordered
            ],
            sort=key,
            descending=desc,
        )

    def get_team_totals(self, team_id: int, year: int | None = None) -> TeamTotals:
        """チームの打撃・投球の合計と、そこから求めた指標。`year` を渡すとその年の成績と試合数。"""
        rows = self._player_stats_query.list_roster(year=year, team_id=team_id)
        active = [stats_player(row) for row in rows]
        batting = domain_services.team_batting(active)
        pitching = domain_services.team_pitching(active)
        # 規定打席・規定投球回の基準になる試合数。数えるだけなので集約を組み立てず、参照クエリに SQL で数えさせる
        games = self._game_list_query.count_by_team(year=year).get(team_id, 0)

        return TeamTotals(
            games=games,
            batting_average=batting.batting_average,
            on_base_percentage=batting.on_base_percentage,
            slugging_percentage=batting.slugging_percentage,
            ops=batting.ops,
            home_runs=batting.home_runs,
            runs_batted_in=batting.runs_batted_in,
            earned_run_average=pitching.earned_run_average,
            whip=pitching.whip,
            strikeouts=pitching.strikeouts,
            innings_pitched=str(pitching.innings),
            required_plate_appearances=domain_services.required_plate_appearances(games),
            required_innings=f"{domain_services.required_outs(games) / 3:.1f}",
            fip=pitching.fip(self._league_contexts.get(rows[0].league_id if rows else None, year).fip_constant),
        )

    def list_team_monthly_splits(self, team_id: int, year: int | None = None) -> list[TeamMonthlyRow]:
        """チームの月別成績。個人の月別成績と対になる、チーム単位の推移。

        `year` を渡すと、その年の試合だけを読む（全シーズンの試合の明細を組み立てない）。
        """
        if year is None:
            member_ids = {saved_id(p.id) for p in self._teams.find_by_id(team_id).players}
        else:
            member_ids = {row.player_id for row in self._player_stats_query.list_roster(year=year, team_id=team_id)}

        return [
            TeamMonthlyRow(
                label=split.label,
                games_played=split.games_played,
                record_label=compact_record_label(split.record),
                winning_percentage=format_average(split.record.winning_percentage),
                batting_average=split.batting.batting_average,
                ops=split.batting.ops,
                home_runs=split.batting.home_runs,
                earned_run_average=split.pitching.earned_run_average,
                whip=split.pitching.whip,
                strikeouts=split.pitching.strikeouts,
            )
            for split in domain_services.team_monthly_splits(
                self._games.find_by_team(team_id, year=year), team_id, member_ids
            )
        ]

    def get_player_detail(self, team_id: int, player_id: int) -> PlayerDetail:
        team = self._teams.find_by_id(team_id)
        return self._to_detail(team, team.find_player(player_id), self._league_contexts.get(team.league_id))

    def get_player_profile(self, team_id: int, player_id: int, *, month: str | None = None) -> PlayerProfile:
        """選手個人ページ。キャリア通算・年度別・月別と、選んだ月の試合ごとの成績。

        試合ごとの成績は**月で絞って**返す。1シーズンで140試合を超えるため、
        全期間を1つの表に並べると読む場所ではなくなる。month の指定が無い・
        出場していない月を指定された場合は最新の月に落とす（並べ替えのキーと
        同じ扱いで、エラーにはしない）。
        """
        # 球団の集約を読むのは1度だけ（詳細の通算と、経歴・プロフィールの両方をここから作る）
        team = self._teams.find_by_id(team_id)
        player = team.find_player(player_id)
        detail = self._to_detail(team, player, self._league_contexts.get(team.league_id))
        names = team_names(self._teams)
        team_games = self._game_list_query.list_for_player(team_id, player_id)

        # 月ごとに束ねてから選んだ月だけを取り出す。行の側で日付を見て絞ると、
        # 表示用の DTO（played_on の型を問わない）に日付の解釈を持ち込むことになる
        by_month: dict[str, list[PlayerGameRow]] = {}
        # 古い試合から順に詰める（表示も試合日の昇順。月の頭から追えるように。並べ替えを DTO 側でやり直さない）
        for game in sorted(team_games, key=lambda g: (g.played_on, saved_id(g.id))):
            batting = next((e for e in game.batting if e.player_id == player_id), None)
            pitching = next((e for e in game.pitching if e.player_id == player_id), None)
            if batting is None and pitching is None:
                continue

            opponent_id = game.away_team_id if game.home_team_id == team_id else game.home_team_id
            by_month.setdefault(f"{game.played_on.year}-{game.played_on.month:02d}", []).append(
                PlayerGameRow(
                    game_id=saved_id(game.id),
                    played_on=game.played_on,
                    opponent_name=names.get(opponent_id, ""),
                    at_bats=batting.line.at_bats if batting else 0,
                    hits=batting.line.hits if batting else 0,
                    home_runs=batting.line.home_runs if batting else 0,
                    runs_batted_in=batting.line.runs_batted_in if batting else 0,
                    runs=batting.line.runs if batting else 0,
                    stolen_bases=batting.line.stolen_bases if batting else 0,
                    caught_stealing=batting.line.caught_stealing if batting else 0,
                    sacrifice_bunts=batting.line.sacrifice_bunts if batting else 0,
                    intentional_walks=batting.line.intentional_walks if batting else 0,
                    strikeouts_batting=batting.line.strikeouts if batting else 0,
                    double_plays=batting.line.double_plays if batting else 0,
                    innings_pitched=str(pitching.line.innings) if pitching else "0.0",
                    runs_allowed=pitching.line.runs_allowed if pitching else 0,
                    earned_runs=pitching.line.earned_runs if pitching else 0,
                    strikeouts=pitching.line.strikeouts if pitching else 0,
                    decision=decision_label(pitching.line) if pitching else "",
                )
            )

        months = [self._to_monthly_row(split) for split in domain_services.monthly_splits(team_games, player_id)]
        selected = next((row for row in months if row.key == month), months[-1] if months else None)
        rows = by_month.get(selected.key, []) if selected else []

        profile = player.profile

        return PlayerProfile(
            detail=detail,
            games=rows,
            appearances=sum(len(month_rows) for month_rows in by_month.values()),
            selected_month=selected.key if selected else "",
            selected_month_label=selected.label if selected else "",
            years=[self._to_yearly_row(split) for split in domain_services.yearly_splits(team_games, player_id)],
            months=months,
            fielding=self._player_fielding_query.for_player(player_id, team_id),
            career=[
                CareerRow(
                    team_id=s.team_id,
                    team_name=s.team_name,
                    number=s.number.value,
                    from_year=s.from_year,
                    to_year=s.to_year,
                    is_current=s.is_current,
                    acquired_via_label=s.acquired_via.label if s.acquired_via is not None else "",
                )
                for s in player.career
            ],
            age=profile.age_or_none(self._today()),
            name_kana=profile.name_kana,
            back_name=profile.back_name,
            throws_bats=profile.throws_bats,
            height_cm=profile.height_cm,
            weight_kg=profile.weight_kg,
            birthplace=profile.birthplace,
            nationality=profile.nationality,
            is_foreign_player=profile.is_foreign_player,
            foreign_background=profile.foreign_background.label if profile.foreign_background else "",
            draft_label=profile.draft.label if profile.draft is not None else "",
            amateur_career=profile.amateur_career,
            has_profile=not profile.is_empty,
        )

    @staticmethod
    def _to_yearly_row(split: domain_services.YearlySplit) -> YearlyRow:
        """年度別成績を表示用に整える。率は年ごとの合計から計算し直された値。"""
        batting, pitching = split.batting, split.pitching
        return YearlyRow(
            label=split.label,
            appearances=split.appearances,
            year=split.year,
            plate_appearances=batting.plate_appearances,
            at_bats=batting.at_bats,
            hits=batting.hits,
            doubles=batting.doubles,
            triples=batting.triples,
            home_runs=batting.home_runs,
            runs_batted_in=batting.runs_batted_in,
            walks=batting.walks,
            hit_by_pitch=batting.hit_by_pitch,
            sacrifice_flies=batting.sacrifice_flies,
            runs=batting.runs,
            stolen_bases=batting.stolen_bases,
            caught_stealing=batting.caught_stealing,
            sacrifice_bunts=batting.sacrifice_bunts,
            intentional_walks=batting.intentional_walks,
            strikeouts_batting=batting.strikeouts,
            double_plays=batting.double_plays,
            batting_average=batting.batting_average,
            on_base_percentage=batting.on_base_percentage,
            slugging_percentage=batting.slugging_percentage,
            ops=batting.ops,
            starts=pitching.starts,
            innings_pitched=str(pitching.innings),
            wins=pitching.wins,
            losses=pitching.losses,
            saves=pitching.saves,
            holds=pitching.holds,
            hold_points=pitching.hold_points,
            hits_allowed=pitching.hits_allowed,
            home_runs_allowed=pitching.home_runs_allowed,
            walks_allowed=pitching.walks_allowed,
            hit_by_pitch_allowed=pitching.hit_by_pitch_allowed,
            strikeouts=pitching.strikeouts,
            runs_allowed=pitching.runs_allowed,
            earned_runs=pitching.earned_runs,
            earned_run_average=pitching.earned_run_average,
            whip=pitching.whip,
            strikeouts_per_nine=pitching.strikeouts_per_nine,
        )

    @staticmethod
    def _to_monthly_row(split: domain_services.MonthlySplit) -> MonthlyRow:
        """月別成績を表示用に整える。率は月ごとの合計から計算し直された値。"""
        batting, pitching = split.batting, split.pitching
        return MonthlyRow(
            label=split.label,
            appearances=split.appearances,
            year=split.year,
            month=split.month,
            at_bats=batting.at_bats,
            hits=batting.hits,
            home_runs=batting.home_runs,
            runs_batted_in=batting.runs_batted_in,
            runs=batting.runs,
            stolen_bases=batting.stolen_bases,
            caught_stealing=batting.caught_stealing,
            sacrifice_bunts=batting.sacrifice_bunts,
            intentional_walks=batting.intentional_walks,
            strikeouts_batting=batting.strikeouts,
            double_plays=batting.double_plays,
            batting_average=batting.batting_average,
            ops=batting.ops,
            innings_pitched=str(pitching.innings),
            runs_allowed=pitching.runs_allowed,
            earned_runs=pitching.earned_runs,
            strikeouts=pitching.strikeouts,
            earned_run_average=pitching.earned_run_average,
            whip=pitching.whip,
        )

    @staticmethod
    def _to_detail(
        team: Team,
        player: Player,
        league_context: LeagueContext = EMPTY_LEAGUE_CONTEXT,
    ) -> PlayerDetail:
        assert player.id is not None and team.id is not None, "個人ページの選手・チームは保存済み"
        batting, pitching = player.batting, player.pitching
        return PlayerDetail(
            id=player.id,
            team_id=team.id,
            name=player.name,
            number=player.number.value,
            position=player.position.label,
            is_pitcher=player.is_pitcher,
            is_captain=team.current_captain is player,
            is_developmental=_is_developmental(team, player),
            at_bats=batting.at_bats,
            singles=batting.singles,
            plate_appearances=batting.plate_appearances,
            hits=batting.hits,
            doubles=batting.doubles,
            triples=batting.triples,
            home_runs=batting.home_runs,
            runs_batted_in=batting.runs_batted_in,
            walks=batting.walks,
            hit_by_pitch=batting.hit_by_pitch,
            sacrifice_flies=batting.sacrifice_flies,
            innings_pitched=str(pitching.innings),
            wins=pitching.wins,
            losses=pitching.losses,
            saves=pitching.saves,
            earned_runs=pitching.earned_runs,
            strikeouts=pitching.strikeouts,
            hits_allowed=pitching.hits_allowed,
            walks_allowed=pitching.walks_allowed,
            home_runs_allowed=pitching.home_runs_allowed,
            hit_by_pitch_allowed=pitching.hit_by_pitch_allowed,
            batting_average=batting.batting_average,
            on_base_percentage=batting.on_base_percentage,
            slugging_percentage=batting.slugging_percentage,
            ops=batting.ops,
            earned_run_average=pitching.earned_run_average,
            whip=pitching.whip,
            strikeouts_per_nine=pitching.strikeouts_per_nine,
            isolated_power=batting.isolated_power,
            walks_per_nine=pitching.walks_per_nine,
            fip=pitching.fip(league_context.fip_constant),
            ops_plus=batting.ops_plus(league_context.average_ops),
            era_plus=pitching.era_plus(league_context.average_era),
            holds=pitching.holds,
            hold_points=pitching.hold_points,
            starts=pitching.starts,
            runs=batting.runs,
            stolen_bases=batting.stolen_bases,
            caught_stealing=batting.caught_stealing,
            sacrifice_bunts=batting.sacrifice_bunts,
            intentional_walks=batting.intentional_walks,
            strikeouts_batting=batting.strikeouts,
            double_plays=batting.double_plays,
            runs_allowed=pitching.runs_allowed,
            fa_declarations=[
                FreeAgentDeclarationRow(
                    year=declaration.year,
                    kind_label=declaration.kind.label,
                    outcome_label=outcome.value,
                    is_moved=outcome is FreeAgencyOutcome.MOVED,
                )
                for declaration in sorted(player.fa_declarations, key=lambda d: -d.year)
                for outcome in [player.outcome_of(declaration)]
            ],
        )
