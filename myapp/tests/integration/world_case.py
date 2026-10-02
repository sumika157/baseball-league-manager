"""ペナントの世界を使うテストの共通の土台。

実データ（`BaseCase` のリーグ・チーム2つ）に選手と試合を入れ、それを分岐して世界をひとつ作る。
世界の側の名前は、実データのどの名前とも**重ならない目印**に付け替える（分岐で写した名前は
元と同じなので、そのままでは「混ざったか」を名前で見分けられない）。目印の名前が実データの
画面に出たら、それは混入。
"""

from datetime import date

from myapp.application.dto import LineupSlot
from myapp.application.game_recording import GameRecordingService
from myapp.domain.entities import Game
from myapp.domain.pennant.world import WorldScope
from myapp.domain.value_objects import BattingLine, FieldingPosition, Season
from myapp.infrastructure import orm_models
from myapp.infrastructure.repositories import DjangoGameRepository, DjangoLeagueRepository, DjangoTeamRepository
from myapp.presentation.views import build_pennant_world_service

from ..helpers import build_scorebook, play_game, register_lineup, to_plate_appearances
from .base import BaseCase

PENNANT_LEAGUE = "目印リーグ"
PENNANT_TEAM = "目印ホーム球団"
PENNANT_RIVAL = "目印ビジター球団"
PENNANT_PLAYER_PREFIX = "目印選手"
PENNANT_WORLD = "目印の世界"
YEAR = 2026


class WorldCase(BaseCase):
    """実データ（リーグ1・チーム2）と、それを分岐した世界ひとつ。"""

    def setUp(self):
        super().setUp()
        # --- 実データ。各チームに打者9人と投手1人、記録済みの試合を1つ ---
        self.real_batters = register_lineup(self.service, self.team, prefix="実打者", first_number=1)
        self.real_rivals = register_lineup(self.service, self.rival, prefix="実相手", first_number=1)
        self.real_pitcher = self.service.register_player(self.team.id, "実投手", 18, "投手")
        self.real_rival_pitcher = self.service.register_player(self.rival.id, "実相手投手", 19, "投手")
        self.real_game = play_game(
            self.team,
            self.rival,
            home_score=3,
            away_score=1,
            year=YEAR,
            batting={self.real_batters[0]: BattingLine(at_bats=4, singles=2)},
        )

        # --- 世界。分岐してから、世界の側の名前を目印に付け替える ---
        created = build_pennant_world_service().create_world(
            name=PENNANT_WORLD,
            owner_id=None,
            source_league_ids=[self.league.id],
            start_year=YEAR,
            seed=1,
        )
        self.world_id = created.world.id
        self.scope = WorldScope.pennant(self.world_id)
        self._mark_the_world()
        self._play_a_game_in_the_world()

    def _mark_the_world(self) -> None:
        self.pennant_league = orm_models.League.objects.get(world_id=self.world_id)
        self.pennant_league.name = PENNANT_LEAGUE
        self.pennant_league.save()

        teams = orm_models.Team.objects.filter(league=self.pennant_league)
        self.pennant_team = teams.get(name=self.team.name)
        self.pennant_rival = teams.get(name=self.rival.name)
        orm_models.Team.objects.filter(id=self.pennant_team.id).update(name=PENNANT_TEAM)
        orm_models.Team.objects.filter(id=self.pennant_rival.id).update(name=PENNANT_RIVAL)

        players = orm_models.Player.objects.filter(stints__team__league=self.pennant_league).order_by("id")
        for index, player in enumerate(players, start=1):
            player.name = f"{PENNANT_PLAYER_PREFIX}{index:02d}"
            player.save()

    def pennant_players(self, team) -> list[int]:
        """その球団の選手の id を背番号順に。"""
        stints = orm_models.PlayerStint.objects.filter(team=team).order_by("number")
        return [stint.player_id for stint in stints]

    def _play_a_game_in_the_world(self) -> None:
        """世界の中で、打席まで揃った試合をひとつ行う。"""
        home = self.pennant_players(self.pennant_team)  # 背番号 1〜9 が打者、18 が投手
        away = self.pennant_players(self.pennant_rival)
        home_batters, home_pitcher = home[:9], home[9]
        away_batters, away_pitcher = away[:9], away[9]

        games = DjangoGameRepository(self.scope)
        empty = games.save(
            Game(
                season=Season(YEAR),
                played_on=date(YEAR, 4, 2),
                home_team_id=self.pennant_team.id,
                away_team_id=self.pennant_rival.id,
            )
        )
        lineup = [
            LineupSlot(
                team_id=team_id,
                player_id=player_id,
                batting_order=order,
                slot_sequence=0,
                fielding_position=FieldingPosition.DESIGNATED_HITTER,
            )
            for team_id, batters in ((self.pennant_team.id, home_batters), (self.pennant_rival.id, away_batters))
            for order, player_id in enumerate(batters, start=1)
        ]
        recording = GameRecordingService(
            games=games,
            teams=DjangoTeamRepository(self.scope),
            leagues=DjangoLeagueRepository(self.scope),
        )
        assert empty.id is not None
        saved = recording.record_scorebook(
            empty.id,
            year=YEAR,
            played_on=date(YEAR, 4, 2),
            home_team_id=self.pennant_team.id,
            away_team_id=self.pennant_rival.id,
            lineup=lineup,
            plate_appearances=to_plate_appearances(
                build_scorebook(
                    away=[2],
                    home=[5],
                    away_batters=away_batters,
                    home_batters=home_batters,
                    away_pitchers={1: away_pitcher},
                    home_pitchers={1: home_pitcher},
                )
            ),
        )
        self.pennant_game_id = saved.id
        self.pennant_home_batters = home_batters
