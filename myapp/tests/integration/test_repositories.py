"""リポジトリの往復。ORM ⇄ ドメインの変換でデータが失われないことを確認する。"""

from myapp.domain.entities import League
from myapp.domain.exceptions import (
    DuplicateJerseyNumber,
    InvalidGame,
    WorkspaceNotFound,
)
from myapp.domain.pennant.world import World
from myapp.domain.value_objects import (
    BattingLine,
    InningsPitched,
    JerseyNumber,
    PitchingLine,
    Position,
    RosterLimits,
)
from myapp.domain.workspace import Visibility
from myapp.infrastructure import orm_models
from myapp.infrastructure.repositories import (
    DjangoGameRepository,
    DjangoLeagueRepository,
    DjangoTeamRepository,
    DjangoWorldRepository,
)

from ..helpers import (
    give_batting,
    give_pitching,
    make_workspace,
    play_game,
    real_scope,
    world_scope,
)
from .base import BaseCase


class RepositoryRoundTripTest(BaseCase):
    """ORM ⇄ ドメインの往復でデータが失われないこと。"""

    def setUp(self):
        super().setUp()
        self.repo = DjangoTeamRepository(real_scope())

    def test_save_and_reload_a_player(self):
        team = self.repo.find_by_id(self.team.id)
        player = team.add_player("山田", JerseyNumber("10"), Position.INFIELDER, limits=RosterLimits.UNLIMITED)
        self.repo.save(team)

        saved = self.repo.find_by_id(self.team.id).find_player(player.id)

        self.assertEqual(saved.name, "山田")
        self.assertEqual(saved.number.value, "10")
        self.assertEqual(saved.position, Position.INFIELDER)

    def test_batting_totals_come_from_games(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        give_batting(self.team, self.rival, player.id, BattingLine(at_bats=4, singles=2), day=1)
        give_batting(self.team, self.rival, player.id, BattingLine(at_bats=3, home_runs=1), day=2)

        saved = self.repo.find_by_id(self.team.id).find_player(player.id)

        self.assertEqual(saved.batting.at_bats, 7)
        self.assertEqual(saved.batting.hits, 3)

    def test_innings_are_added_as_outs_not_decimals(self):
        """5.2 + 5.2 は 10.4 ではなく 11.1。"""
        player = self.service.register_player(self.team.id, "佐藤", 18, "投手")
        line = PitchingLine(innings=InningsPitched.from_notation("5.2"), earned_runs=1)
        give_pitching(self.team, self.rival, player.id, line, day=1)
        give_pitching(self.team, self.rival, player.id, line, day=2)

        saved = self.repo.find_by_id(self.team.id).find_player(player.id)

        self.assertEqual(saved.pitching.innings.outs, 34)
        self.assertEqual(str(saved.pitching.innings), "11.1")
        self.assertEqual(saved.pitching.earned_runs, 2)

    def test_player_without_games_has_empty_stats(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        saved = self.repo.find_by_id(self.team.id).find_player(player.id)

        self.assertEqual(saved.batting.at_bats, 0)
        self.assertEqual(saved.pitching.innings.outs, 0)

    def test_duplicate_number_is_rejected_on_the_aggregate(self):
        team = self.repo.find_by_id(self.team.id)
        team.add_player("山田", JerseyNumber("10"), Position.INFIELDER, limits=RosterLimits.UNLIMITED)
        self.repo.save(team)

        reloaded = self.repo.find_by_id(self.team.id)
        with self.assertRaises(DuplicateJerseyNumber):
            reloaded.add_player("田中", JerseyNumber("10"), Position.OUTFIELDER, limits=RosterLimits.UNLIMITED)


class GameRepositoryTest(BaseCase):
    def test_round_trip(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        saved = play_game(
            self.team,
            self.rival,
            home_score=5,
            away_score=3,
            batting={player.id: BattingLine(at_bats=4, singles=2, runs_batted_in=1)},
        )

        reloaded = DjangoGameRepository(real_scope()).find_by_id(saved.id)

        self.assertEqual(reloaded.home_score, 5)
        self.assertEqual(reloaded.result_for(self.team.id), "win")
        self.assertEqual(len(reloaded.batting), 1)
        self.assertEqual(reloaded.batting[0].line.hits, 2)

    def test_same_team_is_rejected(self):
        with self.assertRaises(InvalidGame):
            play_game(self.team, self.team)

    def test_filter_by_season(self):
        play_game(self.team, self.rival, year=2025, day=1)
        play_game(self.team, self.rival, year=2026, day=1)

        self.assertEqual(len(DjangoGameRepository(real_scope()).find_all(2026)), 1)
        self.assertEqual(len(DjangoGameRepository(real_scope()).find_all()), 2)

    def test_recording_the_same_player_twice_overwrites(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        game = play_game(self.team, self.rival, batting={player.id: BattingLine(at_bats=4, singles=1)})
        game.record_batting(player.id, BattingLine(at_bats=4, home_runs=2), team_id=game.home_team_id)
        DjangoGameRepository(real_scope()).save(game)

        self.assertEqual(orm_models.GameBattingLine.objects.filter(game_id=game.id).count(), 1)
        reloaded = DjangoGameRepository(real_scope()).find_by_id(game.id)
        self.assertEqual(reloaded.batting[0].line.home_runs, 2)


class WorldWorkspaceRoundTripTest(BaseCase):
    """世界の `workspace_id` が、保存と読み戻しで往復すること。"""

    def _workspace(self):
        from django.contrib.auth.models import User

        owner = User.objects.create_user("ws-owner", password="x")
        return orm_models.Workspace.objects.create(
            key="abcde12345", name="テスト用", owner=owner, visibility=Visibility.PUBLIC.value
        )

    def test_the_workspace_id_round_trips(self):
        workspace = self._workspace()
        repo = DjangoWorldRepository()

        saved = repo.save(World(name="世界A", seed=1, start_year=2040, workspace_id=workspace.id))

        self.assertEqual(repo.find_by_id(saved.id).workspace_id, workspace.id)
        self.assertEqual(orm_models.PennantWorld.objects.get(id=saved.id).workspace_id, workspace.id)

    def test_a_world_without_a_workspace_gets_the_legacy_one(self):
        repo = DjangoWorldRepository()

        saved = repo.save(World(name="世界B", seed=2, start_year=2040))

        self.assertEqual(saved.workspace_id, self.workspace.id)
        self.assertEqual(repo.find_by_id(saved.id).workspace_id, self.workspace.id)

    def test_a_world_without_a_workspace_is_rejected_when_none_exists(self):
        orm_models.Stadium.objects.all().delete()
        orm_models.Team.objects.all().delete()
        orm_models.League.objects.all().delete()
        orm_models.Workspace.objects.all().delete()

        with self.assertRaises(WorkspaceNotFound):
            DjangoWorldRepository().save(World(name="世界B", seed=2, start_year=2040))

        self.assertFalse(orm_models.PennantWorld.objects.exists())

    def test_saving_again_keeps_the_workspace(self):
        workspace = self._workspace()
        repo = DjangoWorldRepository()
        saved = repo.save(World(name="世界C", seed=3, start_year=2040, workspace_id=workspace.id))

        saved.name = "世界C改"
        repo.save(saved)

        self.assertEqual(repo.find_by_id(saved.id).workspace_id, workspace.id)


class LeagueWorkspaceTest(BaseCase):
    """リーグの保存が、帰属（実データはワークスペース・ペナントは世界）を埋めること。"""

    def test_a_real_league_gets_the_workspace_and_no_world(self):
        saved = DjangoLeagueRepository(real_scope()).save(League(name="新リーグ"))

        row = orm_models.League.objects.get(id=saved.id)
        self.assertEqual(row.workspace_id, self.workspace.id)
        self.assertIsNone(row.world_id)

    def test_a_pennant_league_gets_the_world_and_no_workspace(self):
        world = DjangoWorldRepository().save(World(name="世界", seed=1, start_year=2040))

        saved = DjangoLeagueRepository(world_scope(world.id)).save(League(name="世界のリーグ"))

        row = orm_models.League.objects.get(id=saved.id)
        self.assertEqual(row.world_id, world.id)
        self.assertIsNone(row.workspace_id)

    def test_the_legacy_workspace_is_the_smallest_id(self):
        make_workspace(name="あとから作った")

        saved = DjangoLeagueRepository(real_scope()).save(League(name="新リーグ"))

        self.assertEqual(orm_models.League.objects.get(id=saved.id).workspace_id, self.workspace.id)

    def test_saving_again_does_not_rewrite_the_workspace(self):
        repo = DjangoLeagueRepository(real_scope())
        saved = repo.save(League(name="新リーグ"))
        other = make_workspace(name="別")
        orm_models.League.objects.filter(id=saved.id).update(workspace=other)

        saved.display_order = 5
        repo.save(saved)

        row = orm_models.League.objects.get(id=saved.id)
        self.assertEqual((row.workspace_id, row.display_order), (other.id, 5))

    def test_without_any_workspace_nothing_is_written(self):
        orm_models.Stadium.objects.all().delete()
        orm_models.Team.objects.all().delete()
        orm_models.League.objects.all().delete()
        orm_models.Workspace.objects.all().delete()

        with self.assertRaisesMessage(
            WorkspaceNotFound,
            "ワークスペースがありません。運営者が管理画面で最初のワークスペースを作るまで、この操作はできません。",
        ):
            DjangoLeagueRepository(real_scope()).save(League(name="新リーグ"))

        self.assertFalse(orm_models.League.objects.exists())
