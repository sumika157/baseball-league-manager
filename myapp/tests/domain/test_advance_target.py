"""「進める」範囲の決定（AdvanceTarget / dates_to_play）の単体テスト。"""

import datetime
from collections.abc import Sequence
from unittest import TestCase

from myapp.domain.exceptions import InvalidSchedule
from myapp.domain.pennant.schedule import AdvanceTarget, Fixture, dates_to_play

MINE, OTHER_A, OTHER_B = 1, 2, 3


def _d(month: int, day: int) -> datetime.date:
    return datetime.date(2026, month, day)


def _fx(month: int, day: int, home: int, visitor: int) -> Fixture:
    return Fixture(_d(month, day), home, visitor)


# 4/1・4/2 は他球団どうし、4/3 に自軍、4/6(月) は休み、4/7・4/8、4/30 は他球団どうし、5/1 に自軍
FIXTURES = [
    _fx(4, 1, OTHER_A, OTHER_B),
    _fx(4, 2, OTHER_A, OTHER_B),
    _fx(4, 3, MINE, OTHER_A),
    _fx(4, 3, OTHER_B, 9),
    _fx(4, 7, OTHER_A, OTHER_B),
    _fx(4, 8, OTHER_A, OTHER_B),
    _fx(4, 30, OTHER_A, OTHER_B),
    _fx(5, 1, OTHER_B, MINE),
    _fx(5, 2, OTHER_A, OTHER_B),
]
TODAY = _d(3, 31)


class DatesToPlayTests(TestCase):
    def _dates(
        self,
        target: AdvanceTarget,
        today: datetime.date = TODAY,
        fixtures: Sequence[Fixture] = FIXTURES,
    ) -> list[datetime.date]:
        return dates_to_play(fixtures, today, target, MINE)

    def test_day_is_the_next_date_with_a_game(self) -> None:
        self.assertEqual([_d(4, 1)], self._dates(AdvanceTarget.DAY))

    def test_day_skips_days_without_games(self) -> None:
        self.assertEqual([_d(4, 7)], self._dates(AdvanceTarget.DAY, today=_d(4, 3)))

    def test_week_is_seven_days_from_the_next_game(self) -> None:
        self.assertEqual([_d(4, 1), _d(4, 2), _d(4, 3), _d(4, 7)], self._dates(AdvanceTarget.WEEK))

    def test_week_with_a_gap_still_advances(self) -> None:
        fixtures = [_fx(5, 20, 1, 2), _fx(5, 21, 1, 2), _fx(5, 30, 1, 2)]
        self.assertEqual([_d(5, 20), _d(5, 21)], self._dates(AdvanceTarget.WEEK, fixtures=fixtures))

    def test_next_managed_game_includes_that_day(self) -> None:
        self.assertEqual([_d(4, 1), _d(4, 2), _d(4, 3)], self._dates(AdvanceTarget.NEXT_MANAGED_GAME))

    def test_next_managed_game_when_it_is_the_next_day(self) -> None:
        self.assertEqual([_d(4, 3)], self._dates(AdvanceTarget.NEXT_MANAGED_GAME, today=_d(4, 2)))

    def test_next_managed_game_after_playing_one(self) -> None:
        expected = [_d(4, 7), _d(4, 8), _d(4, 30), _d(5, 1)]
        self.assertEqual(expected, self._dates(AdvanceTarget.NEXT_MANAGED_GAME, today=_d(4, 3)))

    def test_next_managed_game_without_more_games_goes_to_the_end(self) -> None:
        self.assertEqual([_d(5, 2)], self._dates(AdvanceTarget.NEXT_MANAGED_GAME, today=_d(5, 1)))

    def test_next_managed_game_needs_a_managed_team(self) -> None:
        with self.assertRaises(InvalidSchedule):
            dates_to_play(FIXTURES, TODAY, AdvanceTarget.NEXT_MANAGED_GAME, None)

    def test_month_end(self) -> None:
        expected = [_d(4, 1), _d(4, 2), _d(4, 3), _d(4, 7), _d(4, 8), _d(4, 30)]
        self.assertEqual(expected, self._dates(AdvanceTarget.MONTH_END))

    def test_month_end_on_the_last_day_moves_to_the_next_month(self) -> None:
        self.assertEqual([_d(5, 1), _d(5, 2)], self._dates(AdvanceTarget.MONTH_END, today=_d(4, 30)))

    def test_season_end_is_everything_left(self) -> None:
        self.assertEqual(sorted({f.date for f in FIXTURES}), self._dates(AdvanceTarget.SEASON_END))

    def test_nothing_left_gives_nothing(self) -> None:
        for target in AdvanceTarget:
            self.assertEqual([], self._dates(target, today=_d(5, 2)), target)
            self.assertEqual([], dates_to_play([], TODAY, target, MINE), target)

    def test_every_target_includes_the_next_game_date(self) -> None:
        for target in AdvanceTarget:
            self.assertEqual(_d(4, 1), self._dates(target)[0], target)

    def test_result_is_sorted_and_unique(self) -> None:
        shuffled = list(reversed(FIXTURES))
        result = self._dates(AdvanceTarget.SEASON_END, fixtures=shuffled)
        self.assertEqual(sorted(set(result)), result)
