"""データ投入・補正コマンドが、ペナントの世界に触れないこと。

投入コマンドは ORM へ直接書く。対象を絞らないと、`--replace` がペナントの試合を消し、
ペナントの選手が実データの試合に出場し、ペナントの球団にも選手が足される。
エラーにならずデータだけがずれるので、世界を作ってから流して、世界の側が変わらないことを見る。
"""

from io import StringIO

from django.core.management import call_command

from myapp.infrastructure import orm_models

from ..helpers import play_game
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
            number = first
            for label, count in (("捕手", 2), ("外野手", 5), ("投手", 8)):
                for index in range(count):
                    self.service.register_player(team.id, f"{team.name}追加{label}{index}", number, label)
                    number += 1
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
        call_command("seed_virtual_games", year=year, games_per_pair=2, seed=1, verbosity=0)

    def test_replace_does_not_delete_the_pennant_games(self):
        """`--replace` は実データのその年の試合だけを消す。"""
        self.assertIn(self.pennant_game_id, self.before_games)
        old_real_game = self.real_game.id

        call_command("seed_virtual_games", year=YEAR, games_per_pair=2, seed=1, verbosity=0, replace=True)

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
        call_command("seed_virtual_games", year=YEAR, games_per_pair=2, seed=1, verbosity=0, replace=True)

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
        call_command("seed_virtual_games", year=YEAR, games_per_pair=2, seed=1, verbosity=0, replace=True)

        self._assert_world_untouched()


class SeedVirtualPlayersScopeTest(SeedScopeCase):
    def _run(self, *args):
        call_command("seed_virtual_players", "--seed", "1", *args, stdout=StringIO())

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

        call_command("rebuild_fielding_lines", stdout=StringIO(), stderr=StringIO())

        self.assertFalse(
            orm_models.GameFieldingLine.objects.filter(game_id=self.pennant_game_id).exists(),
            "世界の試合の守備成績は、実データ向けのコマンドでは作り直さない",
        )
