"""実データのリーグを分岐して世界を作る・世界を消す。

複製するのはリーグ・球団・選手のプロフィール・現在の在籍だけ（加入年は開幕年）。試合と過去の
在籍は写さず、球場は共有する。分岐の業務ルールは `tests/domain/test_pennant_world.py`、
ここでは保存と読み込み（集約経由の往復）、実データに触れないこと、削除を見る。
"""

from datetime import date
from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import TestCase

from myapp.application.pennant_world import PennantWorldService, WorldRepositories
from myapp.domain.exceptions import InvalidSeason, InvalidWorld, LeagueNotFound, TeamNotFound, WorldNotFound
from myapp.domain.pennant.world import World, WorldScope
from myapp.infrastructure import orm_models
from myapp.infrastructure.queries import DjangoFieldingTotalsQuery
from myapp.infrastructure.repositories import (
    DjangoLeagueRepository,
    DjangoRatingsRepository,
    DjangoTeamRepository,
    DjangoWorldRepository,
)
from myapp.presentation.views import build_pennant_world_service

from ..helpers import play_game
from .base import BaseCase
from .test_world_isolation import CLASSIFICATION, GAME, LEAGUE, PLAYER
from .world_case import PENNANT_LEAGUE, PENNANT_TEAM, WorldCase

START = 2030
REAL = WorldScope.real()


def _real_counts() -> dict[str, int]:
    """実データの行数。分岐の前後で変わってはならない。"""
    return {
        "league": orm_models.League.objects.filter(world__isnull=True).count(),
        "team": orm_models.Team.objects.filter(league__world__isnull=True).count(),
        "stint": orm_models.PlayerStint.objects.filter(team__league__world__isnull=True).count(),
        "captaincy": orm_models.Captaincy.objects.filter(team__league__world__isnull=True).count(),
        "game": orm_models.Game.objects.filter(home_team__league__world__isnull=True).count(),
        "player": orm_models.Player.objects.count() - _pennant_players(),
    }


def _pennant_players() -> int:
    return orm_models.Player.objects.filter(stints__team__league__world__isnull=False).distinct().count()


class ForkTest(BaseCase):
    """分岐した世界の中身。"""

    def setUp(self):
        super().setUp()
        self.captain = self.service.register_player(self.team.id, "主将", 10, "内野手")
        self.pitcher = self.service.register_player(self.team.id, "投手", 18, "投手")
        self.retired = self.service.register_player(self.team.id, "退団", 5, "外野手")
        self.service.register_player(self.rival.id, "相手の選手", 10, "内野手")
        self.service.appoint_captain(self.team.id, self.captain.id)
        self.service.retire_player(self.team.id, self.retired.id)
        orm_models.Player.objects.filter(id=self.captain.id).update(
            birth_date=date(1995, 3, 4),
            throws="右",
            bats="左",
            height_cm=180,
            nationality="日本",
            birthplace="東京都",
            high_school="東京高校",
            debut_year=2014,
        )
        orm_models.Player.objects.filter(id=self.pitcher.id).update(is_foreign_player=True, nationality="米国")
        play_game(self.team, self.rival, year=2026, home_score=2, away_score=1)
        self.created = self._fork()
        self.world = orm_models.PennantWorld.objects.get(id=self.created.world.id)
        self.copied_team = orm_models.Team.objects.get(league__world=self.world, name=self.team.name)
        self.copied_rival = orm_models.Team.objects.get(league__world=self.world, name=self.rival.name)

    def _fork(self, **overrides):
        arguments = {
            "name": "検査",
            "owner_id": None,
            "source_league_ids": [self.league.id],
            "start_year": START,
            "seed": 7,
        } | overrides
        return build_pennant_world_service().create_world(**arguments)

    def test_the_world_records_what_was_decided(self):
        self.assertEqual(
            (self.world.name, self.world.seed, self.world.start_year, self.world.owner_id), ("検査", 7, START, None)
        )
        self.assertEqual(
            (self.created.league_count, self.created.team_count, self.created.player_count),
            (1, 2, 3),
            "現在の選手は主将・投手・相手の選手の3人（退団した選手は数えない）",
        )

    def test_leagues_and_teams_are_copied_into_the_world(self):
        league = orm_models.League.objects.get(world=self.world)

        self.assertEqual(league.name, self.league.name, "実データと同名のリーグを作れる")
        self.assertEqual(
            set(orm_models.Team.objects.filter(league=league).values_list("name", flat=True)),
            {self.team.name, self.rival.name},
        )

    def test_the_league_rules_are_copied(self):
        orm_models.League.objects.filter(id=self.league.id).update(
            foreign_player_roster_limit=4, foreign_player_game_limit=2, display_order=3
        )

        world = self._fork(name="規則")
        copy = orm_models.League.objects.get(world_id=world.world.id)

        self.assertEqual(
            (copy.foreign_player_roster_limit, copy.foreign_player_game_limit, copy.display_order), (4, 2, 0)
        )

    def test_the_stadium_is_shared(self):
        self.assertEqual(self.copied_team.home_stadium_id, self.stadium.id)
        self.assertEqual(orm_models.Stadium.objects.count(), 1, "球場は複製しない")

    def test_only_current_players_are_copied_with_the_start_year(self):
        stints = orm_models.PlayerStint.objects.filter(team__league__world=self.world)

        self.assertEqual(
            set(stints.values_list("player__name", flat=True)),
            {"主将", "投手", "相手の選手"},
            "退団した選手は写さない",
        )
        for stint in stints:
            self.assertEqual((stint.from_year, stint.to_year), (START, None))
        self.assertEqual(stints.count(), 3, "過去の在籍は写さない")

    def test_jersey_numbers_and_positions_are_copied(self):
        stint = orm_models.PlayerStint.objects.get(team=self.copied_team, player__name="投手")

        self.assertEqual(stint.number, 18)
        self.assertEqual(stint.player.position, "投手")

    def test_the_profile_is_copied(self):
        copy = orm_models.Player.objects.get(stints__team=self.copied_team, name="主将")

        self.assertEqual(
            (copy.birth_date, copy.throws, copy.bats, copy.height_cm, copy.nationality, copy.birthplace),
            (date(1995, 3, 4), "右", "左", 180, "日本", "東京都"),
        )
        self.assertEqual((copy.high_school, copy.debut_year), ("東京高校", 2014))
        foreign = orm_models.Player.objects.get(stints__team=self.copied_team, name="投手")
        self.assertTrue(foreign.is_foreign_player)

    def test_players_are_new_rows_not_shared(self):
        original = orm_models.Player.objects.get(id=self.captain.id)
        copy = orm_models.Player.objects.get(stints__team=self.copied_team, name="主将")

        self.assertNotEqual(original.id, copy.id)

    def test_games_and_captaincy_are_not_copied(self):
        self.assertEqual(orm_models.Game.objects.filter(home_team__league__world=self.world).count(), 0)
        self.assertEqual(orm_models.Captaincy.objects.filter(team__league__world=self.world).count(), 0)

    def test_the_real_data_is_untouched(self):
        before = _real_counts()

        self._fork(name="もう一つ")

        self.assertEqual(_real_counts(), before)
        stint = orm_models.PlayerStint.objects.get(player_id=self.captain.id, team=self.team)
        self.assertEqual(stint.from_year, date.today().year, "実データの加入年は書き換わらない")
        self.assertIsNone(stint.to_year)

    def test_editing_the_world_does_not_change_the_real_data(self):
        """分岐した後は同期しない。世界の選手を直しても、実データの選手は変わらない。"""
        copy = orm_models.Player.objects.get(stints__team=self.copied_team, name="主将")
        copy.name = "世界でだけ改名"
        copy.save()

        self.assertEqual(orm_models.Player.objects.get(id=self.captain.id).name, "主将")

    def test_two_worlds_can_be_made_from_the_same_source(self):
        second = self._fork(name="二つ目")

        self.assertNotEqual(second.world.id, self.world.id)
        self.assertEqual(orm_models.League.objects.filter(name=self.league.name).count(), 3)
        # 世界ごとに選手の行が別
        self.assertEqual(orm_models.Player.objects.filter(name="主将").count(), 3)

    def test_a_forked_world_can_be_read_through_its_scope(self):
        """集約経由で保存したものが、世界の範囲のリポジトリで往復して読める。"""
        scope = WorldScope.pennant(self.world.id)
        teams = DjangoTeamRepository(scope).find_all_with_roster()

        self.assertEqual({t.name for t in teams}, {self.team.name, self.rival.name})
        mine = next(t for t in teams if t.name == self.team.name)
        self.assertEqual(sorted(p.name for p in mine.active_players), ["主将", "投手"])
        self.assertEqual(sorted(p.number.value for p in mine.active_players), [10, 18])
        self.assertTrue(all(p.career[0].from_year == START for p in mine.players))
        self.assertEqual([league.name for league in DjangoLeagueRepository(scope).find_all()], [self.league.name])

    def test_the_league_name_is_unique_within_a_world(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            orm_models.League.objects.create(name=self.league.name, world=self.world)

    def test_the_league_name_is_still_unique_among_the_real_leagues(self):
        """world が NULL のどうしは一意制約で衝突しない。条件付きの制約で実データ側の一意性を保つ。"""
        with self.assertRaises(IntegrityError), transaction.atomic():
            orm_models.League.objects.create(name=self.league.name)

    def test_the_same_league_name_is_allowed_across_worlds(self):
        other = self._fork(name="別の世界")

        self.assertEqual(orm_models.League.objects.filter(world_id=other.world.id, name=self.league.name).count(), 1)


class ForkOptionsTest(BaseCase):
    def setUp(self):
        super().setUp()
        self.service_ = build_pennant_world_service()
        self.service.register_player(self.team.id, "選手", 1, "内野手")
        self.second_league = orm_models.League.objects.create(name="第二リーグ", display_order=1)
        self.other_team = orm_models.Team.objects.create(league=self.second_league, name="第二のチーム")

    def _create(self, **overrides):
        arguments = {
            "name": "検査",
            "owner_id": None,
            "source_league_ids": [self.league.id],
            "start_year": START,
            "seed": 1,
        } | overrides
        return self.service_.create_world(**arguments)

    def test_several_leagues_keep_the_order_given(self):
        created = self._create(source_league_ids=[self.second_league.id, self.league.id])

        names = [league.name for league in DjangoLeagueRepository(WorldScope.pennant(created.world.id)).find_all()]
        self.assertEqual(names, ["第二リーグ", self.league.name], "指定した順に並ぶ")
        self.assertEqual(created.league_count, 2)
        self.assertEqual(created.team_count, 3)

    def test_the_managed_team_is_picked_by_the_source_team(self):
        created = self._create(managed_source_team_id=self.team.id)

        managed = orm_models.Team.objects.get(id=created.world.managed_team_id)
        self.assertEqual(managed.name, self.team.name)
        self.assertEqual(managed.league.world_id, created.world.id, "写した先の球団が受け持ちになる")

    def test_the_managed_team_and_owner_may_be_left_for_later(self):
        created = self._create()

        self.assertIsNone(created.world.managed_team_id)
        self.assertIsNone(created.world.owner_id)

    def test_the_owner_is_recorded(self):
        owner = User.objects.create_user("gm", password="x")

        created = self._create(owner_id=owner.id)

        self.assertEqual(orm_models.PennantWorld.objects.get(id=created.world.id).owner_id, owner.id)

    def test_a_team_outside_the_chosen_leagues_cannot_be_managed(self):
        with self.assertRaises(TeamNotFound):
            self._create(managed_source_team_id=self.other_team.id)

        self.assertEqual(orm_models.PennantWorld.objects.count(), 0, "半端な世界を残さない")

    def test_no_league_is_rejected(self):
        with self.assertRaises(InvalidWorld):
            self._create(source_league_ids=[])

    def test_a_blank_name_is_rejected_without_leaving_a_world(self):
        with self.assertRaises(InvalidWorld):
            self._create(name="  ")

        self.assertEqual(orm_models.PennantWorld.objects.count(), 0)

    def test_a_bad_start_year_is_rejected_without_leaving_a_world(self):
        with self.assertRaises(InvalidSeason):
            self._create(start_year=1800)

        self.assertEqual(orm_models.PennantWorld.objects.count(), 0)

    def test_an_unknown_league_is_rejected_without_leaving_a_world(self):
        with self.assertRaises(LeagueNotFound):
            self._create(source_league_ids=[self.league.id, 99999])

        self.assertEqual(orm_models.PennantWorld.objects.count(), 0)

    def test_a_pennant_league_cannot_be_a_source(self):
        """分岐元に使えるのは実データのリーグだけ（世界の世界は作らない）。"""
        created = self._create()
        pennant_league = orm_models.League.objects.get(world_id=created.world.id)

        with self.assertRaises(LeagueNotFound):
            self._create(source_league_ids=[pennant_league.id])

    def test_a_failure_in_the_middle_leaves_nothing_behind(self):
        class FailingTeams:
            """3回目の保存で落ちる。1球団ぶんの保存を済ませたあとの失敗を再現する。"""

            def __init__(self, inner):
                self._inner = inner
                self._saves = 0

            def save(self, team):
                self._saves += 1
                if self._saves == 3:
                    raise RuntimeError("途中で失敗")
                return self._inner.save(team)

        def factory(scope):
            return WorldRepositories(
                leagues=DjangoLeagueRepository(scope),
                teams=FailingTeams(DjangoTeamRepository(scope)),
                ratings=DjangoRatingsRepository(scope),
            )

        service = PennantWorldService(
            real_leagues=DjangoLeagueRepository(REAL),
            real_teams=DjangoTeamRepository(REAL),
            real_fielding=DjangoFieldingTotalsQuery(REAL),
            worlds=DjangoWorldRepository(),
            repositories_for=factory,  # type: ignore[arg-type]
            atomic=transaction.atomic,
            ensure_schedule=lambda world_id: False,
        )

        with self.assertRaises(RuntimeError):
            service.create_world(
                name="失敗", owner_id=None, source_league_ids=[self.league.id], start_year=START, seed=1
            )

        self.assertEqual(orm_models.PennantWorld.objects.count(), 0)
        self.assertEqual(orm_models.League.objects.filter(world__isnull=False).count(), 0)
        self.assertEqual(orm_models.Team.objects.exclude(league__world__isnull=True).count(), 0)
        self.assertEqual(orm_models.Player.objects.filter(name="選手").count(), 1, "実データの選手だけが残る")
        self.assertEqual(orm_models.PennantPlayerRatings.objects.count(), 0)


class WorldRepositoryTest(TestCase):
    def test_a_world_round_trips(self):
        repository = DjangoWorldRepository()
        owner = User.objects.create_user("gm", password="x")

        saved = repository.save(World(name="世界", seed=2**40, start_year=2026, owner_id=owner.id))
        loaded = repository.find_by_id(saved.id)

        self.assertEqual(loaded, saved)
        self.assertEqual(loaded.seed, 2**40)

    def test_the_newest_world_comes_first(self):
        repository = DjangoWorldRepository()
        first = repository.save(World(name="古い", seed=1, start_year=2026))
        second = repository.save(World(name="新しい", seed=2, start_year=2026))

        self.assertEqual([w.id for w in repository.find_all()], [second.id, first.id])

    def test_a_missing_world_is_not_found(self):
        with self.assertRaises(WorldNotFound):
            DjangoWorldRepository().find_by_id(999)

    def test_the_list_through_the_service(self):
        DjangoWorldRepository().save(World(name="世界", seed=1, start_year=2026))

        rows = build_pennant_world_service().list_worlds()

        self.assertEqual([row.name for row in rows], ["世界"])
        self.assertEqual(build_pennant_world_service().get_world(rows[0].id).name, "世界")


class DeleteWorldTest(WorldCase):
    """世界の削除。子のテーブルから範囲で絞って消し、実データには触れない。"""

    def _rows_in_world(self) -> dict[str, int]:
        """世界に属すモデルごとの行数（分類表の道をたどって数える）。"""
        counts = {}
        for name, (kind, route) in CLASSIFICATION.items():
            if kind not in (LEAGUE, PLAYER, GAME):
                continue
            model = getattr(orm_models, name)
            counts[name] = model.objects.filter(**{f"{route}__isnull": False}).count()
        return counts

    def test_the_world_has_rows_in_every_table_before_deleting(self):
        """前提。消す対象が空では、削除の検査にならない。"""
        counts = self._rows_in_world()

        for name in ("League", "Team", "PlayerStint", "Player", "Game", "GameBattingLine", "GamePitchingLine"):
            self.assertGreater(counts[name], 0, name)
        for name in ("GamePlateAppearance", "GameRunnerAdvance", "GameInningScore", "GameFieldingLine"):
            self.assertGreater(counts[name], 0, name)
        self.assertGreater(counts["PennantPlayerRatings"], 0, "能力の行も世界に属す")

    def test_deleting_removes_every_row_of_the_world(self):
        build_pennant_world_service().delete_world(self.world_id)

        for name, count in self._rows_in_world().items():
            self.assertEqual(count, 0, f"{name} に世界の行が残っています")
        self.assertFalse(orm_models.PennantWorld.objects.filter(id=self.world_id).exists())

    def test_deleting_leaves_the_real_data_alone(self):
        before = _real_counts()
        real_lines = orm_models.GameBattingLine.objects.filter(game=self.real_game.id).count()

        build_pennant_world_service().delete_world(self.world_id)

        self.assertEqual(_real_counts(), before)
        self.assertEqual(orm_models.GameBattingLine.objects.filter(game=self.real_game.id).count(), real_lines)
        self.assertTrue(orm_models.Stadium.objects.filter(id=self.stadium.id).exists(), "球場は共有なので残る")
        self.assertEqual(self.service.get_standings(2026).rows[0].games_played, 1)

    def test_deleting_one_world_leaves_another_alone(self):
        other = build_pennant_world_service().create_world(
            name="別", owner_id=None, source_league_ids=[self.league.id], start_year=2030, seed=2
        )
        other_scope = WorldScope.pennant(other.world.id)

        build_pennant_world_service().delete_world(self.world_id)

        self.assertEqual(len(DjangoTeamRepository(other_scope).find_all_with_roster()), 2)
        self.assertGreater(orm_models.PlayerStint.objects.filter(team__league__world_id=other.world.id).count(), 0)

    def test_deleting_a_world_with_a_managed_team_works(self):
        created = build_pennant_world_service().create_world(
            name="受け持ち",
            owner_id=None,
            source_league_ids=[self.league.id],
            start_year=2030,
            seed=3,
            managed_source_team_id=self.team.id,
        )

        build_pennant_world_service().delete_world(created.world.id)

        self.assertFalse(orm_models.PennantWorld.objects.filter(id=created.world.id).exists())

    def test_deleting_a_missing_world_is_reported(self):
        with self.assertRaises(WorldNotFound):
            build_pennant_world_service().delete_world(999999)

    def test_the_pennant_marker_names_are_gone(self):
        build_pennant_world_service().delete_world(self.world_id)

        self.assertFalse(orm_models.League.objects.filter(name=PENNANT_LEAGUE).exists())
        self.assertFalse(orm_models.Team.objects.filter(name=PENNANT_TEAM).exists())


class PennantCommandsTest(BaseCase):
    def setUp(self):
        super().setUp()
        self.service.register_player(self.team.id, "選手", 1, "内野手")

    def _create(self, *args):
        out = StringIO()
        call_command("pennant_create", "--name", "コマンド", "--league", str(self.league.id), *args, stdout=out)
        return out.getvalue()

    def test_pennant_create_makes_a_world_and_reports_it(self):
        output = self._create("--year", "2031", "--seed", "99")

        world = orm_models.PennantWorld.objects.get()
        self.assertEqual((world.name, world.start_year, world.seed), ("コマンド", 2031, 99))
        self.assertIn("リーグ 1 / 球団 2 / 選手 1人", output)
        self.assertIn("所要", output)

    def test_pennant_create_picks_a_seed_when_not_given(self):
        self._create()

        self.assertIsNotNone(orm_models.PennantWorld.objects.get().seed)

    def test_pennant_create_records_the_owner_and_managed_team(self):
        User.objects.create_user("gm", password="x")

        self._create("--owner", "gm", "--managed-team", str(self.team.id))

        world = orm_models.PennantWorld.objects.get()
        self.assertEqual(world.owner.username, "gm")
        self.assertEqual(world.managed_team.name, self.team.name)

    def test_pennant_create_rejects_an_unknown_owner(self):
        with self.assertRaises(CommandError):
            self._create("--owner", "いない人")

        self.assertEqual(orm_models.PennantWorld.objects.count(), 0)

    def test_pennant_create_reports_a_domain_error_as_a_command_error(self):
        with self.assertRaises(CommandError) as raised:
            call_command("pennant_create", "--name", "x", "--league", "9999", stdout=StringIO())

        self.assertIn("リーグが見つかりません", str(raised.exception))

    def test_pennant_delete_removes_the_world(self):
        self._create()
        world = orm_models.PennantWorld.objects.get()

        call_command("pennant_delete", "--world", str(world.id), stdout=StringIO())

        self.assertEqual(orm_models.PennantWorld.objects.count(), 0)
        self.assertEqual(orm_models.Team.objects.count(), 2, "実データの球団は残る")

    def test_pennant_delete_reports_a_missing_world(self):
        with self.assertRaises(CommandError):
            call_command("pennant_delete", "--world", "999", stdout=StringIO())
