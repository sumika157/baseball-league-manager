"""管理コマンド `simulate_sample`。**DB には一切書かない**こと。

シミュレーションエンジンの確認と調整の道具で、ボックススコアを文字で出し、水準の表を
目標帯と並べて出す。DB に触れないことは「クエリが1本も発行されない」ことで確かめる。
"""

import re
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from myapp.domain.pennant.schedule import ScheduleRules
from myapp.infrastructure import orm_models
from myapp.management.commands import simulate_sample

TIMING_LINE = re.compile(r"[\d.]+ ms/試合")


def run(**options) -> str:
    out = StringIO()
    call_command("simulate_sample", stdout=out, **options)
    return out.getvalue()


class SimulateSampleTest(TestCase):
    def test_it_never_touches_the_database(self):
        before = {model.__name__: model.objects.count() for model in orm_models.__dict__.values() if _is_model(model)}
        with self.assertNumQueries(0):
            run(games=5, seed=3)
        after = {model.__name__: model.objects.count() for model in orm_models.__dict__.values() if _is_model(model)}
        self.assertEqual(before, after)

    def test_it_prints_a_box_score_and_the_level_table_against_the_target_bands(self):
        output = run(games=30, seed=3)
        self.assertIn("打撃", output)
        self.assertIn("投手", output)
        self.assertIn("投球回", output)
        self.assertIn("リーグ全体の水準", output)
        self.assertIn("1チーム1試合の得点", output)
        self.assertIn("目標 3.8〜4", output)
        self.assertIn("ms/試合", output)

    def test_the_box_score_reads_like_a_score_sheet(self):
        output = run(games=1, seed=3)
        self.assertRegex(output, r"平均\d \d+ - \d+ 平均\d（\d+回）")
        self.assertRegex(output, r"R\s+H\s+E")
        # 先発（登板順1）と、野手9人（打順1〜9）が載っている
        for order in range(1, 10):
            self.assertIn(f"\n{order} ", output)

    def test_the_same_seed_prints_the_same_result(self):
        first = TIMING_LINE.sub("", run(games=20, seed=11))
        second = TIMING_LINE.sub("", run(games=20, seed=11))
        self.assertEqual(first, second)
        self.assertNotEqual(first, TIMING_LINE.sub("", run(games=20, seed=12)))

    def test_a_game_count_below_one_is_rejected(self):
        with self.assertRaises(CommandError):
            run(games=0)

    def test_the_season_mode_prints_the_title_levels_too(self):
        # リーグ内は各相手2試合・交流戦は1試合（1球団16試合・全96試合）の日程に縮めて、数秒に収める。
        # 143試合の調整は手で回す
        small = ScheduleRules(games_per_team=16, inter_games=1, series_length=2)
        with patch.object(simulate_sample, "SEASON_RULES", small), self.assertNumQueries(0):
            output = run(season=True, seed=2)
        self.assertIn("1シーズン 96試合", output)
        self.assertIn("タイトル争いの水準", output)
        self.assertIn("首位打者の打率", output)
        self.assertIn("本塁打王の本数", output)


class BoxScoreFormatTest(TestCase):
    def test_pad_counts_full_width_characters_as_two_columns(self):
        self.assertEqual(simulate_sample._pad("平均", 6), "平均  ")
        self.assertEqual(simulate_sample._pad("ab", 6), "ab    ")
        self.assertEqual(simulate_sample._pad("long-long-name", 4), "long-long-name")


def _is_model(value) -> bool:
    return isinstance(value, type) and hasattr(value, "objects") and hasattr(value, "_meta")
