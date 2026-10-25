"""リポジトリの往復。ORM ⇄ ドメインの変換でデータが失われないことを確認する。"""

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext

from myapp.domain.entities import League, Team
from myapp.domain.exceptions import (
    DuplicateJerseyNumber,
    InvalidGame,
    InvalidWorld,
    LeagueNotFound,
    WorldNotFound,
)
from myapp.domain.pennant.world import World
from myapp.domain.value_objects import (
    BattingLine,
    ForeignPlayerRules,
    InningsPitched,
    JerseyNumber,
    PitchingLine,
    Position,
    RosterLimits,
)
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
    make_league,
    make_stadium,
    make_workspace,
    play_game,
    real_scope,
    world_ledger,
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
        player = self.roster.register_player(self.team.id, "山田", 10, "内野手")
        give_batting(self.team, self.rival, player.id, BattingLine(at_bats=4, singles=2), day=1)
        give_batting(self.team, self.rival, player.id, BattingLine(at_bats=3, home_runs=1), day=2)

        saved = self.repo.find_by_id(self.team.id).find_player(player.id)

        self.assertEqual(saved.batting.at_bats, 7)
        self.assertEqual(saved.batting.hits, 3)

    def test_innings_are_added_as_outs_not_decimals(self):
        """5.2 + 5.2 は 10.4 ではなく 11.1。"""
        player = self.roster.register_player(self.team.id, "佐藤", 18, "投手")
        line = PitchingLine(innings=InningsPitched.from_notation("5.2"), earned_runs=1)
        give_pitching(self.team, self.rival, player.id, line, day=1)
        give_pitching(self.team, self.rival, player.id, line, day=2)

        saved = self.repo.find_by_id(self.team.id).find_player(player.id)

        self.assertEqual(saved.pitching.innings.outs, 34)
        self.assertEqual(str(saved.pitching.innings), "11.1")
        self.assertEqual(saved.pitching.earned_runs, 2)

    def test_player_without_games_has_empty_stats(self):
        player = self.roster.register_player(self.team.id, "山田", 10, "内野手")
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


class HomeStadiumCheckQueriesTest(BaseCase):
    """本拠地の検査（W1b T4）は、新しいチームと本拠地を変えた保存でだけ球場を引く。

    チームの存在を確かめるクエリで今の本拠地も読むので、本拠地を変えない保存（シーズンを締める処理など、
    ロスターだけを書く保存）のクエリ数は検査を入れる前と同じ。
    """

    def setUp(self):
        super().setUp()
        self.repo = DjangoTeamRepository(real_scope())
        self.other = make_stadium("第二球場")

    def _queries_to_save(self, change) -> int:
        team = self.repo.find_by_id(self.team.id)
        change(team)
        with CaptureQueriesContext(connection) as captured:
            self.repo.save(team)
        return len(captured)

    def test_only_a_changed_stadium_costs_one_query(self):
        # 名前を変えない保存を基準にする（改名はチーム名の重複の検査で1回引く。W3 の `NameCheckQueriesTest`）
        unchanged = self._queries_to_save(lambda team: setattr(team, "display_order", 5))
        changed = self._queries_to_save(lambda team: setattr(team, "home_stadium_id", self.other.id))
        cleared = self._queries_to_save(lambda team: setattr(team, "home_stadium_id", None))

        self.assertEqual(changed, unchanged + 1, "本拠地を変えた保存だけが球場を1回引く")
        self.assertEqual(cleared, unchanged, "本拠地を外す保存は球場を引かない")
        self.assertIsNone(orm_models.Team.objects.get(id=self.team.id).home_stadium_id)

    def test_a_new_team_takes_a_stadium_of_its_workspace(self):
        saved = self.repo.save(Team(name="新球団", league_id=self.league.id, home_stadium_id=self.other.id))

        self.assertEqual(orm_models.Team.objects.get(id=saved.id).home_stadium_id, self.other.id)


class GameRepositoryTest(BaseCase):
    def test_round_trip(self):
        player = self.roster.register_player(self.team.id, "山田", 10, "内野手")
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
        player = self.roster.register_player(self.team.id, "山田", 10, "内野手")
        game = play_game(self.team, self.rival, batting={player.id: BattingLine(at_bats=4, singles=1)})
        game.record_batting(player.id, BattingLine(at_bats=4, home_runs=2), team_id=game.home_team_id)
        DjangoGameRepository(real_scope()).save(game)

        self.assertEqual(orm_models.GameBattingLine.objects.filter(game_id=game.id).count(), 1)
        reloaded = DjangoGameRepository(real_scope()).find_by_id(game.id)
        self.assertEqual(reloaded.batting[0].line.home_runs, 2)


class WorldWorkspaceRoundTripTest(BaseCase):
    """世界の台帳（ワークスペースで絞る）の保存と読み戻し。"""

    def setUp(self):
        super().setUp()
        self.other = make_workspace(name="テスト用")

    def test_the_workspace_id_round_trips(self):
        repo = DjangoWorldRepository(self.other.id)

        saved = repo.save(World(name="世界A", seed=1, start_year=2040, workspace_id=self.other.id))

        self.assertEqual(repo.find_by_id(saved.id).workspace_id, self.other.id)
        self.assertEqual(orm_models.PennantWorld.objects.get(id=saved.id).workspace_id, self.other.id)

    def test_a_world_without_a_workspace_is_rejected(self):
        """ワークスペースの無い世界は保存しない（台帳のワークスペースで補わない。W1b T2）。"""
        before = orm_models.PennantWorld.objects.count()

        with self.assertRaisesMessage(InvalidWorld, "世界はワークスペースに属している必要があります。"):
            DjangoWorldRepository(self.workspace.id).save(World(name="世界B", seed=2, start_year=2040))

        self.assertEqual(orm_models.PennantWorld.objects.count(), before)

    def test_a_world_of_another_workspace_is_rejected(self):
        """台帳と違うワークスペースの世界は、新しい世界でも保存しない（W1b T3）。"""
        before = orm_models.PennantWorld.objects.count()

        with self.assertRaisesMessage(InvalidWorld, "ほかのワークスペースの世界は保存できません。"):
            DjangoWorldRepository(self.workspace.id).save(
                World(name="世界B", seed=2, start_year=2040, workspace_id=self.other.id)
            )

        self.assertEqual(orm_models.PennantWorld.objects.count(), before)

    def test_saving_again_keeps_the_workspace(self):
        repo = DjangoWorldRepository(self.other.id)
        saved = repo.save(World(name="世界C", seed=3, start_year=2040, workspace_id=self.other.id))

        saved.name = "世界C改"
        repo.save(saved)

        self.assertEqual(repo.find_by_id(saved.id).name, "世界C改")
        self.assertEqual(repo.find_by_id(saved.id).workspace_id, self.other.id)

    def test_an_unknown_id_is_not_created(self):
        """台帳に無い id の世界は作らない（`update_or_create(id=...)` のように新しい行を作らない）。"""
        before = orm_models.PennantWorld.objects.count()

        with self.assertRaises(WorldNotFound):
            DjangoWorldRepository(self.workspace.id).save(
                World(id=999, name="世界D", seed=4, start_year=2040, workspace_id=self.workspace.id)
            )

        self.assertEqual(orm_models.PennantWorld.objects.count(), before)

    def test_the_ledger_reads_and_deletes_only_its_workspaces_worlds(self):
        mine = world_ledger().save(World(name="自分の世界", seed=1, start_year=2040, workspace_id=self.workspace.id))
        theirs = DjangoWorldRepository(self.other.id).save(
            World(name="他所の世界", seed=2, start_year=2040, workspace_id=self.other.id)
        )
        ledger = world_ledger()

        self.assertEqual([world.id for world in ledger.find_all()], [mine.id])
        with self.assertRaises(WorldNotFound):
            ledger.find_by_id(theirs.id)
        with self.assertRaises(WorldNotFound):
            ledger.delete(theirs.id)
        self.assertTrue(orm_models.PennantWorld.objects.filter(id=theirs.id).exists())

    def test_the_owner_count_spans_workspaces(self):
        """世界の数の上限は1人あたりなので、`count_by_owner` は全ワークスペースで数える（W6 で見直す）。"""
        owner = User.objects.create_user("world-owner", password="x")
        for workspace in (self.workspace, self.other):
            DjangoWorldRepository(workspace.id).save(
                World(name="世界", seed=1, start_year=2040, owner_id=owner.id, workspace_id=workspace.id)
            )

        self.assertEqual(world_ledger().count_by_owner(owner.id), 2)


class LeagueWorkspaceTest(BaseCase):
    """リーグの保存が、帰属（実データはワークスペース・ペナントは世界）を埋めること。"""

    def test_a_real_league_gets_the_workspace_and_no_world(self):
        saved = DjangoLeagueRepository(real_scope()).save(League(name="新リーグ"))

        row = orm_models.League.objects.get(id=saved.id)
        self.assertEqual(row.workspace_id, self.workspace.id)
        self.assertIsNone(row.world_id)

    def test_a_pennant_league_gets_the_world_and_no_workspace(self):
        world = world_ledger().save(World(name="世界", seed=1, start_year=2040, workspace_id=self.workspace.id))

        saved = DjangoLeagueRepository(world_scope(world.id)).save(League(name="世界のリーグ"))

        row = orm_models.League.objects.get(id=saved.id)
        self.assertEqual(row.world_id, world.id)
        self.assertIsNone(row.workspace_id)

    def test_a_real_league_goes_to_the_workspace_of_the_scope(self):
        """新しい実データのリーグは範囲のワークスペースに属す（id が最小のワークスペースではない）。"""
        other = make_workspace(name="あとから作った")

        saved = DjangoLeagueRepository(real_scope(other)).save(League(name="新リーグ"))

        self.assertEqual(orm_models.League.objects.get(id=saved.id).workspace_id, other.id)

    def test_saving_again_does_not_rewrite_the_workspace(self):
        repo = DjangoLeagueRepository(real_scope())
        saved = repo.save(League(name="新リーグ"))

        saved.display_order = 5
        repo.save(saved)

        row = orm_models.League.objects.get(id=saved.id)
        self.assertEqual((row.workspace_id, row.display_order), (self.workspace.id, 5))

    def test_a_league_of_another_workspace_cannot_be_written(self):
        """ほかのワークスペースのリーグの id を渡しても、範囲の外なので LeagueNotFound で何も書かない。"""
        other = make_workspace(name="別")
        saved = DjangoLeagueRepository(real_scope(other)).save(League(name="別のリーグ"))

        saved.display_order = 5
        with self.assertRaises(LeagueNotFound):
            DjangoLeagueRepository(real_scope()).save(saved)

        row = orm_models.League.objects.get(id=saved.id)
        self.assertEqual((row.workspace_id, row.display_order), (other.id, 0))


class LeagueForeignRulesRoundTripTest(BaseCase):
    """リーグの外国人枠（協約第82条の2）の3列 ⇄ `ForeignPlayerRules`。"""

    def test_a_new_league_follows_the_agreement(self):
        """列の既定（マイグレーションで既存のリーグを埋める値）は `ForeignPlayerRules.NPB`。"""
        league = make_league("新リーグ")

        found = DjangoLeagueRepository(real_scope()).find_by_id(league.id)

        self.assertEqual(found.foreign_rules, ForeignPlayerRules.NPB)

    def test_the_three_limits_survive_a_round_trip(self):
        repository = DjangoLeagueRepository(real_scope())
        league = repository.find_by_id(self.league.id)
        league.foreign_rules = ForeignPlayerRules(active=5, pitchers=None, batters=2)

        repository.save(league)

        row = orm_models.League.objects.get(id=self.league.id)
        self.assertEqual(
            (row.foreign_active_limit, row.foreign_active_pitcher_limit, row.foreign_active_batter_limit), (5, None, 2)
        )
        self.assertEqual(repository.find_by_id(self.league.id).foreign_rules, league.foreign_rules)
