"""データ投入・補正コマンドが、ペナントの世界と、ほかのワークスペースに触れないこと。

投入コマンドは ORM へ直接書く。対象を絞らないと、`--replace` がペナントの試合を消し、
ペナントの選手が実データの試合に出場し、ペナントの球団にも選手が足される。
エラーにならずデータだけがずれるので、世界を作ってから流して、世界の側が変わらないことを見る。

ワークスペース（W1b T5）: 実データを読み書きするコマンドは `--workspace <key>` を必須で受け取る。
B を指定して流しても A の行が変わらないこと、省いた・無い key は日本語のエラーで何も書かないことを見る。
`--world` を取るコマンドは、世界の行からワークスペースを導く。
"""

from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from myapp.infrastructure import orm_models
from myapp.presentation.views import build_pennant_world_service

from ..helpers import (
    DEFAULT_WORKSPACE_KEY,
    default_workspace,
    make_league,
    make_stadium,
    make_workspace,
    play_game,
    real_scope,
    register_players,
)
from .world_case import YEAR, WorldCase


def _pennant_players() -> list[tuple]:
    """世界の選手の中身。分岐の直後から変わってはならない。"""
    rows = orm_models.Player.objects.filter(stints__team__league__world__isnull=False).order_by("id")
    return list(
        rows.values_list("id", "name", "name_kana", "back_name", "high_school", "university", "corporate_team")
    )


def _pennant_stints() -> list[tuple]:
    rows = orm_models.PlayerStint.objects.filter(team__league__world__isnull=False).order_by("id")
    return list(rows.values_list("id", "player_id", "team_id", "number", "from_year", "to_year"))


def _pennant_games() -> list[int]:
    return sorted(orm_models.Game.objects.filter(home_team__league__world__isnull=False).values_list("id", flat=True))


class SeedScopeCase(WorldCase):
    def setUp(self):
        super().setUp()
        # 試合の投入に要る人数（レギュラー9人と控え、先発ローテーションと救援）まで増やす
        for team, first in ((self.team, 30), (self.rival, 30)):
            labels = [
                (label, index) for label, count in (("捕手", 2), ("外野手", 5), ("投手", 8)) for index in range(count)
            ]
            register_players(
                team,
                [
                    (f"{team.name}追加{label}{index}", number, label)
                    for number, (label, index) in enumerate(labels, start=first)
                ],
            )
        # 読みを付与するモードが拾う名前（実在の選手の読みを持つ名前）の選手を、世界に1人置く。
        # 絞り込みが漏れていれば、この選手によみがなが付く
        orm_models.Player.objects.filter(id=self.pennant_players(self.pennant_team)[0]).update(name="藤井健吾")
        self.before_players = _pennant_players()
        self.before_stints = _pennant_stints()
        self.before_games = _pennant_games()

    def _assert_world_untouched(self):
        self.assertEqual(_pennant_players(), self.before_players)
        self.assertEqual(_pennant_stints(), self.before_stints)
        self.assertEqual(_pennant_games(), self.before_games)


class SeedVirtualGamesScopeTest(SeedScopeCase):
    def _seed(self, year):
        call_command(
            "seed_virtual_games", workspace=DEFAULT_WORKSPACE_KEY, year=year, games_per_pair=2, seed=1, verbosity=0
        )

    def test_replace_does_not_delete_the_pennant_games(self):
        """`--replace` は実データのその年の試合だけを消す。"""
        self.assertIn(self.pennant_game_id, self.before_games)
        old_real_game = self.real_game.id

        call_command(
            "seed_virtual_games",
            workspace=DEFAULT_WORKSPACE_KEY,
            year=YEAR,
            games_per_pair=2,
            seed=1,
            verbosity=0,
            replace=True,
        )

        self.assertEqual(_pennant_games(), self.before_games, "ペナントの試合は消えない")
        self.assertTrue(orm_models.GamePlateAppearance.objects.filter(game_id=self.pennant_game_id).exists())
        self.assertFalse(orm_models.Game.objects.filter(id=old_real_game).exists(), "実データの試合は作り直される")
        self.assertEqual(orm_models.Game.objects.filter(home_team__league__world__isnull=True, year=YEAR).count(), 2)

    def test_a_year_with_only_pennant_games_is_not_treated_as_existing(self):
        """実データにその年の試合が無ければ、ペナントの試合があっても `--replace` なしで投入できる。"""
        play_game(
            self.pennant_team,
            self.pennant_rival,
            year=2029,
            scope=self.scope,
        )

        self._seed(2029)

        real = orm_models.Game.objects.filter(home_team__league__world__isnull=True, year=2029)
        self.assertEqual(real.count(), 2)
        self.assertEqual(orm_models.Game.objects.filter(home_team__league__world__isnull=False, year=2029).count(), 1)

    def test_pennant_players_never_play_in_the_real_games(self):
        call_command(
            "seed_virtual_games",
            workspace=DEFAULT_WORKSPACE_KEY,
            year=YEAR,
            games_per_pair=2,
            seed=1,
            verbosity=0,
            replace=True,
        )

        real_games = orm_models.Game.objects.filter(home_team__league__world__isnull=True)
        strangers = orm_models.GamePlateAppearance.objects.filter(game__in=real_games).filter(
            batter__stints__team__league__world__isnull=False
        )
        self.assertFalse(strangers.exists(), "ペナントの選手が実データの試合に出場しています")
        lines = orm_models.GameBattingLine.objects.filter(game__in=real_games).filter(
            player__stints__team__league__world__isnull=False
        )
        self.assertFalse(lines.exists())

    def test_the_world_is_untouched(self):
        call_command(
            "seed_virtual_games",
            workspace=DEFAULT_WORKSPACE_KEY,
            year=YEAR,
            games_per_pair=2,
            seed=1,
            verbosity=0,
            replace=True,
        )

        self._assert_world_untouched()


class SeedVirtualPlayersScopeTest(SeedScopeCase):
    def _run(self, *args):
        call_command(
            "seed_virtual_players", "--workspace", DEFAULT_WORKSPACE_KEY, "--seed", "1", *args, stdout=StringIO()
        )

    def test_new_players_are_added_to_the_real_teams_only(self):
        real_before = orm_models.PlayerStint.objects.filter(team__league__world__isnull=True).count()

        self._run()

        self.assertGreater(
            orm_models.PlayerStint.objects.filter(team__league__world__isnull=True).count(), real_before
        )
        self._assert_world_untouched()

    def test_maintenance_modes_leave_the_world_alone(self):
        for option in ("--refresh-schools", "--rename-existing", "--assign-readings"):
            with self.subTest(option=option):
                self._run(option)

                self._assert_world_untouched()

    def test_maintenance_modes_still_work_on_the_real_data(self):
        """絞り込んだ結果、実データの補正まで止まっていないこと。"""
        real_before = list(
            orm_models.Player.objects.filter(stints__team__league__world__isnull=True)
            .order_by("id")
            .values_list("name", flat=True)
        )

        self._run("--rename-existing")

        real_after = list(
            orm_models.Player.objects.filter(stints__team__league__world__isnull=True)
            .order_by("id")
            .values_list("name", flat=True)
        )
        self.assertNotEqual(real_before, real_after, "実データの選手の氏名が選び直されていません")


class RebuildFieldingLinesScopeTest(SeedScopeCase):
    def test_only_the_real_games_are_rebuilt(self):
        orm_models.GameFieldingLine.objects.filter(game_id=self.pennant_game_id).delete()

        call_command("rebuild_fielding_lines", workspace=DEFAULT_WORKSPACE_KEY, stdout=StringIO(), stderr=StringIO())

        self.assertFalse(
            orm_models.GameFieldingLine.objects.filter(game_id=self.pennant_game_id).exists(),
            "世界の試合の守備成績は、実データ向けのコマンドでは作り直さない",
        )


# --- ワークスペース（W1b T5）---

BATTER_POSITIONS = ["捕手"] * 2 + ["内野手"] * 5 + ["外野手"] * 4 + ["指名打者"]
PITCHERS = 11
UNKNOWN_KEY = "nosuchws00"


def _workspace_rows(workspace: orm_models.Workspace) -> dict[str, list]:
    """ワークスペースの実データと世界の中身。ほかのワークスペースを指したコマンドで変わってはならない。"""
    return {
        "players": list(
            orm_models.Player.objects.filter(stints__team__league__workspace=workspace)
            .distinct()
            .order_by("id")
            .values_list("id", "name", "name_kana", "back_name", "high_school", "university", "corporate_team")
        ),
        "stints": list(
            orm_models.PlayerStint.objects.filter(team__league__workspace=workspace)
            .order_by("id")
            .values_list("id", "player_id", "team_id", "number", "from_year", "to_year")
        ),
        "games": list(
            orm_models.Game.objects.filter(home_team__league__workspace=workspace)
            .order_by("id")
            .values_list("id", "year", "home_score", "away_score")
        ),
        "fielding": list(
            orm_models.GameFieldingLine.objects.filter(game__home_team__league__workspace=workspace)
            .order_by("id")
            .values_list("id", "game_id", "player_id", "putouts", "assists", "errors")
        ),
        "worlds": list(orm_models.PennantWorld.objects.filter(workspace=workspace).values_list("id", flat=True)),
        "world_games": list(
            orm_models.Game.objects.filter(home_team__league__world__workspace=workspace)
            .order_by("id")
            .values_list("id", flat=True)
        ),
    }


class WorkspaceCommandsCase(TestCase):
    """ワークスペース A（既定。id が最小）と B に、試合を組める2球団のリーグと、投入した試合。

    A を最小の id にしておくので、コマンドが `--workspace` を見ずに一時関数のワークスペースを使えば、
    B を指しても A が変わる（それを検査で拾う）。
    """

    workspace_a: orm_models.Workspace
    workspace_b: orm_models.Workspace
    leagues: dict[str, orm_models.League]
    homes: dict[str, orm_models.Team]

    @classmethod
    def setUpTestData(cls):
        cls.workspace_a = default_workspace()
        cls.workspace_b = make_workspace(name="ワークスペースB")
        assert cls.workspace_a.id < cls.workspace_b.id
        cls.leagues, cls.homes = {}, {}
        for label, workspace in (("A", cls.workspace_a), ("B", cls.workspace_b)):
            league = make_league(f"{label}リーグ", workspace=workspace)
            stadium = make_stadium(f"{label}球場", workspace=workspace, capacity=30000)
            home = orm_models.Team.objects.create(league=league, name=f"{label}ホーム", home_stadium=stadium)
            rival = orm_models.Team.objects.create(league=league, name=f"{label}ビジター")
            for team in (home, rival):
                register_players(
                    team,
                    [
                        *(
                            (f"{team.name}野手{number}", number, position)
                            for number, position in enumerate(BATTER_POSITIONS, start=1)
                        ),
                        *((f"{team.name}投手{number}", 20 + number, "投手") for number in range(1, PITCHERS + 1)),
                    ],
                )
            cls.leagues[label], cls.homes[label] = league, home
            call_command(
                "seed_virtual_games", workspace=workspace.key, year=YEAR, games_per_pair=1, seed=1, verbosity=0
            )

    def setUp(self):
        self.before_a = _workspace_rows(self.workspace_a)
        self.before_b = _workspace_rows(self.workspace_b)
        self.assertTrue(self.before_a["games"], "A に試合が投入されていません")
        self.assertTrue(self.before_b["games"], "B に試合が投入されていません")

    def assert_a_untouched(self):
        self.assertEqual(_workspace_rows(self.workspace_a), self.before_a)

    def assert_nothing_changed(self):
        self.assert_a_untouched()
        self.assertEqual(_workspace_rows(self.workspace_b), self.before_b)

    def _create_world(self, label: str) -> int:
        created = build_pennant_world_service(real_scope(self.leagues[label].workspace)).create_world(
            name=f"{label}の世界",
            owner_id=None,
            source_league_ids=[self.leagues[label].id],
            start_year=YEAR,
            seed=1,
            managed_source_team_id=self.homes[label].id,
        )
        return created.world.id

    def commands(self) -> list[tuple[str, list[str]]]:
        """実データを読み書きするコマンドと、`--workspace` 以外の引数（A のリーグ・球団を指す）。"""
        league, home = str(self.leagues["A"].id), str(self.homes["A"].id)
        return [
            ("seed_virtual_players", ["--seed", "1"]),
            ("seed_virtual_players", ["--rename-existing", "--seed", "1"]),
            ("seed_virtual_games", ["--year", str(YEAR), "--replace", "--seed", "1"]),
            ("rebuild_fielding_lines", []),
            ("pennant_create", ["--name", "x", "--league", league, "--managed-team", home]),
            ("simulate_sample", ["--from-real-leagues", league]),
            ("simulate_sample", ["--from-real-leagues", league, "--seasons", "1"]),
        ]


class WorkspaceOptionTest(WorkspaceCommandsCase):
    def test_omitting_the_workspace_is_an_error(self):
        """省くと日本語のエラーで、何も書かない（id が最小のワークスペースを推し量らない）。"""
        for command, args in self.commands():
            with self.subTest(command=command, args=args):
                with self.assertRaisesMessage(CommandError, "--workspace でワークスペースの key を指定してください"):
                    call_command(command, *args, stdout=StringIO(), stderr=StringIO())

                self.assert_nothing_changed()

    def test_an_unknown_key_is_an_error(self):
        for command, args in self.commands():
            with self.subTest(command=command, args=args):
                with self.assertRaisesMessage(CommandError, f"ワークスペースが見つかりません（key={UNKNOWN_KEY}）"):
                    call_command(command, "--workspace", UNKNOWN_KEY, *args, stdout=StringIO(), stderr=StringIO())

                self.assert_nothing_changed()

    def test_simulate_sample_without_real_leagues_needs_no_workspace(self):
        """DB を読まない型（既定・`--season`）は `--workspace` を取らない。"""
        out = StringIO()
        call_command("simulate_sample", games=1, seed=1, stdout=out)

        self.assertIn("ms/試合", out.getvalue())

    def test_simulate_sample_rejects_a_workspace_without_real_leagues(self):
        """DB を読まない型に `--workspace` を渡すと、黙って捨てずにエラーにする。"""
        for extra in ([], ["--season"]):
            with (
                self.subTest(extra=extra),
                self.assertRaisesMessage(
                    CommandError, "--workspace は --from-real-leagues と一緒に指定してください。"
                ),
            ):
                call_command("simulate_sample", "--workspace", self.workspace_a.key, *extra, stdout=StringIO())


class SeedCommandsWorkspaceTest(WorkspaceCommandsCase):
    def _run(self, command: str, *args: str) -> None:
        call_command(command, "--workspace", self.workspace_b.key, *args, stdout=StringIO(), stderr=StringIO())

    def test_seed_virtual_players_fills_only_that_workspace(self):
        self._run("seed_virtual_players", "--seed", "1")

        self.assertGreater(len(_workspace_rows(self.workspace_b)["stints"]), len(self.before_b["stints"]))
        self.assert_a_untouched()

    def test_maintenance_modes_touch_only_that_workspace(self):
        for option in ("--refresh-schools", "--rename-existing", "--assign-readings"):
            with self.subTest(option=option):
                self._run("seed_virtual_players", option, "--seed", "1")

                self.assert_a_untouched()

        b_names = [row[1] for row in _workspace_rows(self.workspace_b)["players"]]
        self.assertNotEqual(b_names, [row[1] for row in self.before_b["players"]], "B の氏名が選び直されていません")

    def test_seed_virtual_games_replace_touches_only_that_workspace(self):
        """A にも同じ年の試合があるが、`--replace` が消して作り直すのは B の試合だけ。"""
        self._run("seed_virtual_games", "--year", str(YEAR), "--replace", "--seed", "2", "--verbosity", "0")

        after_b = {row[0] for row in _workspace_rows(self.workspace_b)["games"]}
        self.assertTrue(after_b)
        self.assertFalse(after_b & {row[0] for row in self.before_b["games"]}, "B の試合が作り直されていません")
        self.assert_a_untouched()

    def test_seed_virtual_games_plays_only_that_workspace_s_players(self):
        self._run("seed_virtual_games", "--year", str(YEAR + 1), "--seed", "2", "--verbosity", "0")

        new_games = orm_models.Game.objects.filter(year=YEAR + 1)
        self.assertTrue(new_games.exists())
        self.assertFalse(new_games.exclude(home_team__league__workspace=self.workspace_b).exists())
        strangers = orm_models.GamePlateAppearance.objects.filter(game__in=new_games).exclude(
            batter__stints__team__league__workspace=self.workspace_b
        )
        self.assertFalse(strangers.exists(), "ほかのワークスペースの選手が打席に立っています")
        self.assert_a_untouched()

    def test_rebuild_fielding_lines_rebuilds_only_that_workspace(self):
        orm_models.GameFieldingLine.objects.all().delete()
        self.before_a = _workspace_rows(self.workspace_a)

        self._run("rebuild_fielding_lines")

        self.assertTrue(_workspace_rows(self.workspace_b)["fielding"], "B の守備成績が導き直されていません")
        self.assert_a_untouched()


class PennantCommandsWorkspaceTest(WorkspaceCommandsCase):
    def _create(self, workspace: orm_models.Workspace, league_label: str) -> None:
        call_command(
            "pennant_create",
            "--workspace",
            workspace.key,
            "--name",
            "分岐",
            "--league",
            str(self.leagues[league_label].id),
            "--managed-team",
            str(self.homes[league_label].id),
            "--seed",
            "1",
            stdout=StringIO(),
        )

    def test_pennant_create_cannot_fork_a_league_of_another_workspace(self):
        with self.assertRaisesMessage(CommandError, "リーグが見つかりません"):
            self._create(self.workspace_a, "B")

        self.assertFalse(orm_models.PennantWorld.objects.exists(), "世界が作られています")
        self.assert_nothing_changed()

    def test_pennant_create_makes_the_world_in_that_workspace(self):
        self._create(self.workspace_b, "B")

        world = orm_models.PennantWorld.objects.get()
        self.assertEqual(world.workspace_id, self.workspace_b.id)
        self.assert_a_untouched()

    def test_pennant_advance_runs_in_the_world_s_workspace(self):
        """`--world` は世界の行からワークスペースを導く（B の世界は B の範囲で進み、A の試合は増えない）。"""
        self._create_world("A")
        b_world = self._create_world("B")
        self.before_a = _workspace_rows(self.workspace_a)

        call_command("pennant_advance", "--world", str(b_world), stdout=StringIO())

        self.assertTrue(
            orm_models.Game.objects.filter(home_team__league__world_id=b_world).exists(), "B の世界が進んでいません"
        )
        self.assert_a_untouched()

    def test_simulate_sample_reads_only_that_workspace_s_leagues(self):
        """B を指して A（id が最小）のリーグを読めない。id が最小のワークスペースに落ちる退行なら読めてしまう。"""
        for extra in ([], ["--seasons", "1"]):
            with self.subTest(extra=extra), self.assertRaisesMessage(CommandError, "リーグが見つかりません"):
                call_command(
                    "simulate_sample",
                    "--workspace",
                    self.workspace_b.key,
                    "--from-real-leagues",
                    str(self.leagues["A"].id),
                    *extra,
                    stdout=StringIO(),
                )
