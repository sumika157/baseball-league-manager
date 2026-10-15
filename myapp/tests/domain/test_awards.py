"""タイトルの部門（`Title`）とペナントの表彰（`awards.py`）、締めていない年の判定。DB 不要。#248"""

from __future__ import annotations

import unittest

from myapp.domain.entities import Player
from myapp.domain.pennant.awards import Award, title_counts
from myapp.domain.pennant.season import SeasonPhase, completed_years, unclosed_completed_year
from myapp.domain.services.rankings import WINNING_PERCENTAGE_MINIMUM_WINS, Title, leaders_by_home_runs
from myapp.domain.value_objects import BattingLine, JerseyNumber, Position


class TitleTest(unittest.TestCase):
    """部門の並び・名前・区分の出典。値は保存する文字列なので変えない。"""

    def test_the_stored_values_do_not_change(self) -> None:
        self.assertEqual(
            [title.value for title in Title],
            [
                "average",
                "home_runs",
                "rbi",
                "hits",
                "stolen_bases",
                "obp",
                "era",
                "wins",
                "winning_percentage",
                "strikeouts",
                "saves",
                "hold_points",
            ],
        )

    def test_batting_comes_before_pitching(self) -> None:
        flags = [title.is_pitching for title in Title]
        self.assertEqual(flags, sorted(flags), "打撃の部門が先、投手の部門が後")
        self.assertEqual(sum(flags), 6)

    def test_every_title_has_a_distinct_label(self) -> None:
        labels = [title.label for title in Title]
        self.assertEqual(len(set(labels)), len(labels))
        self.assertEqual(Title.AVERAGE.label, "首位打者")

    def test_only_the_rate_titles_have_a_note(self) -> None:
        noted = {title for title in Title if title.note}
        self.assertEqual(
            noted,
            {Title.AVERAGE, Title.ON_BASE_PERCENTAGE, Title.EARNED_RUN_AVERAGE, Title.WINNING_PERCENTAGE},
        )
        self.assertEqual(Title.WINNING_PERCENTAGE.note, f"{WINNING_PERCENTAGE_MINIMUM_WINS}勝以上")

    def test_the_order_is_the_definition_order(self) -> None:
        self.assertEqual([title.order for title in Title], list(range(len(Title))))


class TitleCountsTest(unittest.TestCase):
    def award(self, player_id: int, title: Title, year: int = 2026) -> Award:
        return Award(year=year, league_id=1, title=title, player_id=player_id, team_id=10, value="1")

    def test_each_award_counts_one(self) -> None:
        """同じ年の別の部門も、別の年の同じ部門も1つずつ。"""
        awards = [
            self.award(1, Title.HOME_RUNS),
            self.award(1, Title.RUNS_BATTED_IN),
            self.award(1, Title.HOME_RUNS, year=2027),
            self.award(2, Title.WINS),
        ]
        self.assertEqual(title_counts(awards), {1: 3, 2: 1})

    def test_no_awards_no_counts(self) -> None:
        self.assertEqual(title_counts([]), {})


class UnclosedCompletedYearTest(unittest.TestCase):
    """終わったが締めていない年は、シーズン終了の局面のいまの年度だけ（ほかの終わった年は締めてある）。"""

    def test_only_the_finished_phase_has_an_unclosed_year(self) -> None:
        self.assertEqual(unclosed_completed_year(phase=SeasonPhase.FINISHED, current_year=2028), 2028)
        for phase in (SeasonPhase.BEFORE_OPENING, SeasonPhase.IN_SEASON):
            with self.subTest(phase=phase):
                self.assertIsNone(unclosed_completed_year(phase=phase, current_year=2028))

    def test_the_unclosed_year_is_the_last_completed_year(self) -> None:
        """シーズン終了なら、終わった年の最後がその年。締めた後の開幕前なら、終わった年はどれも締めてある。"""
        recorded = [2026, 2027, 2028]
        finished = completed_years(recorded, phase=SeasonPhase.FINISHED, current_year=2028)
        self.assertEqual(finished[-1], unclosed_completed_year(phase=SeasonPhase.FINISHED, current_year=2028))
        closed = completed_years(recorded, phase=SeasonPhase.BEFORE_OPENING, current_year=2029)
        self.assertEqual(closed, recorded)
        self.assertIsNone(unclosed_completed_year(phase=SeasonPhase.BEFORE_OPENING, current_year=2029))


if __name__ == "__main__":
    unittest.main()


class TiedLeadersTest(unittest.TestCase):
    """表彰は同率の1位を全員拾う。部門の全員を順位づければ（`limit=None`）、4人以上の同率も全員が1位になる。"""

    @staticmethod
    def batter(name: str, number: int, home_runs: int) -> Player:
        return Player(
            name=name,
            number=JerseyNumber(str(number)),
            position=Position.INFIELDER,
            id=number,
            batting=BattingLine(at_bats=100, home_runs=home_runs),
        )

    def test_every_tied_leader_ranks_first_without_a_limit(self) -> None:
        tied = [self.batter(f"選手{n}", n, 30) for n in range(1, 6)]
        players = [*tied, self.batter("次点", 9, 29)]

        ranked = leaders_by_home_runs(players, limit=None)

        self.assertEqual({r.player.id for r in ranked if r.rank == 1}, {p.id for p in tied})
        self.assertEqual(next(r.rank for r in ranked if r.player.id == 9), 6)

    def test_the_titles_screen_limit_cuts_the_tie(self) -> None:
        """タイトルの画面の上位3人で切ると同率の1位が欠けるので、表彰は上限なしで導く。"""
        tied = [self.batter(f"選手{n}", n, 30) for n in range(1, 6)]

        self.assertEqual(len([r for r in leaders_by_home_runs(tied, limit=3) if r.rank == 1]), 3)
