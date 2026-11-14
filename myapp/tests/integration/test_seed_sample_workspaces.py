"""見本つきのワークスペースを大量に作る・消す管理コマンド `seed_sample_workspaces`（仕上げ T1。#74）。

計画（#74 の仕上げ T1）のとおり:

- `--count N` で `<prefix>-0001`… の N 人（ログインできないユーザー）と、1人につき1つの見本ありのワークスペース
  （既定は公開。`--private` で非公開）ができる。見本はそれぞれの範囲に6チーム・36試合で、key ごとに中身が違う。
- ほかのワークスペース（既定のワークスペース）の行は変わらない。
- 同じ prefix のユーザーがいれば、作らずに「先に --delete」と案内して止まる。
- `--delete` は、その prefix のユーザーが持つワークスペースとユーザーだけを消す（ほかの prefix・似た名前の
  ユーザー・ほかのワークスペースは残る）。確認で `yes` と打たなければ何も消さない。
- `DEBUG` が偽（本番）なら、作るのも消すのも拒む。
- 作成と削除は組み立て口のサービスだけを通す（ORM・リポジトリの実装を直接触らない）。
"""

from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings

from myapp.domain.sample.league import SAMPLE_GAME_COUNT, SAMPLE_TEAM_COUNT
from myapp.domain.workspace import Visibility
from myapp.infrastructure import orm_models

from ..helpers import default_workspace, make_league, make_pennant_world, real_scope
from .test_workspace_delete import _rows_of

COMMAND = "seed_sample_workspaces"


def _run(*args: str) -> str:
    out = StringIO()
    call_command(COMMAND, *args, stdout=out, stderr=StringIO())
    return out.getvalue()


def _owned(username: str) -> list[orm_models.Workspace]:
    return list(orm_models.Workspace.objects.filter(owner__username=username))


def _player_names(workspace: orm_models.Workspace) -> set[str]:
    return set(
        orm_models.Player.objects.filter(stints__team__league__workspace=workspace).values_list("name", flat=True)
    )


@override_settings(DEBUG=True)
class SeedSampleWorkspacesTest(TestCase):
    """`--count 2` で作っておく（見本ありの作成は1個あたり約1秒かかるので、クラスで1回）。"""

    @classmethod
    def setUpTestData(cls):
        cls.default = default_workspace()
        make_league("既定のリーグ", workspace=cls.default)
        cls.default_rows = _rows_of(cls.default.pk)
        cls.output = _run("--count", "2")
        cls.workspaces = [_owned("load-0001")[0], _owned("load-0002")[0]]

    def test_two_users_each_own_one_public_sample_workspace(self):
        for name in ("load-0001", "load-0002"):
            with self.subTest(user=name):
                user = User.objects.get(username=name)
                self.assertFalse(user.has_usable_password(), "計測用のユーザーはログインできない")
                [workspace] = _owned(name)
                self.assertEqual(workspace.visibility, Visibility.PUBLIC.value)
        self.assertFalse(User.objects.filter(username="load-0003").exists())

    def test_each_workspace_holds_its_own_sample_league(self):
        for workspace in self.workspaces:
            with self.subTest(workspace=workspace.key):
                scope = real_scope(workspace)
                teams = orm_models.Team.objects.filter(league__workspace_id=scope.workspace_id)
                self.assertEqual(teams.count(), SAMPLE_TEAM_COUNT)
                self.assertEqual(orm_models.Game.objects.filter(home_team__in=teams).count(), SAMPLE_GAME_COUNT)
        first, second = (_player_names(workspace) for workspace in self.workspaces)
        self.assertTrue(first)
        self.assertNotEqual(first, second, "見本は key ごとに違う中身になる")

    def test_the_default_workspace_is_untouched(self):
        self.assertEqual(_rows_of(self.default.pk), self.default_rows)

    def test_the_progress_and_the_time_per_workspace_are_reported(self):
        self.assertIn("2/2 個", self.output)
        self.assertIn("見本つきのワークスペースを 2 個作りました（公開）", self.output)
        self.assertIn("1個あたり", self.output)

    def test_the_same_prefix_twice_stops_without_creating(self):
        users, workspaces = User.objects.count(), orm_models.Workspace.objects.count()

        with self.assertRaisesMessage(CommandError, "先に --prefix load --delete で消してください"):
            _run("--count", "1")

        self.assertEqual((User.objects.count(), orm_models.Workspace.objects.count()), (users, workspaces))


@override_settings(DEBUG=True)
class DeleteSampleWorkspacesTest(TestCase):
    """prefix「load」の2つと「keep」の1つ（非公開）を作っておき、「load」だけを消す。"""

    @classmethod
    def setUpTestData(cls):
        cls.default = default_workspace()
        make_league("既定のリーグ", workspace=cls.default)
        # 名前が prefix で始まるが、計測用の形（<prefix>-<数字>）でないユーザーは消さない
        cls.lookalike = User.objects.create_user(username="load-admin", password="x")
        _run("--count", "2")
        _run("--count", "1", "--prefix", "keep", "--private")
        cls.kept = _owned("keep-0001")[0]

    def setUp(self):
        self.default_rows = _rows_of(self.default.pk)
        self.kept_rows = _rows_of(self.kept.pk)

    def test_private_creates_private_workspaces(self):
        self.assertEqual(self.kept.visibility, Visibility.PRIVATE.value)

    def test_delete_removes_only_that_prefix(self):
        doomed = [workspace.pk for name in ("load-0001", "load-0002") for workspace in _owned(name)]
        self.assertEqual(len(doomed), 2)

        output = _run("--delete", "--yes")

        self.assertIn("ワークスペース 2 個とユーザー 2 人を消しました", output)
        self.assertFalse(User.objects.filter(username__in=["load-0001", "load-0002"]).exists())
        self.assertFalse(orm_models.Workspace.objects.filter(pk__in=doomed).exists())
        self.assertFalse(orm_models.Team.objects.filter(league__workspace_id__in=doomed).exists())
        self.assertTrue(User.objects.filter(pk=self.lookalike.pk).exists())
        self.assertTrue(User.objects.filter(username="keep-0001").exists())
        self.assertEqual(_rows_of(self.kept.pk), self.kept_rows)
        self.assertEqual(_rows_of(self.default.pk), self.default_rows)

    def test_delete_asks_before_deleting(self):
        users, workspaces = User.objects.count(), orm_models.Workspace.objects.count()

        with (
            patch("builtins.input", return_value="no"),
            self.assertRaisesMessage(CommandError, "取りやめました（何も消していません）"),
        ):
            _run("--delete")

        self.assertEqual((User.objects.count(), orm_models.Workspace.objects.count()), (users, workspaces))

    def test_after_delete_the_prefix_can_be_created_again(self):
        _run("--delete", "--yes")

        _run("--count", "1")

        self.assertEqual(len(_owned("load-0001")), 1)

    def test_without_input_nothing_is_deleted(self):
        """入力の無い実行（`exec -T` など）は、確かめられないので取りやめる。"""
        users = User.objects.count()

        with (
            patch("builtins.input", side_effect=EOFError),
            self.assertRaisesMessage(CommandError, "取りやめました"),
        ):
            _run("--delete")

        self.assertEqual(User.objects.count(), users)

    def test_a_workspace_with_a_world_stops_before_deleting_the_users(self):
        """世界のあるワークスペースは消せない（`workspace_delete` と同じ検査）。ユーザーは消さずに止まる。"""
        [blocked] = _owned("load-0002")
        make_pennant_world("計測中の世界", workspace=blocked, seed=1, start_year=2026)

        with self.assertRaisesMessage(CommandError, "ユーザーは消していません"):
            _run("--delete", "--yes")

        self.assertTrue(orm_models.Workspace.objects.filter(pk=blocked.pk).exists())
        self.assertEqual(User.objects.filter(username__in=["load-0001", "load-0002"]).count(), 2)

    @override_settings(DEBUG=False)
    def test_delete_is_refused_when_not_debug(self):
        users, workspaces = User.objects.count(), orm_models.Workspace.objects.count()

        with self.assertRaisesMessage(CommandError, "DEBUG が偽の環境（本番）では動かしません"):
            _run("--delete", "--yes")

        self.assertEqual((User.objects.count(), orm_models.Workspace.objects.count()), (users, workspaces))


@override_settings(DEBUG=True)
class StopsWithoutCreatingTest(TestCase):
    def test_the_owner_limit_still_applies(self):
        """上限はサービスがそのまま検査する（素通りしない）。失敗した分のユーザーも残さない。"""
        with (
            override_settings(WORKSPACE_MAX_OWNED=0),
            self.assertRaisesMessage(CommandError, "load-0001 のワークスペースを作れませんでした"),
        ):
            _run("--count", "1")

        self.assertFalse(User.objects.filter(username="load-0001").exists())
        self.assertFalse(orm_models.Workspace.objects.exists())

    def test_wrong_arguments(self):
        for args in (
            ["--count", "0"],
            [],
            ["--count", "1", "--yes"],
            ["--delete", "--count", "1"],
            ["--delete", "--private"],
            ["--count", "1", "--prefix", "a-b"],
        ):
            with self.subTest(args=args), self.assertRaises(CommandError):
                _run(*args)

        self.assertFalse(User.objects.exists())


@override_settings(DEBUG=False)
class RefusedWhenNotDebugTest(TestCase):
    def test_nothing_is_created(self):
        users, workspaces = User.objects.count(), orm_models.Workspace.objects.count()

        with self.assertRaisesMessage(CommandError, "DEBUG が偽の環境（本番）では動かしません"):
            _run("--count", "2")

        self.assertEqual((User.objects.count(), orm_models.Workspace.objects.count()), (users, workspaces))


class UsesOnlyTheBuildersTest(SimpleTestCase):
    """作成・削除は組み立て口のサービスを通す（ORM・リポジトリ・参照クエリの実装を直接 import しない）。

    直接書くと、持てる数の上限・見本の照合・削除の検査（世界が残っていれば消さない）を素通りする。
    """

    def test_the_command_imports_no_infrastructure(self):
        root = Path(__file__).resolve().parents[3]
        source = (root / "myapp/management/commands/seed_sample_workspaces.py").read_text(encoding="utf-8")

        self.assertIn("build_workspace_service", source, "検査の対象を拾えていません")
        self.assertNotIn("infrastructure", source)
