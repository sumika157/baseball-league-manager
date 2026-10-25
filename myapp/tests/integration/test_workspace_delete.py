"""ワークスペースの削除（`WorkspaceService.delete`・`DjangoWorkspaceRepository.delete`。W4 T2。#80）。

- B を消すと、B の行（分類表で B に属すとした行と、担当・招待の担当チームの中間表の行）がすべての表で0行になり、
  ほかの行（A・A の世界・在籍の無い選手・利用者）は1行も変わらない。B の行は、削除の順（実装）からではなく
  分類表（`test_world_isolation.CLASSIFICATION`）から数える（削除の順から1つ外すと、消し残しとして落ちる）。
- 削除の順: 分類表のどのモデル（世界を除く）と多対多の中間表も、削除の順のどれかに入っている。消す表を指す
  参照は、消す表から（子が先）か、SET_NULL として外す一覧にある。
- 消せないとき（何も消さない）: 世界が1つでもある（`NotDeletable`）・ほかのワークスペースにも在籍がある選手がいる
  （`InvalidWorkspace`）・無い id（`WorkspaceNotFound`）。
- ほかのワークスペースの球団が B の球場を本拠地にしている迷子の行は、Django の SET_NULL と同じく本拠地を外す。
- クエリの本数はチーム・試合の数によらない。削除の確認の件数（`WorkspaceManagementQuery.contents`）。
- 管理コマンド `workspace_delete`（確認のプロンプトと `--yes`）。
"""

from datetime import UTC, date, datetime
from io import StringIO
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.db import connection, models
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext

from myapp.domain.exceptions import InvalidWorkspace, NotDeletable, WorkspaceNotFound
from myapp.domain.pennant.world import WorldScope
from myapp.domain.services.rankings import Title
from myapp.domain.workspace import InvitedRole, workspace_deletion_blocker
from myapp.infrastructure import orm_models
from myapp.infrastructure.repositories import (
    _SCOPE_ROWS_CHILD_FIRST,
    _WORKSPACE_DETACHED_REFERENCES,
    _WORKSPACE_ROWS_CHILD_FIRST,
    DjangoWorldRepository,
    _workspace_incoming_references,
)
from myapp.infrastructure.scoping import players_in, world_condition

from ..helpers import (
    build_workspace_management_query,
    build_workspace_service,
    make_league,
    make_workspace,
    register_players,
)
from .test_workspace_isolation import B_YEAR, YEAR, WorkspaceCase
from .test_world_isolation import BOTH, CLASSIFICATION, DIRECT, GAME, LEAGUE, MEMBER, PLAYER, ROOT

MANAGERS = orm_models.Team.managers.through
INVITATION_TEAMS = orm_models.WorkspaceInvitation.teams.through
# 世界はワークスペースの削除では消さない（1つでもあれば消せない。2026-10-25 ユーザー決定 2）
NOT_DELETED = {"PennantWorld"}


def _all_rows() -> dict[str, dict[int, dict]]:
    """アプリの全表（多対多の中間表を含む）の行を、主キーごとに。"""
    return {
        model._meta.label: {row["id"]: row for row in model._default_manager.values()}
        for model in apps.get_app_config("myapp").get_models(include_auto_created=True)
    }


def _rows_of(workspace_id: int) -> dict[str, set[int]]:
    """そのワークスペースに属す行の主キー（分類表から数える。削除の実装の一覧は使わない）。"""
    scope = WorldScope.real(workspace_id)
    result: dict[str, set[int]] = {}
    for name, (kind, route) in CLASSIFICATION.items():
        model = apps.get_model("myapp", name)
        if kind == PLAYER and name == "Player":
            rows = players_in(scope)
        elif kind in (LEAGUE, PLAYER, GAME):
            assert route is not None
            rows = model._default_manager.filter(world_condition(route, scope))
        elif kind in (BOTH, DIRECT, MEMBER):
            # League（BOTH）は実データの道（`workspace`）。ペナントのリーグは世界とともに消える
            rows = model._default_manager.filter(workspace_id=workspace_id)
        else:
            assert kind == ROOT
            rows = model._default_manager.filter(id=workspace_id)
        result[model._meta.label] = set(rows.values_list("id", flat=True).distinct())
    result[MANAGERS._meta.label] = set(
        MANAGERS.objects.filter(team__league__workspace_id=workspace_id).values_list("id", flat=True)
    )
    result[INVITATION_TEAMS._meta.label] = set(
        INVITATION_TEAMS.objects.filter(workspaceinvitation__workspace_id=workspace_id).values_list("id", flat=True)
    )
    return result


def _deleted_models() -> list[type[models.Model]]:
    """削除の順（子から親へ）: 範囲の行 → 選手 → ワークスペースの行。"""
    return [
        *(model for model, _ in _SCOPE_ROWS_CHILD_FIRST),
        orm_models.Player,
        *(model for model, _ in _WORKSPACE_ROWS_CHILD_FIRST),
    ]


class DeletionOrderCoversEveryModelTest(SimpleTestCase):
    """モデルを足して削除の順に入れ忘れると落ちる。"""

    def test_every_classified_model_and_through_table_is_deleted(self):
        expected = {apps.get_model("myapp", name) for name in CLASSIFICATION} - {
            apps.get_model("myapp", name) for name in NOT_DELETED
        }
        expected |= {
            field.remote_field.through
            for model in apps.get_app_config("myapp").get_models()
            for field in model._meta.local_many_to_many
        }
        deleted = _deleted_models()
        self.assertEqual(len(deleted), len(set(deleted)), "同じモデルを2回消している")
        self.assertEqual(set(deleted), expected)

    def test_every_foreign_key_to_deleted_rows_is_detached_or_checked(self):
        """全アプリのモデルの外部キーのうち、消すモデルを指すものは、空にして外すか、消す前の検査の対象のどちらか。

        検査（`_workspace_incoming_references`）から漏れた外部キーは、迷子の参照があるとコミットで落ちる。
        """
        deleted = set(_deleted_models())
        detached = {(model, column) for model, column, _ in _WORKSPACE_DETACHED_REFERENCES}
        expected = {
            (model, field.name)
            for model in apps.get_models(include_auto_created=True)
            for field in model._meta.concrete_fields
            if field.is_relation and field.related_model in deleted
        }
        checked = {(model, field.name) for model, field in _workspace_incoming_references()}
        self.assertEqual(checked | detached, expected)
        self.assertEqual(checked & detached, set(), "空にする参照を検査で拒まない")
        # 試合の相手・明細の選手・中間表のチームが入っている（空にできない参照の代表）
        for model, name in (
            (orm_models.Game, "away_team"),
            (orm_models.GameBattingLine, "player"),
            (orm_models.GamePlateAppearance, "batter"),  # PROTECT
            (orm_models.PennantFixture, "visitor_team"),
            (orm_models.WorkspaceInvitation.teams.through, "team"),
        ):
            self.assertIn((model, name), checked)

    def test_references_to_deleted_rows_come_first_or_are_detached(self):
        """消す表を指す参照は、消す表から（指す側が先に消える）か、SET_NULL として先に外す一覧にある。"""
        order = {model: index for index, model in enumerate(_deleted_models())}
        detached = {(model, column) for model, column, _ in _WORKSPACE_DETACHED_REFERENCES}
        for model in apps.get_app_config("myapp").get_models(include_auto_created=True):
            for field in model._meta.concrete_fields:
                target = field.related_model if field.is_relation else None
                if target not in order or target is model:
                    continue
                with self.subTest(field=f"{model.__name__}.{field.name}"):
                    if field.remote_field.on_delete is models.SET_NULL:
                        self.assertIn((model, field.name), detached)
                    elif model.__name__ in NOT_DELETED:
                        # 世界が指す行は、世界が無いときだけ消す（`workspace_deletion_blocker`）
                        self.assertIs(field.remote_field.on_delete, models.PROTECT)
                    else:
                        self.assertIn(model, order, "消す表を指す、消さない表がある")
                        self.assertLess(order[model], order[target], "指す側を先に消す")


class WorkspaceDeletionCase(WorkspaceCase):
    """`WorkspaceCase` の B から世界を消し、メンバー・招待と、まだ行の無い表の行を足したもの（A にも同じく）。"""

    member: User

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        DjangoWorldRepository(cls.workspace_b.id).delete(cls.b_world_id)
        # A の球団が B の球場を指す迷子の行（`_plant_stray_stadiums`）は戻す（迷子の扱いは別のテスト）
        orm_models.Team.objects.filter(home_stadium=cls.b_stadium).exclude(
            id__in=[cls.b_team.id, cls.b_rival.id]
        ).update(home_stadium=None)
        cls.member = User.objects.create_user(username="b-member", password="x")
        for workspace, team in ((cls.workspace_b, cls.b_team), (cls.workspace, cls.team)):
            orm_models.WorkspaceEditor.objects.create(workspace=workspace, user=cls.member)
            team.managers.add(cls.b_owner)
            invitation = orm_models.WorkspaceInvitation.objects.create(
                workspace=workspace,
                token_digest=f"{workspace.id:064x}",
                role=InvitedRole.EDIT_TEAMS.value,
                expires_at=datetime(2099, 1, 1, tzinfo=UTC),
            )
            invitation.teams.add(team)
        cls._plant_more_b_rows()

    @classmethod
    def _plant_more_b_rows(cls) -> None:
        """B の実データに、まだ行の無い表（打席まわり・主将・編成の行・表彰・日本一）の行を置く。"""
        batter, runner = cls.b_batters[0], cls.b_batters[1]
        game_id = cls.b_game_ids[0]
        entry = orm_models.GamePlateAppearance.objects.create(
            game_id=game_id,
            sequence=1,
            inning=1,
            is_bottom=True,
            batter_id=batter,
            pitcher_id=cls.b_pitcher,
            batting_order=1,
            result="四球",
        )
        orm_models.GameRunnerAdvance.objects.create(
            plate_appearance=entry, runner_id=runner, from_base=0, to_base=1, reason="打撃"
        )
        orm_models.GameRunnerSubstitution.objects.create(
            plate_appearance=entry, base=1, leaving_runner_id=runner, entering_runner_id=cls.b_batters[2]
        )
        orm_models.GameFieldingError.objects.create(
            plate_appearance=entry, player_id=cls.b_batters[3], position="遊", kind="捕球"
        )
        orm_models.GameInningScore.objects.create(game_id=game_id, inning=1, is_home=True, runs=1)
        orm_models.Captaincy.objects.create(player_id=batter, team=cls.b_team, from_year=YEAR)
        plan = orm_models.PennantClubPlan.objects.get(team=cls.b_team)
        orm_models.PennantClubPlanEntry.objects.create(plan=plan, section="active", order=1, player_id=batter)
        orm_models.PennantAward.objects.create(
            league=cls.b_league, year=YEAR, title=Title.HOME_RUNS.value, player_id=batter, team=cls.b_team, value="1"
        )
        orm_models.PennantChampionship.objects.create(team=cls.b_team, year=B_YEAR)


class DeletingAWorkspaceTest(WorkspaceDeletionCase):
    def test_every_row_of_b_is_deleted_and_nothing_else_changes(self):
        b_rows = _rows_of(self.workspace_b.id)
        missing = sorted(label for label, ids in b_rows.items() if not ids and label.split(".")[1] not in NOT_DELETED)
        self.assertEqual(missing, [], "B に行の無い表がある（消えたことを確かめられない）")
        before = _all_rows()
        users = list(User.objects.order_by("id").values())

        build_workspace_service().delete(self.workspace_b.id)

        expected = {
            label: {pk: row for pk, row in rows.items() if pk not in b_rows.get(label, set())}
            for label, rows in before.items()
        }
        self.assertEqual(_all_rows(), expected)
        self.assertEqual(list(User.objects.order_by("id").values()), users, "利用者は消さない")

    def test_a_player_without_a_stint_is_kept(self):
        """在籍の無い選手はどの範囲にも属さないので消さない（B の試合の明細に出ていても）。"""
        loner = orm_models.Player.objects.create(name="在籍なし")
        orm_models.GameBattingLine.objects.create(game_id=self.b_game_ids[0], player=loner, team=self.b_team)

        build_workspace_service().delete(self.workspace_b.id)

        self.assertTrue(orm_models.Player.objects.filter(id=loner.id).exists())
        self.assertFalse(orm_models.GameBattingLine.objects.filter(player=loner).exists())

    def test_a_stray_home_stadium_in_another_workspace_is_detached(self):
        """A の球団が B の球場を本拠地にしている迷子の行は、本拠地を外す（Django の SET_NULL と同じ）。"""
        orm_models.Team.objects.filter(id=self.rival.id).update(home_stadium=self.b_stadium)
        b_rows = _rows_of(self.workspace_b.id)
        before = _all_rows()

        build_workspace_service().delete(self.workspace_b.id)

        after = _all_rows()
        team_label = orm_models.Team._meta.label
        self.assertIsNone(after[team_label][self.rival.id]["home_stadium_id"])
        before[team_label][self.rival.id]["home_stadium_id"] = None
        expected = {
            label: {pk: row for pk, row in rows.items() if pk not in b_rows.get(label, set())}
            for label, rows in before.items()
        }
        self.assertEqual(after, expected)

    def test_stray_references_from_a_world_of_another_workspace_are_detached(self):
        """A の世界の受け持つ球団・編成の抑えが B の行を指す迷子も外す（`_WORKSPACE_DETACHED_REFERENCES` の道）。"""
        world_team = orm_models.Team.objects.filter(league__world_id=self.a_world_id).order_by("id").first()
        assert world_team is not None
        orm_models.PennantWorld.objects.filter(id=self.a_world_id).update(managed_team=self.b_team)
        plan = orm_models.PennantClubPlan.objects.create(team=world_team, closer_id=self.b_pitcher)

        build_workspace_service().delete(self.workspace_b.id)

        self.assertIsNone(orm_models.PennantWorld.objects.get(id=self.a_world_id).managed_team_id)
        plan.refresh_from_db()
        self.assertIsNone(plan.closer_id)
        self.assertEqual(plan.team_id, world_team.id, "A の世界の編成の行そのものは残る")


class WorkspacesThatCannotBeDeletedTest(WorkspaceDeletionCase):
    def _assert_nothing_deleted(self, workspace_id: int, error: type[Exception]) -> None:
        before = _all_rows()
        with self.assertRaises(error) as raised:
            build_workspace_service().delete(workspace_id)
        self.assertEqual(_all_rows(), before)
        self.last_message = str(raised.exception)

    def test_a_workspace_with_a_world_is_not_deletable(self):
        self._assert_nothing_deleted(self.workspace.id, NotDeletable)
        self.assertEqual(self.last_message, workspace_deletion_blocker(1))

    def test_a_player_with_a_stint_in_another_workspace_stops_everything(self):
        orm_models.PlayerStint.objects.create(player_id=self.b_batters[0], team=self.team, from_year=YEAR)
        self._assert_nothing_deleted(self.workspace_b.id, InvalidWorkspace)
        self.assertIn(f"id={self.b_batters[0]}", self.last_message)

    def test_a_player_with_a_stint_in_a_world_of_another_workspace_stops_everything(self):
        world_team = orm_models.Team.objects.filter(league__world_id=self.a_world_id).first()
        assert world_team is not None
        orm_models.PlayerStint.objects.create(player_id=self.b_batters[0], team=world_team, from_year=YEAR)
        self._assert_nothing_deleted(self.workspace_b.id, InvalidWorkspace)

    def test_an_unknown_workspace(self):
        self._assert_nothing_deleted(10**9, WorkspaceNotFound)

    # --- 空にできない迷子の参照（2026-10-25 ユーザー決定: 消す前の検査で拒む）---

    def test_a_game_of_another_workspace_against_a_team_of_b_stops_everything(self):
        """A の試合の相手が B のチーム（直書き）。消すと A の試合が指す先を失うので、何も消さない。"""
        orm_models.Game.objects.filter(id=self.a_game_id).update(away_team=self.b_team)
        self._assert_nothing_deleted(self.workspace_b.id, InvalidWorkspace)
        self.assertIn(f"ほかのワークスペースの試合（id={self.a_game_id}）", self.last_message)
        self.assertIn("このワークスペースのチームを指している", self.last_message)
        self.assertIn("運営者に連絡してください", self.last_message)

    def test_a_batting_line_of_another_workspace_for_a_player_of_b_stops_everything(self):
        """A の試合の打撃成績が、B にしか在籍の無い選手を指す（直書き）。"""
        line = orm_models.GameBattingLine.objects.create(
            game_id=self.a_game_id, player_id=self.b_batters[0], team=self.team
        )
        self._assert_nothing_deleted(self.workspace_b.id, InvalidWorkspace)
        self.assertIn(f"打撃成績（id={line.id}）", self.last_message)
        self.assertIn("選手を指している", self.last_message)

    def test_an_invitation_of_another_workspace_for_a_team_of_b_stops_everything(self):
        """A の招待の担当チームに B のチーム（中間表の行。名前は招待のモデルのもの）。"""
        invitation = orm_models.WorkspaceInvitation.objects.get(workspace=self.workspace)
        invitation.teams.add(self.b_team)
        self._assert_nothing_deleted(self.workspace_b.id, InvalidWorkspace)
        # id は中間表の行ではなく招待の id（管理画面でその招待を開ける）
        self.assertIn(f"ほかのワークスペースのワークスペースへの招待（id={invitation.id}）", self.last_message)

    def test_a_fixture_of_a_world_of_another_workspace_against_a_team_of_b_stops_everything(self):
        """A の世界の日程の相手が B のチーム。ペナントのリーグは workspace が空でも、消さない行として拾う。"""
        world_team = orm_models.Team.objects.filter(league__world_id=self.a_world_id).order_by("id").first()
        assert world_team is not None
        fixture = orm_models.PennantFixture.objects.create(
            date=date(YEAR, 9, 1), home_team=world_team, visitor_team=self.b_team
        )
        self._assert_nothing_deleted(self.workspace_b.id, InvalidWorkspace)
        self.assertIn(f"ペナントの日程（id={fixture.id}）", self.last_message)


class DeletionQueryCountTest(WorkspaceDeletionCase):
    def _queries_to_delete(self, workspace_id: int) -> int:
        with CaptureQueriesContext(connection) as queries:
            build_workspace_service().delete(workspace_id)
        return len(queries)

    def test_the_number_of_queries_does_not_depend_on_teams_or_games(self):
        small = make_workspace(name="小さい")
        team = orm_models.Team.objects.create(league=make_league("小さいリーグ", workspace=small), name="1つだけ")
        register_players(team, [("1人だけ", 1, "投手")])

        self.assertEqual(self._queries_to_delete(small.id), self._queries_to_delete(self.workspace_b.id))


class WorkspaceContentsTest(WorkspaceDeletionCase):
    def test_counts_of_the_real_data(self):
        with self.assertNumQueries(5):
            contents = build_workspace_management_query(self.workspace_b.id).contents()
        self.assertEqual(contents.league_count, 1)
        self.assertEqual(contents.team_count, 2)
        self.assertEqual(contents.player_count, players_in(WorldScope.real(self.workspace_b.id)).count())
        self.assertEqual(contents.player_count, len(self.b_batters) + 1)
        self.assertEqual(contents.game_count, len(self.b_game_ids))
        self.assertEqual(contents.worlds, ())
        self.assertIsNone(contents.deletion_blocker)

    def test_worlds_left_block_the_deletion(self):
        contents = build_workspace_management_query(self.workspace.id).contents()
        self.assertEqual([world.world_id for world in contents.worlds], [self.a_world_id])
        self.assertEqual(contents.deletion_blocker, workspace_deletion_blocker(1))


class WorkspaceDeleteCommandTest(WorkspaceDeletionCase):
    def _call(self, *args: str, answer: str | None = None) -> str:
        out = StringIO()
        with patch("builtins.input", return_value=answer) as prompt:
            call_command("workspace_delete", *args, stdout=out)
        if answer is None:
            prompt.assert_not_called()
        return out.getvalue()

    def test_yes_skips_the_prompt(self):
        output = self._call("--workspace", self.workspace_b.key, "--yes")
        self.assertIn("消しました", output)
        self.assertFalse(orm_models.Workspace.objects.filter(id=self.workspace_b.id).exists())

    def test_answering_yes_deletes(self):
        output = self._call("--workspace", self.workspace_b.key, answer="yes")
        self.assertIn("試合 2", output)
        self.assertFalse(orm_models.Workspace.objects.filter(id=self.workspace_b.id).exists())

    def test_any_other_answer_deletes_nothing(self):
        before = _all_rows()
        with self.assertRaisesMessage(CommandError, "取りやめました"):
            self._call("--workspace", self.workspace_b.key, answer="y")
        self.assertEqual(_all_rows(), before)

    def test_no_input_deletes_nothing(self):
        """入力の無い実行（`exec -T` で `--yes` を付け忘れた）は取りやめる。"""
        before = _all_rows()
        with (
            patch("builtins.input", side_effect=EOFError),
            self.assertRaisesMessage(CommandError, "取りやめました"),
        ):
            call_command("workspace_delete", "--workspace", self.workspace_b.key, stdout=StringIO())
        self.assertEqual(_all_rows(), before)

    def test_a_stray_reference_stops_the_command(self):
        orm_models.Game.objects.filter(id=self.a_game_id).update(away_team=self.b_team)
        before = _all_rows()
        with self.assertRaisesMessage(CommandError, f"ほかのワークスペースの試合（id={self.a_game_id}）"):
            self._call("--workspace", self.workspace_b.key, "--yes")
        self.assertEqual(_all_rows(), before)

    def test_a_workspace_with_a_world_lists_the_worlds(self):
        with self.assertRaises(CommandError) as raised:
            self._call("--workspace", self.workspace.key, "--yes")
        self.assertIn(workspace_deletion_blocker(1) or "", str(raised.exception))
        self.assertIn(f"id={self.a_world_id}", str(raised.exception))
        self.assertTrue(orm_models.Workspace.objects.filter(id=self.workspace.id).exists())

    def test_the_workspace_is_required(self):
        with self.assertRaisesMessage(CommandError, "--workspace"):
            self._call("--yes")
        with self.assertRaises(CommandError):
            self._call("--workspace", "nosuchkey0", "--yes")
