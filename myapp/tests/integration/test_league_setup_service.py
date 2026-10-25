"""リーグ・チーム・球場の登録（`LeagueSetupService`・`SetupQuery`・リポジトリの削除と名前の一意。W3 T1。#79）。

- 往復: サービスで作った・変えた行を、参照クエリ（`build_setup_query`）で同じ値として読める。新しい行の表示順は末尾。
- 名前の一意はワークスペースの中だけ（B では同じ名前を作れる）。保存の前の確認をすり抜けた違反（同時の書き込み）は、
  DB の一意制約の `IntegrityError` を `DuplicateName` に訳す。
- 消せない行は `NotDeletable` で、どの表の行も変わらない。消せる行は、その1行（チームなら担当の割り当ても）
  だけが消える。
  球場は、ペナントの世界の球団が本拠地にしているだけでも消せない（2026-10-25 決定 1）。
- 所属リーグは、試合が1つも無いときだけ変えられる（2026-10-25 決定 2）。移す先の支配下選手の登録枠を
  超えるなら移せない（T3）。
- 一覧は行数によらず一定のクエリ数で読む。
"""

from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext

from myapp.domain.exceptions import (
    DomainError,
    DuplicateName,
    InvalidName,
    InvalidOrder,
    InvalidWorld,
    LeagueChangeLocked,
    LeagueNotFound,
    NotDeletable,
    RegisteredPlayerLimitExceeded,
    StadiumNotFound,
    TeamNotFound,
)
from myapp.domain.pennant.world import WorldScope
from myapp.domain.value_objects import ForeignPlayerRules, StadiumProfile
from myapp.infrastructure import orm_models
from myapp.infrastructure.queries import DjangoSetupQuery
from myapp.infrastructure.repositories import DjangoStadiumRepository, DjangoTeamRepository
from myapp.presentation.views import build_pennant_world_service

from ..helpers import (
    build_league_setup_service,
    build_setup_query,
    make_league,
    make_pennant_world,
    make_stadium,
    make_workspace,
    play_game,
    real_scope,
    register_players,
)
from .base import ROSTER_CHECK, BaseCase
from .test_workspace_isolation import snapshot_all_rows

NPB = ForeignPlayerRules.NPB
MANAGERS = orm_models.Team.managers.through


def _all_rows() -> dict[str, list[dict]]:
    """アプリの全表の行と、担当者の中間表（自動で作られる表は `snapshot_all_rows` に入らない）。"""
    managers: list[dict] = [dict(row) for row in MANAGERS.objects.order_by("pk").values()]
    return {**snapshot_all_rows(), "managers": managers}


def _removed(before: dict[str, list[dict]], after: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """前後で消えた行（表ごと）。足された・変わった行があれば落とす。"""
    removed = {}
    for table, rows in before.items():
        remaining = after[table]
        gone = [row for row in rows if row not in remaining]
        if gone:
            removed[table] = gone
        self_check = [row for row in remaining if row not in rows]
        assert not self_check, f"{table} に足された・変わった行があります: {self_check}"
    return removed


class SetupCase(BaseCase):
    """既定のワークスペース（A。`BaseCase` のリーグ・球場・チーム2つ）と、別のワークスペース B。"""

    workspace_b: orm_models.Workspace

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.workspace_b = make_workspace(name="B")

    def setUp(self):
        super().setUp()
        self.service = build_league_setup_service(real_scope())
        self.query = build_setup_query(real_scope())
        self.service_b = build_league_setup_service(real_scope(self.workspace_b))

    def assert_nothing_changes(self, call, error: type[DomainError]) -> DomainError:
        before = _all_rows()
        with self.assertRaises(error) as raised:
            call()
        self.assertEqual(_all_rows(), before, "弾かれた操作で行が変わった")
        return raised.exception


class RoundTripTest(SetupCase):
    def test_a_league_round_trips(self):
        league_id = self.service.create_league(
            name=" 新リーグ ", foreign_rules=ForeignPlayerRules(5, 4, None), registered_player_limit=None
        )
        row = self.query.league(league_id)
        self.assertEqual(row.name, "新リーグ", "前後の空白を除いて保存する")
        self.assertEqual(row.foreign_rules, ForeignPlayerRules(5, 4, None))
        self.assertIsNone(row.registered_player_limit)
        self.assertEqual(row.team_count, 0)
        self.assertIsNone(row.deletion_blocker)
        self.assertEqual(orm_models.League.objects.get(id=league_id).workspace_id, self.workspace.id)

        order = row.display_order
        self.service.update_league(league_id, name="改名リーグ", foreign_rules=NPB, registered_player_limit=65)
        row = self.query.league(league_id)
        self.assertEqual(
            (row.name, row.display_order, row.foreign_rules, row.registered_player_limit),
            ("改名リーグ", order, NPB, 65),
        )

    def test_a_stadium_round_trips(self):
        profile = StadiumProfile(city="大阪", capacity=36000, surface="人工芝", opened_year=1997, roof="ドーム")
        stadium_id = self.service.create_stadium(name="新球場", profile=profile)
        row = self.query.stadium(stadium_id)
        self.assertEqual((row.name, row.profile), ("新球場", profile))
        self.assertEqual((row.real_home_team_count, row.world_home_team_count), (0, 0))
        self.assertEqual(orm_models.Stadium.objects.get(id=stadium_id).workspace_id, self.workspace.id)

        self.service.update_stadium(stadium_id, name="改名球場", profile=StadiumProfile())
        row = self.query.stadium(stadium_id)
        self.assertEqual((row.name, row.profile), ("改名球場", StadiumProfile()))

    def test_a_team_round_trips(self):
        other = make_league("別リーグ")
        team_id = self.service.create_team(name="新チーム", league_id=self.league.id, home_stadium_id=None)
        row = self.query.team(team_id)
        self.assertEqual((row.name, row.league_id, row.league_name), ("新チーム", self.league.id, "テストリーグ"))
        self.assertEqual((row.home_stadium_id, row.home_stadium_name), (None, None))
        self.assertIsNone(row.deletion_blocker)
        self.assertIsNone(row.league_change_blocker)

        # 移したチームは、移す先のリーグの末尾に入る（W3 T8。表示順の欄は無い）
        orm_models.Team.objects.create(league=other, name="先客", display_order=3)
        self.service.update_team(team_id, name="改名チーム", league_id=other.id, home_stadium_id=self.stadium.id)
        row = self.query.team(team_id)
        self.assertEqual(
            (row.name, row.league_id, row.home_stadium_id, row.home_stadium_name, row.display_order),
            ("改名チーム", other.id, self.stadium.id, "テスト球場", 4),
        )

    def test_an_edit_without_moving_keeps_the_display_order(self):
        orm_models.Team.objects.filter(id=self.team.id).update(display_order=7)
        self.service.update_team(self.team.id, name="改名", league_id=self.league.id, home_stadium_id=None)
        self.assertEqual(self.query.team(self.team.id).display_order, 7)

    def test_renaming_a_team_keeps_its_roster(self):
        """チームの編集は集約ごと読み書きするが、選手・在籍は変えない。"""
        register_players(self.team, [("在籍選手", 10, "内野手")])
        before = {name: rows for name, rows in _all_rows().items() if name != "Team"}
        self.service.update_team(self.team.id, name="改名", league_id=self.league.id, home_stadium_id=self.stadium.id)
        after = {name: rows for name, rows in _all_rows().items() if name != "Team"}
        self.assertEqual(after, before)
        self.assertEqual(orm_models.Team.objects.get(id=self.team.id).name, "改名")

    def test_new_rows_go_to_the_end(self):
        """新しいリーグ・チームの表示順は今の最大＋1（チームはそのリーグの中で）。"""
        orm_models.League.objects.filter(id=self.league.id).update(display_order=3)
        orm_models.Team.objects.filter(id=self.rival.id).update(display_order=5)
        first = self.service.create_league(name="2番目", foreign_rules=NPB, registered_player_limit=70)
        second = self.service.create_league(name="3番目", foreign_rules=NPB, registered_player_limit=70)
        self.assertEqual([self.query.league(i).display_order for i in (first, second)], [4, 5])
        self.assertEqual([row.league_id for row in self.query.leagues()], [self.league.id, first, second])

        in_old = self.service.create_team(name="末尾", league_id=self.league.id, home_stadium_id=None)
        in_new = self.service.create_team(name="最初", league_id=first, home_stadium_id=None)
        self.assertEqual(self.query.team(in_old).display_order, 6)
        self.assertEqual(self.query.team(in_new).display_order, 0, "チームの無いリーグでは 0 から")

    def test_invalid_names_are_rejected_before_writing(self):
        for call in (
            lambda: self.service.create_league(name=" ", foreign_rules=NPB, registered_player_limit=70),
            lambda: self.service.create_team(name="", league_id=self.league.id, home_stadium_id=None),
            lambda: self.service.create_stadium(name="あ" * 101, profile=StadiumProfile()),
            lambda: self.service.update_team(self.team.id, name="", league_id=self.league.id, home_stadium_id=None),
        ):
            with self.subTest(call=call):
                self.assert_nothing_changes(call, InvalidName)


class NameUniquenessTest(SetupCase):
    """名前の一意は、そのワークスペースの実データの中だけ（推奨 3・4）。"""

    def test_a_duplicate_name_in_the_workspace_is_rejected(self):
        cases = {
            "リーグ": lambda: self.service.create_league(
                name="テストリーグ", foreign_rules=NPB, registered_player_limit=70
            ),
            "球場": lambda: self.service.create_stadium(name="テスト球場", profile=StadiumProfile()),
            "チーム": lambda: self.service.create_team(
                name="相手チーム", league_id=self.league.id, home_stadium_id=None
            ),
            "チームの改名": lambda: self.service.update_team(
                self.team.id, name="相手チーム", league_id=self.league.id, home_stadium_id=None
            ),
        }
        for kind, call in cases.items():
            with self.subTest(kind=kind):
                error = self.assert_nothing_changes(call, DuplicateName)
                self.assertIn("同じ名前", str(error))

    def test_a_team_in_another_league_of_the_workspace_is_also_a_duplicate(self):
        """チーム名はリーグではなくワークスペースの中で一意。"""
        other = make_league("別リーグ")
        self.assert_nothing_changes(
            lambda: self.service.create_team(name="テストチーム", league_id=other.id, home_stadium_id=None),
            DuplicateName,
        )

    def test_keeping_the_own_name_is_not_a_duplicate(self):
        self.service.update_team(self.team.id, name="テストチーム", league_id=self.league.id, home_stadium_id=None)
        self.service.update_league(self.league.id, name="テストリーグ", foreign_rules=NPB, registered_player_limit=70)
        self.service.update_stadium(self.stadium.id, name="テスト球場", profile=StadiumProfile(city="横浜"))
        self.assertEqual(self.query.team(self.team.id).name, "テストチーム")
        self.assertEqual(self.query.stadium(self.stadium.id).profile.city, "横浜")

    def test_another_workspace_can_use_the_same_names(self):
        league_id = self.service_b.create_league(name="テストリーグ", foreign_rules=NPB, registered_player_limit=70)
        stadium_id = self.service_b.create_stadium(name="テスト球場", profile=StadiumProfile())
        team_id = self.service_b.create_team(name="テストチーム", league_id=league_id, home_stadium_id=stadium_id)
        query_b = build_setup_query(real_scope(self.workspace_b))
        self.assertEqual([row.name for row in query_b.leagues()], ["テストリーグ"])
        self.assertEqual(query_b.team(team_id).home_stadium_name, "テスト球場")

    def test_the_names_of_the_pennant_worlds_do_not_count(self):
        """世界のリーグ・球団は分岐の写しで同じ名前を持つ。実データの一意の検査に数えない。"""
        world = make_pennant_world("世界", seed=1, start_year=2026)
        world_league = make_league("世界のリーグ", world=world)
        orm_models.Team.objects.create(league=world_league, name="世界の球団")
        self.service.create_league(name="世界のリーグ", foreign_rules=NPB, registered_player_limit=70)
        self.service.create_team(name="世界の球団", league_id=self.league.id, home_stadium_id=None)

    def test_an_old_name_with_spaces_does_not_turn_an_unrelated_save_into_a_rename(self):
        """前後に空白のある古い行（検査の前に書かれた行）は、名前を変えない保存では重複の検査にかけない。

        集約は空白を除いて読むので、比べるときも除く（除いた名前が別のチームと同じでも、ロスターの保存を落とさない）。
        """
        orm_models.Team.objects.filter(id=self.rival.id).update(name=" テストチーム ")
        repo = DjangoTeamRepository(real_scope())
        rival = repo.find_by_id(self.rival.id)
        rival.reorder(9)
        repo.save(rival)
        self.assertEqual(orm_models.Team.objects.get(id=self.rival.id).display_order, 9)

    def test_a_fork_of_teams_with_the_same_name_is_rejected_before_writing(self):
        """チーム名は世界の中でも一意。実データに同名のチームが残っていたら（管理画面・投入コマンドは検査しない）、
        写し始める前に `InvalidWorld` で弾き、世界を作らない。"""
        other = make_league("別リーグ")
        orm_models.Team.objects.create(league=other, name="同名")
        orm_models.Team.objects.create(league=self.league, name="同名")
        before = _all_rows()
        with patch(ROSTER_CHECK), self.assertRaisesRegex(InvalidWorld, "同じ名前のチーム"):
            build_pennant_world_service(real_scope()).create_world(
                name="世界", owner_id=None, source_league_ids=[self.league.id, other.id], start_year=2026, seed=1
            )
        self.assertEqual(_all_rows(), before)

    def test_a_race_past_the_check_is_translated_from_the_integrity_error(self):
        """保存の前の確認をすり抜けた違反（同時の書き込み）も、500 ではなく `DuplicateName` になり、何も書かない。"""
        with patch("myapp.infrastructure.repositories._name_taken", return_value=False):
            for kind, call in {
                "リーグ": lambda: self.service.create_league(
                    name="テストリーグ", foreign_rules=NPB, registered_player_limit=70
                ),
                "リーグの改名": lambda: self.service.update_league(
                    self.league.id, name="Bと同名", foreign_rules=NPB, registered_player_limit=70
                ),
                "球場": lambda: self.service.create_stadium(name="テスト球場", profile=StadiumProfile()),
            }.items():
                if kind == "リーグの改名":
                    make_league("Bと同名")
                with self.subTest(kind=kind):
                    self.assert_nothing_changes(call, DuplicateName)
            # 外側のトランザクションは使い続けられる（違反をセーブポイントの中に閉じ込めている）
            self.assertTrue(orm_models.League.objects.filter(id=self.league.id).exists())


class DeletionTest(SetupCase):
    def test_an_empty_row_is_deleted_alone(self):
        """消すのはその1行だけ（ほかのどの表の行も変えない）。"""
        league_id = self.service.create_league(name="空のリーグ", foreign_rules=NPB, registered_player_limit=70)
        stadium_id = self.service.create_stadium(name="空の球場", profile=StadiumProfile())
        for kind, delete, model, row_id in (
            ("リーグ", self.service.delete_league, "League", league_id),
            ("球場", self.service.delete_stadium, "Stadium", stadium_id),
        ):
            with self.subTest(kind=kind):
                before = _all_rows()
                delete(row_id)
                removed = _removed(before, _all_rows())
                self.assertEqual(list(removed), [model])
                self.assertEqual([row["id"] for row in removed[model]], [row_id])

    def test_a_team_is_deleted_with_its_manager_assignments_only(self):
        team_id = self.service.create_team(
            name="空のチーム", league_id=self.league.id, home_stadium_id=self.stadium.id
        )
        manager = User.objects.create_user("manager", password="x")
        orm_models.Team.objects.get(id=team_id).managers.add(manager)
        self.team.managers.add(manager)
        self.assertEqual(self.query.team(team_id).manager_count, 1)

        before = _all_rows()
        self.service.delete_team(team_id)
        removed = _removed(before, _all_rows())
        self.assertEqual(sorted(removed), ["Team", "managers"])
        self.assertEqual([row["id"] for row in removed["Team"]], [team_id])
        self.assertEqual([row["team_id"] for row in removed["managers"]], [team_id], "ほかのチームの担当は残る")

    def test_a_league_with_teams_cannot_be_deleted(self):
        error = self.assert_nothing_changes(lambda: self.service.delete_league(self.league.id), NotDeletable)
        self.assertEqual(str(error), self.query.league(self.league.id).deletion_blocker)

    def test_a_team_with_a_stint_a_captaincy_or_a_game_cannot_be_deleted(self):
        with_stint = self.service.create_team(name="在籍あり", league_id=self.league.id, home_stadium_id=None)
        with_game = self.service.create_team(name="試合あり", league_id=self.league.id, home_stadium_id=None)
        with_captain = self.service.create_team(name="主将あり", league_id=self.league.id, home_stadium_id=None)
        register_players(orm_models.Team.objects.get(id=with_stint), [("選手", 1, "内野手")])
        play_game(orm_models.Team.objects.get(id=self.rival.id), orm_models.Team.objects.get(id=with_game))
        (captain,) = register_players(orm_models.Team.objects.get(id=with_captain), [("主将", 2, "内野手")])
        stint = orm_models.PlayerStint.objects.get(player_id=captain)
        orm_models.Captaincy.objects.create(player_id=captain, team_id=with_captain, from_year=stint.from_year)
        # 主将の在任だけを残す（在籍を消すと在籍の数で止まらない）
        orm_models.PlayerStint.objects.filter(id=stint.id).delete()

        for team_id, counts in ((with_stint, (1, 0, 0)), (with_game, (0, 0, 1)), (with_captain, (0, 1, 0))):
            row = self.query.team(team_id)
            with self.subTest(team=row.name):
                self.assertEqual((row.stint_count, row.captaincy_count, row.game_count), counts)
                error = self.assert_nothing_changes(
                    lambda team_id=team_id: self.service.delete_team(team_id), NotDeletable
                )
                self.assertEqual(str(error), row.deletion_blocker)

    def test_a_home_stadium_cannot_be_deleted(self):
        error = self.assert_nothing_changes(lambda: self.service.delete_stadium(self.stadium.id), NotDeletable)
        self.assertEqual(str(error), self.query.stadium(self.stadium.id).deletion_blocker)

    def test_a_stadium_a_pennant_team_calls_home_cannot_be_deleted(self):
        """世界は球場を共有する。実データのチームが使っていなくても、世界の球団の本拠地なら消せない（決定 1）。"""
        stadium = make_stadium("世界だけの本拠地")
        world = make_pennant_world("世界", seed=1, start_year=2026)
        orm_models.Team.objects.create(
            league=make_league("世界のリーグ", world=world), name="球団", home_stadium=stadium
        )
        row = self.query.stadium(stadium.id)
        self.assertEqual((row.real_home_team_count, row.world_home_team_count), (0, 1))
        error = self.assert_nothing_changes(lambda: self.service.delete_stadium(stadium.id), NotDeletable)
        self.assertIn("ペナントの世界の1球団", str(error))

    def test_the_teams_of_another_workspaces_worlds_are_not_counted(self):
        """B の世界の球団は A の球場の数に入らない（ORM の直書きで A の球場を指していても）。"""
        stadium = make_stadium("A の空の球場")
        world_b = make_pennant_world("B の世界", workspace=self.workspace_b, seed=1, start_year=2026)
        stray = orm_models.Team.objects.create(league=make_league("B のリーグ", world=world_b), name="B の球団")
        orm_models.Team.objects.filter(id=stray.id).update(home_stadium=stadium)
        self.assertEqual(self.query.stadium(stadium.id).world_home_team_count, 0)


class LeagueChangeTest(SetupCase):
    """所属リーグは試合が1つも無いときだけ変えられる（決定 2）。"""

    def setUp(self):
        super().setUp()
        self.other = make_league("移籍先")

    def test_a_team_without_games_can_move(self):
        self.service.update_team(self.team.id, name="テストチーム", league_id=self.other.id, home_stadium_id=None)
        self.assertEqual(orm_models.Team.objects.get(id=self.team.id).league_id, self.other.id)

    def test_a_team_with_a_game_cannot_move_and_nothing_is_written(self):
        play_game(self.team, self.rival)
        self.assertIsNotNone(self.query.team(self.team.id).league_change_blocker)
        error = self.assert_nothing_changes(
            lambda: self.service.update_team(
                self.team.id, name="改名も書かない", league_id=self.other.id, home_stadium_id=None
            ),
            LeagueChangeLocked,
        )
        self.assertEqual(str(error), self.query.team(self.team.id).league_change_blocker)

    def test_a_team_over_the_new_leagues_registered_limit_cannot_move(self):
        """移す先のリーグの支配下選手の登録枠を超えるなら移せず、何も書かない（W3 T3）。"""
        register_players(self.team, [("一郎", "1", "投手"), ("二郎", "2", "捕手")])
        small = make_league("小さいリーグ", registered_player_limit=1)
        self.assert_nothing_changes(
            lambda: self.service.update_team(
                self.team.id, name="改名も書かない", league_id=small.id, home_stadium_id=None
            ),
            RegisteredPlayerLimitExceeded,
        )

    def test_a_team_with_games_can_still_be_edited_in_its_league(self):
        play_game(self.team, self.rival)
        self.service.update_team(self.team.id, name="改名", league_id=self.league.id, home_stadium_id=None)
        self.assertEqual(orm_models.Team.objects.get(id=self.team.id).name, "改名")


class NameCheckQueriesTest(SetupCase):
    """チーム名の重複は、新しいチームと改名のときだけ1回引く。所属リーグを変えたときだけ試合を1回数える。

    今の名前・リーグは、チームの存在を確かめるクエリで一緒に読むので、どちらも変えない保存（シーズンを締める処理など、
    ロスターだけを書く保存）のクエリ数は W3 の前と同じ。
    """

    def _queries_to_save(self, change) -> int:
        repo = DjangoTeamRepository(real_scope())
        team = repo.find_by_id(self.team.id)
        change(team)
        with CaptureQueriesContext(connection) as captured:
            repo.save(team)
        return len(captured)

    def test_only_a_rename_or_a_move_costs_a_query(self):
        other = make_league("移籍先")
        unchanged = self._queries_to_save(lambda team: team.reorder(5))
        renamed = self._queries_to_save(lambda team: team.rename("改名"))
        moved = self._queries_to_save(lambda team: setattr(team, "league_id", other.id))
        self.assertEqual(renamed, unchanged + 1, "改名だけが同名のチームを1回引く")
        self.assertEqual(moved, unchanged + 1, "所属リーグの変更だけが試合を1回数える")


class OtherWorkspaceIdsTest(SetupCase):
    """B のリーグ・チーム・球場の id を A のサービスに混ぜても、見つからない扱いで何も変わらない。"""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.b_league = make_league("B のリーグ", workspace=cls.workspace_b)
        cls.b_stadium = make_stadium("B の球場", workspace=cls.workspace_b)
        cls.b_team = orm_models.Team.objects.create(league=cls.b_league, name="B のチーム")
        cls.b_empty_league = make_league("B の空のリーグ", workspace=cls.workspace_b)

    def test_writes_with_bs_ids_raise_and_change_nothing(self):
        s = self.service
        cases = [
            (lambda: s.delete_league(self.b_empty_league.id), LeagueNotFound),
            (lambda: s.delete_team(self.b_team.id), TeamNotFound),
            (lambda: s.delete_stadium(self.b_stadium.id), StadiumNotFound),
            (
                lambda: s.update_league(self.b_league.id, name="x", foreign_rules=NPB, registered_player_limit=70),
                LeagueNotFound,
            ),
            (lambda: s.update_stadium(self.b_stadium.id, name="x", profile=StadiumProfile()), StadiumNotFound),
            (
                lambda: s.update_team(self.b_team.id, name="x", league_id=self.b_league.id, home_stadium_id=None),
                TeamNotFound,
            ),
            (lambda: s.create_team(name="x", league_id=self.b_league.id, home_stadium_id=None), LeagueNotFound),
            (
                lambda: s.create_team(name="x", league_id=self.league.id, home_stadium_id=self.b_stadium.id),
                StadiumNotFound,
            ),
            (
                lambda: s.update_team(self.team.id, name="x", league_id=self.b_league.id, home_stadium_id=None),
                LeagueNotFound,
            ),
        ]
        for call, error in cases:
            with self.subTest(error=error.__name__):
                self.assert_nothing_changes(call, error)

    def test_the_query_reads_only_its_workspace(self):
        self.assertEqual([row.league_id for row in self.query.leagues()], [self.league.id])
        self.assertEqual({row.team_id for row in self.query.teams()}, {self.team.id, self.rival.id})
        self.assertEqual([row.stadium_id for row in self.query.stadiums()], [self.stadium.id])
        for read, row_id, error in (
            (self.query.league, self.b_league.id, LeagueNotFound),
            (self.query.team, self.b_team.id, TeamNotFound),
            (self.query.stadium, self.b_stadium.id, StadiumNotFound),
        ):
            with self.subTest(read=read.__name__), self.assertRaises(error):
                read(row_id)


class RealScopeOnlyTest(SetupCase):
    def test_the_stadium_repository_and_the_query_reject_a_pennant_scope(self):
        world = make_pennant_world("世界", seed=1, start_year=2026)
        scope = WorldScope.pennant(self.workspace.id, world.id)
        for build in (DjangoStadiumRepository, DjangoSetupQuery, build_league_setup_service, build_setup_query):
            with self.subTest(build=build.__name__), self.assertRaises(InvalidWorld):
                build(scope)


class ListQueryCountTest(SetupCase):
    """一覧は行数によらず、それぞれクエリ1本（数は相関した副問い合わせで数える）。"""

    def _counts(self) -> tuple[int, int, int]:
        counts = []
        for read in (self.query.leagues, self.query.teams, self.query.stadiums):
            with CaptureQueriesContext(connection) as captured:
                read()
            counts.append(len(captured))
        return (counts[0], counts[1], counts[2])

    def test_the_lists_take_one_query_whatever_the_rows(self):
        self.assertEqual(self._counts(), (1, 1, 1))
        for number in range(3):
            league = make_league(f"追加リーグ{number}")
            stadium = make_stadium(f"追加球場{number}")
            team = orm_models.Team.objects.create(league=league, name=f"追加チーム{number}", home_stadium=stadium)
            register_players(team, [(f"追加選手{number}", 1, "内野手")])
            play_game(team, self.rival)
        self.assertEqual(self._counts(), (1, 1, 1))
        # 数が掛け合わさらない（在籍1・試合1のチームの試合数が 1 のまま）
        row = next(row for row in self.query.teams() if row.name == "追加チーム0")
        self.assertEqual((row.stint_count, row.game_count, row.captaincy_count), (1, 1, 0))


class ReorderTest(SetupCase):
    """表示順の並べ替え（W3 T8）。識別子の並びで受け取り、範囲の行の集まりとちょうど一致するときだけ書く。"""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.second = make_league("2番目のリーグ")
        cls.third = make_league("3番目のリーグ")
        cls.third_team = orm_models.Team.objects.create(league=cls.league, name="3番目のチーム")
        cls.elsewhere = orm_models.Team.objects.create(league=cls.second, name="別リーグのチーム")
        cls.b_league = make_league("B のリーグ", workspace=cls.workspace_b)
        cls.b_team = orm_models.Team.objects.create(league=cls.b_league, name="B のチーム")

    def _team_ids(self, league_id: int) -> list[int]:
        return [row.team_id for row in self.query.teams() if row.league_id == league_id]

    def test_leagues_round_trip(self):
        order = [self.third.id, self.league.id, self.second.id]
        self.service.reorder_leagues(order)
        self.assertEqual([row.league_id for row in self.query.leagues()], order)
        self.assertEqual([row.display_order for row in self.query.leagues()], [0, 1, 2])

    def test_teams_round_trip_inside_the_league(self):
        elsewhere = orm_models.Team.objects.filter(id=self.elsewhere.id).values().get()
        order = [self.third_team.id, self.rival.id, self.team.id]
        self.service.reorder_teams(self.league.id, order)
        self.assertEqual(self._team_ids(self.league.id), order)
        self.assertEqual([self.query.team(i).display_order for i in order], [0, 1, 2])
        self.assertEqual(orm_models.Team.objects.filter(id=self.elsewhere.id).values().get(), elsewhere)

    def test_only_the_display_order_is_written(self):
        """表示順のほか（名前・リーグ・本拠地・在籍など）はどの表も変わらない。"""
        register_players(self.team, [("在籍選手", 10, "内野手")])

        def without_orders(rows: dict[str, list[dict]]) -> dict[str, list[dict]]:
            return {
                table: [{k: v for k, v in row.items() if k != "display_order"} for row in table_rows]
                for table, table_rows in rows.items()
            }

        before = _all_rows()
        self.service.reorder_teams(self.league.id, [self.rival.id, self.third_team.id, self.team.id])
        self.service.reorder_leagues([self.second.id, self.third.id, self.league.id])
        after = _all_rows()
        self.assertNotEqual(after, before)
        self.assertEqual(without_orders(after), without_orders(before))

    def test_an_order_that_does_not_match_the_rows_writes_nothing(self):
        """欠け・余り・重複・ほかのリーグやワークスペースの id・空は、どれも `InvalidOrder` で何も書かない。"""
        leagues = [self.league.id, self.second.id, self.third.id]
        teams = [self.team.id, self.rival.id, self.third_team.id]
        cases = {
            "リーグの欠け": lambda: self.service.reorder_leagues(leagues[:2]),
            "リーグの余り（B のリーグ）": lambda: self.service.reorder_leagues([*leagues, self.b_league.id]),
            "リーグの入れ替え（B のリーグ）": lambda: self.service.reorder_leagues([*leagues[:2], self.b_league.id]),
            "リーグの重複": lambda: self.service.reorder_leagues([*leagues, leagues[0]]),
            "リーグの空": lambda: self.service.reorder_leagues([]),
            "チームの欠け": lambda: self.service.reorder_teams(self.league.id, teams[1:]),
            "チームの余り（別リーグ）": lambda: self.service.reorder_teams(
                self.league.id, [*teams, self.elsewhere.id]
            ),
            "チームの入れ替え（B のチーム）": lambda: self.service.reorder_teams(
                self.league.id, [*teams[:2], self.b_team.id]
            ),
            "チームの重複": lambda: self.service.reorder_teams(self.league.id, [teams[0], *teams]),
            "知らない id": lambda: self.service.reorder_teams(self.league.id, [*teams[:2], 999_999]),
        }
        for label, call in cases.items():
            with self.subTest(label):
                self.assert_nothing_changes(call, InvalidOrder)

    def test_the_teams_of_another_workspaces_league_are_not_found(self):
        self.assert_nothing_changes(
            lambda: self.service.reorder_teams(self.b_league.id, [self.b_team.id]), LeagueNotFound
        )

    def test_the_repository_rejects_rows_outside_the_scope(self):
        """`save_display_orders` も範囲の外の行があれば何も書かない（サービスを通らない呼び方の守り）。"""
        b_teams = DjangoTeamRepository(real_scope(self.workspace_b)).find_all()
        repo = DjangoTeamRepository(real_scope())
        self.assert_nothing_changes(lambda: repo.save_display_orders(b_teams), TeamNotFound)

    def _queries(self, call) -> int:
        with CaptureQueriesContext(connection) as captured:
            call()
        return len(captured)

    def test_the_query_count_does_not_grow_with_the_rows(self):
        def reorder_all():
            leagues = [row.league_id for row in self.query.leagues()][::-1]
            teams = self._team_ids(self.league.id)[::-1]
            return lambda: (self.service.reorder_leagues(leagues), self.service.reorder_teams(self.league.id, teams))

        few = self._queries(reorder_all())
        for number in range(5):
            make_league(f"追加リーグ{number}")
            team = orm_models.Team.objects.create(league=self.league, name=f"追加チーム{number}")
            register_players(team, [(f"追加選手{number}", 1, "内野手")])
        self.assertEqual(self._queries(reorder_all()), few)
