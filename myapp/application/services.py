"""アプリケーションサービス（ユースケース）。

「集約を読む → ドメインに操作させる → 保存する」という手順のみを担う。
背番号の重複判定や指標の計算といった業務ルールはドメイン層にあり、ここには無い。
"""

from __future__ import annotations

from ..domain import services as domain_services
from ..domain.entities import Game, Player, Team
from ..domain.value_objects import (
    BattingLine,
    InningsPitched,
    JerseyNumber,
    PitchingLine,
    Position,
    Season,
    TeamRecord,
    format_average,
)
from .dto import (
    AdminOverview,
    BatterRow,
    Dashboard,
    Listing,
    PitcherRow,
    PlayerDetail,
    RankingEntry,
    Standings,
    StandingRow,
    TeamSummary,
)


class TeamApplicationService:
    """チームとロスターに関するユースケース。"""

    def __init__(self, teams, team_list_query=None, games=None):
        # 具象クラスではなくリポジトリのインターフェースに依存する
        self._teams = teams
        # 一覧表示は集約を組み立てないリードモデルを使う
        self._team_list_query = team_list_query
        # 勝敗と通算成績の出典
        self._games = games

    # --- 参照系 ---

    # チーム一覧の並べ替え。業務ルールではなく表示上の都合なのでここに置く。
    # 既定は管理画面で設定した手動の表示順（DTO の並びそのまま）。
    TEAM_SORT_KEYS = {
        'name': (lambda t: t.name, False),
        'league': (lambda t: (t.league_name, t.name), False),
        'city': (lambda t: (t.city or '￿', t.name), False),
        'players': (lambda t: t.player_count, True),
    }

    def list_teams(self, *, sort: str = None, descending: bool = None) -> Listing:
        rows = self._team_list_query.list_summaries()

        if sort not in self.TEAM_SORT_KEYS:
            # 既定は手動の表示順。並べ替えずにそのまま返す
            return Listing(rows=rows, sort='order', descending=False)

        getter, default_desc = self.TEAM_SORT_KEYS[sort]
        desc = default_desc if descending is None else bool(descending)
        return Listing(
            rows=sorted(rows, key=getter, reverse=desc), sort=sort, descending=desc
        )

    def get_dashboard(self, *, leaders: int = 5) -> Dashboard:
        """ホーム画面の概況とランキングを組み立てる。

        順位づけの規則そのものはドメインサービスに委ね、ここでは
        集約をまたいで選手を集め、DTO に詰め替えるだけにとどめる。
        """
        teams = self._teams.find_all_with_roster()
        team_name_by_id = {team.id: team.name for team in teams}

        players: list[tuple[Player, int]] = [
            (player, team.id) for team in teams for player in team.active_players
        ]
        team_of = {id(player): team_id for player, team_id in players}
        all_players = [player for player, _ in players]

        def to_entries(ranked, formatter) -> list[RankingEntry]:
            return [
                RankingEntry(
                    rank=item.rank,
                    player_id=item.player.id,
                    player_name=item.player.name,
                    team_id=team_of[id(item.player)],
                    team_name=team_name_by_id[team_of[id(item.player)]],
                    value=formatter(item.value),
                )
                for item in ranked
            ]

        rate3 = lambda v: f"{v:.3f}"
        rate2 = lambda v: f"{v:.2f}"
        count = lambda v: str(int(v))

        return Dashboard(
            league_count=len({team.league_id for team in teams}),
            team_count=len(teams),
            batter_count=sum(1 for p in all_players if not p.is_pitcher),
            pitcher_count=sum(1 for p in all_players if p.is_pitcher),
            ops_leaders=to_entries(
                domain_services.leaders_by_ops(all_players, limit=leaders), rate3
            ),
            home_run_leaders=to_entries(
                domain_services.leaders_by_home_runs(all_players, limit=leaders), count
            ),
            era_leaders=to_entries(
                domain_services.leaders_by_era(all_players, limit=leaders), rate2
            ),
            strikeout_leaders=to_entries(
                domain_services.leaders_by_strikeouts(all_players, limit=leaders), count
            ),
            teams=self._team_list_query.list_summaries(),
        )

    def get_team_name(self, team_id: int) -> str:
        return self._teams.find_by_id(team_id).name

    def list_batters(
        self, team_id: int, *, sort: str = None, descending: bool = None
    ) -> Listing:
        team = self._teams.find_by_id(team_id)
        batters = [p for p in team.active_players if not p.is_pitcher]
        players, key, desc = domain_services.sort_batters(batters, sort, descending)
        return Listing(
            rows=[self._to_batter_row(p) for p in players], sort=key, descending=desc
        )

    def list_pitchers(
        self, team_id: int, *, sort: str = None, descending: bool = None
    ) -> Listing:
        team = self._teams.find_by_id(team_id)
        pitchers = [p for p in team.active_players if p.is_pitcher]
        players, key, desc = domain_services.sort_pitchers(pitchers, sort, descending)
        return Listing(
            rows=[self._to_pitcher_row(p) for p in players], sort=key, descending=desc
        )

    def get_player_detail(self, team_id: int, player_id: int) -> PlayerDetail:
        team = self._teams.find_by_id(team_id)
        return self._to_detail(team, team.find_player(player_id))

    # 順位表の並べ替え。順位そのものは勝率から決まるので、ここでの並べ替えは
    # 表示順を変えるだけで rank の値は変えない。
    STANDING_SORT_KEYS = {
        'rank': (lambda r: r.rank, False),
        'team': (lambda r: r.team_name, False),
        'games': (lambda r: r.games_played, True),
        'wins': (lambda r: r.wins, True),
        'losses': (lambda r: r.losses, True),
        'ties': (lambda r: r.ties, True),
        'pct': (lambda r: r.rank, False),  # 勝率順＝順位順
    }

    def get_standings(
        self, year: int | None = None, *, sort: str = None, descending: bool = None
    ) -> Standings:
        """指定シーズンの順位表を返す。年を省略した場合は最新シーズン。

        順位づけの規則そのものはドメインサービスにあり、ここでは
        表示用に整形するだけにとどめる。
        """
        teams = self._teams.find_all_with_roster()
        all_games = self._games.find_all()
        seasons = domain_services.seasons_of(all_games)

        if not seasons:
            return Standings(year=year or 0, rows=[], available_years=[])

        target = Season(year) if year is not None else seasons[0]
        rows = domain_services.standings(
            teams, [g for g in all_games if g.season == target]
        )

        display_rows = [
            StandingRow(
                rank=row.rank,
                team_id=row.team_id,
                team_name=row.team_name,
                wins=row.record.wins,
                losses=row.record.losses,
                ties=row.record.ties,
                games_played=row.record.games_played,
                winning_percentage=format_average(row.record.winning_percentage),
                games_behind='—' if row.is_leader else f'{row.games_behind:.1f}',
            )
            for row in rows
        ]

        # 並べ替えは表示順を変えるだけで、rank の値は動かさない
        if sort in self.STANDING_SORT_KEYS:
            getter, default_desc = self.STANDING_SORT_KEYS[sort]
            desc = default_desc if descending is None else bool(descending)
            display_rows = sorted(display_rows, key=getter, reverse=desc)
        else:
            sort, desc = 'rank', False

        return Standings(
            year=target.year,
            rows=display_rows,
            available_years=[s.year for s in seasons],
            sort=sort,
            descending=desc,
        )

    def record_game(
        self,
        *,
        year: int,
        played_on,
        home_team_id: int,
        away_team_id: int,
        home_score: int,
        away_score: int,
    ) -> Game:
        """試合を登録する。勝敗はここから集計されるので、別途入力しない。"""
        game = Game(
            season=Season(year),
            played_on=played_on,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
            home_score=home_score,
            away_score=away_score,
        )
        return self._games.save(game)

    def get_admin_overview(self) -> AdminOverview:
        """管理画面トップ用の概況。

        「成績が未入力か」の判定はドメインサービスの規定（打数0・投球回0を除く）を
        そのまま使う。ここで独自に条件を書くと、ランキングの対象と管理画面の
        警告がずれてしまう。
        """
        teams = self._teams.find_all_with_roster()
        active = [p for team in teams for p in team.active_players]
        retired = sum(
            1 for team in teams for p in team.players if not p.is_active
        )

        qualified = {
            id(p) for p in domain_services.qualified_batters(active)
        } | {
            id(p) for p in domain_services.qualified_pitchers(active)
        }

        return AdminOverview(
            league_count=len({team.league_id for team in teams}),
            team_count=len(teams),
            player_count=len(active),
            batter_count=sum(1 for p in active if not p.is_pitcher),
            pitcher_count=sum(1 for p in active if p.is_pitcher),
            players_without_stats=sum(1 for p in active if id(p) not in qualified),
            retired_count=retired,
            teams_without_players=sum(1 for team in teams if not team.active_players),
        )

    # --- 更新系 ---

    def register_player(
        self, team_id: int, name: str, number: int, position_label: str
    ) -> Player:
        """新しい選手をロスターに加える。"""
        team = self._teams.find_by_id(team_id)
        player = team.add_player(
            name=name,
            number=JerseyNumber(number),
            position=Position.from_label(position_label),
        )
        self._teams.save(team)
        return player

    def update_player(
        self, team_id: int, player_id: int, *, name: str, number: int, position_label: str
    ) -> Player:
        """選手の基本情報を更新する。

        成績は試合から集計するため、ここでは扱わない。
        """
        team = self._teams.find_by_id(team_id)
        player = team.find_player(player_id)

        player.rename(name)
        team.change_player_number(player, JerseyNumber(number))
        player.change_position(Position.from_label(position_label))

        self._teams.save(team)
        return player

    def retire_player(self, team_id: int, player_id: int) -> Player:
        """選手を退団させる。成績は残り、背番号は再利用できるようになる。"""
        team = self._teams.find_by_id(team_id)
        player = team.find_player(player_id)
        player.retire()
        self._teams.save(team)
        return player

    # --- DTO への詰め替え ---

    @staticmethod
    def _to_batter_row(player: Player) -> BatterRow:
        line = player.batting
        return BatterRow(
            id=player.id,
            name=player.name,
            number=player.number.value,
            position=player.position.label,
            at_bats=line.at_bats,
            hits=line.hits,
            doubles=line.doubles,
            triples=line.triples,
            home_runs=line.home_runs,
            runs_batted_in=line.runs_batted_in,
            batting_average=line.batting_average,
            on_base_percentage=line.on_base_percentage,
            ops=line.ops,
        )

    @staticmethod
    def _to_pitcher_row(player: Player) -> PitcherRow:
        line = player.pitching
        return PitcherRow(
            id=player.id,
            name=player.name,
            number=player.number.value,
            position=player.position.label,
            innings_pitched=str(line.innings),
            wins=line.wins,
            losses=line.losses,
            strikeouts=line.strikeouts,
            earned_run_average=line.earned_run_average,
            whip=line.whip,
            strikeouts_per_nine=line.strikeouts_per_nine,
        )

    @staticmethod
    def _to_detail(team: Team, player: Player) -> PlayerDetail:
        batting, pitching = player.batting, player.pitching
        return PlayerDetail(
            id=player.id,
            team_id=team.id,
            name=player.name,
            number=player.number.value,
            position=player.position.label,
            is_pitcher=player.is_pitcher,
            at_bats=batting.at_bats,
            singles=batting.singles,
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
            batting_average=batting.batting_average,
            on_base_percentage=batting.on_base_percentage,
            slugging_percentage=batting.slugging_percentage,
            ops=batting.ops,
            earned_run_average=pitching.earned_run_average,
            whip=pitching.whip,
            strikeouts_per_nine=pitching.strikeouts_per_nine,
        )
