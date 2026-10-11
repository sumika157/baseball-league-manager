"""試合の開始・終了・中断・観客数の単体テスト。Django も DB も使わない。

試合時間は保存せず `GameTimes` が「終了 − 開始 − 中断」で導く（公認野球規則 9.02（ℓ））。
開始・終了・観客数は打席から導けない手入力の事実で、`Game` が持つが `is_recorded` には効かない。
"""

from datetime import date, time
from typing import Any
from unittest import TestCase

from myapp.domain import services
from myapp.domain.entities import Game
from myapp.domain.exceptions import InvalidGame
from myapp.domain.value_objects import GameHeader, GameTimes, Season, attendance_label


class GameTimesTest(TestCase):
    def test_the_duration_is_end_minus_start(self):
        times = GameTimes(start=time(18, 19), end=time(21, 9))

        self.assertEqual(times.elapsed_minutes, 170)
        self.assertEqual(times.net_minutes, 170)
        self.assertEqual(times.duration_label, "2時間50分")

    def test_the_interruption_is_subtracted_from_the_official_duration(self):
        times = GameTimes(start=time(18, 0), end=time(21, 30), interruption_minutes=25)

        self.assertEqual(times.elapsed_minutes, 210)
        self.assertEqual(times.net_minutes, 185)
        self.assertEqual(times.duration_label, "3時間5分")

    def test_a_game_ending_after_midnight_needs_the_next_day_flag(self):
        times = GameTimes(start=time(18, 0), end=time(0, 30), ends_next_day=True)

        self.assertEqual(times.net_minutes, 390)

    def test_an_end_before_the_start_is_rejected_without_the_flag(self):
        """終了が開始より前なら翌日と推測しない（取り違えた入力が「23時間の試合」になるため）。"""
        with self.assertRaisesRegex(InvalidGame, "終了時刻は開始時刻より後"):
            GameTimes(start=time(21, 0), end=time(18, 0))

    def test_an_end_equal_to_the_start_is_rejected(self):
        with self.assertRaisesRegex(InvalidGame, "終了時刻は開始時刻より後"):
            GameTimes(start=time(18, 0), end=time(18, 0))

    def test_the_flag_with_an_end_after_the_start_is_rejected_as_24_hours_or_more(self):
        with self.assertRaisesRegex(InvalidGame, "24時間未満"):
            GameTimes(start=time(18, 0), end=time(19, 0), ends_next_day=True)

    def test_an_interruption_as_long_as_the_game_is_rejected(self):
        with self.assertRaisesRegex(InvalidGame, "中断時間は"):
            GameTimes(start=time(18, 0), end=time(19, 0), interruption_minutes=60)

    def test_a_negative_interruption_is_rejected(self):
        with self.assertRaisesRegex(InvalidGame, "負の値"):
            GameTimes(start=time(18, 0), end=time(21, 0), interruption_minutes=-1)

    def test_under_an_hour_is_labelled_in_minutes(self):
        self.assertEqual(GameTimes(start=time(18, 0), end=time(18, 45)).duration_label, "45分")


def _game(**overrides) -> Game:
    values: dict[str, Any] = {
        "season": Season(2026),
        "played_on": date(2026, 4, 1),
        "home_team_id": 1,
        "away_team_id": 2,
    }
    values.update(overrides)
    return Game(**values)


class GameAttendanceTest(TestCase):
    def test_a_negative_attendance_is_rejected(self):
        with self.assertRaisesRegex(InvalidGame, "観客数に負の値"):
            _game(attendance=-1)

    def test_a_non_numeric_attendance_is_rejected(self):
        with self.assertRaisesRegex(InvalidGame, "観客数は数値"):
            _game(attendance="多数")

    def test_the_times_and_attendance_do_not_make_a_game_recorded(self):
        game = _game(times=GameTimes(start=time(18, 0), end=time(21, 0)), attendance=30000)

        self.assertFalse(game.is_recorded)


class AssembleGameTest(TestCase):
    def test_the_header_times_and_attendance_are_copied_to_the_game(self):
        times = GameTimes(start=time(18, 19), end=time(21, 9))
        header = GameHeader(
            season=Season(2026),
            played_on=date(2026, 4, 1),
            home_team_id=1,
            away_team_id=2,
            times=times,
            attendance=42071,
        )

        game = services.assemble_game(header, [], [])

        self.assertEqual(game.times, times)
        self.assertEqual(game.attendance, 42071)


class AttendanceWarningsTest(TestCase):
    def test_exceeding_the_capacity_gives_one_warning_naming_the_numbers_and_the_stadium(self):
        warnings = services.attendance_warnings(50000, "月島パレスドーム", 42000)

        self.assertEqual(len(warnings), 1)
        message = warnings[0].message
        self.assertIn("50,000人", message)
        self.assertIn("42,000人", message)
        self.assertIn("月島パレスドーム", message)

    def test_exactly_the_capacity_gives_no_warning(self):
        self.assertEqual(services.attendance_warnings(42000, "球場", 42000), ())

    def test_nothing_to_compare_gives_no_warning(self):
        self.assertEqual(services.attendance_warnings(None, "球場", 42000), ())
        self.assertEqual(services.attendance_warnings(50000, "球場", None), ())


class GameTimesFromInputsTest(TestCase):
    """画面・管理画面の入力の組み合わせの規則（出典はここ。フォームは例外を訳すだけ）。"""

    def test_both_empty_is_unrecorded(self):
        self.assertIsNone(GameTimes.from_inputs(None, None, False, None))
        self.assertIsNone(GameTimes.from_inputs(None, None, False, 0))

    def test_start_without_end_is_rejected(self):
        with self.assertRaisesRegex(InvalidGame, "開始時刻と終了時刻は両方入力してください"):
            GameTimes.from_inputs(time(18, 0), None, False, None)

    def test_end_without_start_is_rejected(self):
        with self.assertRaisesRegex(InvalidGame, "開始時刻と終了時刻は両方入力してください"):
            GameTimes.from_inputs(None, time(21, 0), False, None)

    def test_interruption_without_times_is_rejected(self):
        with self.assertRaisesRegex(InvalidGame, "中断時間と「翌日」の印は"):
            GameTimes.from_inputs(None, None, False, 10)

    def test_next_day_without_times_is_rejected(self):
        with self.assertRaisesRegex(InvalidGame, "中断時間と「翌日」の印は"):
            GameTimes.from_inputs(None, None, True, None)

    def test_a_blank_interruption_means_zero(self):
        times = GameTimes.from_inputs(time(18, 0), time(21, 0), False, None)

        assert times is not None
        self.assertEqual(times.interruption_minutes, 0)

    def test_the_value_object_rules_still_apply(self):
        with self.assertRaisesRegex(InvalidGame, "終了時刻は開始時刻より後"):
            GameTimes.from_inputs(time(21, 0), time(18, 0), False, None)


class GameTimesLabelsTest(TestCase):
    def test_labels(self):
        times = GameTimes(start=time(18, 0), end=time(0, 30), ends_next_day=True, interruption_minutes=25)

        self.assertEqual((times.start_text, times.end_text), ("18:00", "00:30"))
        self.assertEqual(times.end_label, "翌00:30")
        self.assertEqual(times.interruption_label, "25分")
        self.assertEqual(GameTimes(start=time(18, 0), end=time(21, 0)).interruption_label, "")
        self.assertEqual(GameTimes(start=time(18, 0), end=time(21, 0)).end_label, "21:00")
        self.assertEqual(attendance_label(42071), "42,071人")
