"""ペナントの世界を使うテストの共通の土台。

実データ（`BaseCase` のリーグ・チーム2つ）に選手と試合を入れ、それを分岐して世界をひとつ作る。
世界の側の名前は、実データのどの名前とも**重ならない目印**に付け替える（分岐で写した名前は
元と同じなので、そのままでは「混ざったか」を名前で見分けられない）。目印の名前が実データの
画面に出たら、それは混入。
"""

from datetime import date
from unittest.mock import patch

from django.urls import reverse

from myapp.application.dto import LineupSlot
from myapp.application.game_recording import GameRecordingService
from myapp.domain.entities import Game, Player
from myapp.domain.pennant.world import WorldScope
from myapp.domain.value_objects import BattingLine, FieldingPosition, GameStage, JerseyNumber, Season
from myapp.infrastructure import orm_models
from myapp.infrastructure.queries import DjangoHomeStadiumQuery
from myapp.infrastructure.repositories import DjangoGameRepository, DjangoLeagueRepository, DjangoTeamRepository
from myapp.presentation.views import build_pennant_world_service

from ..helpers import build_roster_service, build_scorebook, play_game, register_lineup, to_plate_appearances
from .base import ROSTER_CHECK, BaseCase

PENNANT_LEAGUE = "目印リーグ"
PENNANT_TEAM = "目印ホーム球団"
PENNANT_RIVAL = "目印ビジター球団"
PENNANT_PLAYER_PREFIX = "目印選手"
PENNANT_WORLD = "目印の世界"
YEAR = 2026


class WorldCase(BaseCase):
    """実データ（リーグ1・チーム2）と、それを分岐した世界ひとつ。

    準備（選手の登録・試合・世界の分岐）はクラスで1回だけ行う（`setUpTestData`）。テストごとの変更は
    `TestCase` がトランザクションで巻き戻すので、テストどうしは独立のまま。
    """

    real_batters: list[int]
    real_rivals: list[int]
    real_pitcher: Player
    real_rival_pitcher: Player
    real_game: Game
    world_id: int
    scope: WorldScope
    pennant_league: orm_models.League
    pennant_team: orm_models.Team
    pennant_rival: orm_models.Team
    pennant_game_id: int | None
    pennant_home_batters: list[int]

    def setUp(self):
        super().setUp()
        # 世界の作成の名簿の検査は、テストの中で世界を作り直すときにも外しておく
        self.skip_roster_check()

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        with patch(ROSTER_CHECK):
            cls._build_the_real_data_and_the_world()

    @classmethod
    def _build_the_real_data_and_the_world(cls) -> None:
        roster = build_roster_service()
        # --- 実データ。各チームに打者9人と投手1人、記録済みの試合を1つ ---
        cls.real_batters = register_lineup(roster, cls.team, prefix="実打者", first_number=1)
        cls.real_rivals = register_lineup(roster, cls.rival, prefix="実相手", first_number=1)
        cls.real_pitcher = roster.register_player(cls.team.id, "実投手", "18", "投手")
        cls.real_rival_pitcher = roster.register_player(cls.rival.id, "実相手投手", "19", "投手")
        cls.real_game = play_game(
            cls.team,
            cls.rival,
            home_score=3,
            away_score=1,
            year=YEAR,
            batting={cls.real_batters[0]: BattingLine(at_bats=4, singles=2)},
        )

        # --- 世界。分岐してから、世界の側の名前を目印に付け替える ---
        created = build_pennant_world_service().create_world(
            name=PENNANT_WORLD,
            owner_id=None,
            source_league_ids=[cls.league.id],
            start_year=YEAR,
            seed=1,
        )
        cls.world_id = created.world.id
        cls.scope = WorldScope.pennant(cls.world_id)
        cls._mark_the_world()
        cls._play_a_game_in_the_world()

    @classmethod
    def _mark_the_world(cls) -> None:
        cls.pennant_league = orm_models.League.objects.get(world_id=cls.world_id)
        cls.pennant_league.name = PENNANT_LEAGUE
        cls.pennant_league.save()

        teams = orm_models.Team.objects.filter(league=cls.pennant_league)
        cls.pennant_team = teams.get(name=cls.team.name)
        cls.pennant_rival = teams.get(name=cls.rival.name)
        orm_models.Team.objects.filter(id=cls.pennant_team.id).update(name=PENNANT_TEAM)
        orm_models.Team.objects.filter(id=cls.pennant_rival.id).update(name=PENNANT_RIVAL)

        players = orm_models.Player.objects.filter(stints__team__league=cls.pennant_league).order_by("id")
        for index, player in enumerate(players, start=1):
            player.name = f"{PENNANT_PLAYER_PREFIX}{index:02d}"
            player.save()

    @classmethod
    def pennant_players(cls, team) -> list[int]:
        """その球団の選手の id を背番号順に。"""
        # 背番号は文字列の列なので、SQL ではなく背番号順（`JerseyNumber.sort_key`）で並べる
        stints = sorted(
            orm_models.PlayerStint.objects.filter(team=team), key=lambda s: (JerseyNumber(s.number).sort_key, s.id)
        )
        return [stint.player_id for stint in stints]

    @classmethod
    def _play_a_game_in_the_world(cls) -> None:
        """世界の中で、打席まで揃った試合をひとつ行う。"""
        home = cls.pennant_players(cls.pennant_team)  # 背番号 1〜9 が打者、18 が投手
        away = cls.pennant_players(cls.pennant_rival)
        home_batters, home_pitcher = home[:9], home[9]
        away_batters, away_pitcher = away[:9], away[9]

        games = DjangoGameRepository(cls.scope)
        empty = games.save(
            Game(
                season=Season(YEAR),
                played_on=date(YEAR, 4, 2),
                home_team_id=cls.pennant_team.id,
                away_team_id=cls.pennant_rival.id,
                stage=GameStage.REGULAR,
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
            for team_id, batters in ((cls.pennant_team.id, home_batters), (cls.pennant_rival.id, away_batters))
            for order, player_id in enumerate(batters, start=1)
        ]
        recording = GameRecordingService(
            games=games,
            teams=DjangoTeamRepository(cls.scope),
            leagues=DjangoLeagueRepository(cls.scope),
            home_stadiums=DjangoHomeStadiumQuery(cls.scope),
        )
        assert empty.id is not None
        saved = recording.record_scorebook(
            empty.id,
            year=YEAR,
            played_on=date(YEAR, 4, 2),
            home_team_id=cls.pennant_team.id,
            away_team_id=cls.pennant_rival.id,
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
            times=None,
            attendance=None,
        )
        cls.pennant_game_id = saved.game_id
        cls.pennant_home_batters = home_batters

    def world_urls(self, world_id=None) -> list[str]:
        """世界の範囲の参照画面の URL をすべて（世界の id を省けば、この世界）。"""
        world_id = self.world_id if world_id is None else world_id
        team = orm_models.Team.objects.filter(league__world_id=world_id).order_by("id").first()
        assert team is not None
        league = team.league
        stint = min(
            orm_models.PlayerStint.objects.filter(team=team), key=lambda s: (JerseyNumber(s.number).sort_key, s.id)
        )
        assert stint is not None
        player_id = stint.player_id
        game = orm_models.Game.objects.filter(home_team__league__world_id=world_id).first()
        urls = [
            reverse("pennant_world", args=[world_id]),
            reverse("pennant_standings", args=[world_id]),
            reverse("pennant_standings_by_year", args=[world_id, YEAR]),
            reverse("pennant_league_detail", args=[world_id, league.id]),
            reverse("pennant_league_detail_by_year", args=[world_id, league.id, YEAR]),
            reverse("pennant_league_titles", args=[world_id, league.id]),
            reverse("pennant_league_titles_by_year", args=[world_id, league.id, YEAR]),
            reverse("pennant_league_stats", args=[world_id, league.id]),
            reverse("pennant_team_list", args=[world_id]),
            reverse("pennant_game_list", args=[world_id]),
            reverse("pennant_history", args=[world_id]),
            reverse("pennant_team_history", args=[world_id, team.id]),
            *(reverse("pennant_club", args=[world_id]) + f"?tab={tab}" for tab in ("active", "lineup", "pitching")),
            reverse("pennant_player_list", args=[world_id, team.id]),
            reverse("pennant_player_detail", args=[world_id, team.id, player_id]),
            reverse("pennant_analysis_index", args=[world_id]),
            *(
                reverse("pennant_team_analysis", args=[world_id, team.id]) + f"?tab={tab}"
                for tab in ("depth", "usage", "fa", "stats")
            ),
            reverse("pennant_team_analysis", args=[world_id, team.id]) + "?tab=stats&pos=pitcher",
        ]
        if game is not None:
            urls.append(reverse("pennant_game_detail", args=[world_id, game.id]))
        return urls
