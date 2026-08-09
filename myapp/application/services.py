"""アプリケーションサービス（ユースケース）。

「集約を読む → ドメインに操作させる → 保存する」という手順のみを担う。
背番号の重複判定や指標の計算といった業務ルールはドメイン層にあり、ここには無い。
"""

from __future__ import annotations

from ..domain import services as domain_services
from ..domain.entities import Player, Team
from ..domain.value_objects import (
    BattingLine,
    InningsPitched,
    JerseyNumber,
    PitchingLine,
    Position,
)
from .dto import BatterRow, Dashboard, PitcherRow, PlayerDetail, RankingEntry, TeamSummary


class TeamApplicationService:
    """チームとロスターに関するユースケース。"""

    def __init__(self, teams, team_list_query=None):
        # 具象クラスではなくリポジトリのインターフェースに依存する
        self._teams = teams
        # 一覧表示は集約を組み立てないリードモデルを使う
        self._team_list_query = team_list_query

    # --- 参照系 ---

    def list_teams(self) -> list[TeamSummary]:
        return self._team_list_query.list_summaries()

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

    def list_batters(self, team_id: int) -> list[BatterRow]:
        team = self._teams.find_by_id(team_id)
        return [self._to_batter_row(p) for p in team.batters_by_ops()]

    def list_pitchers(self, team_id: int) -> list[PitcherRow]:
        team = self._teams.find_by_id(team_id)
        return [self._to_pitcher_row(p) for p in team.pitchers_by_era()]

    def get_player_detail(self, team_id: int, player_id: int) -> PlayerDetail:
        team = self._teams.find_by_id(team_id)
        return self._to_detail(team, team.find_player(player_id))

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
        self,
        team_id: int,
        player_id: int,
        *,
        name: str,
        number: int,
        position_label: str,
        batting: BattingLine | None = None,
        pitching: PitchingLine | None = None,
    ) -> Player:
        """選手の基本情報と成績を更新する。"""
        team = self._teams.find_by_id(team_id)
        player = team.find_player(player_id)

        player.rename(name)
        team.change_player_number(player, JerseyNumber(number))
        player.change_position(Position.from_label(position_label))

        if batting is not None:
            player.record_batting(batting)
        if pitching is not None:
            player.record_pitching(pitching)

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
