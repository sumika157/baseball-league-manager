"""GM ホームの「進めた結果」の材料（期間の見どころ・期間で絞った試合の一覧）の参照。

世界の範囲に閉じること（実データの試合・選手は混ざらない）と、期間の端（`after` は含まず `through` は含む）を確かめる。
"""

from datetime import date, timedelta

from myapp.domain.pennant.world import WorldScope
from myapp.infrastructure.queries import DjangoGameListQuery, DjangoPennantActivityQuery

from .world_case import PENNANT_PLAYER_PREFIX, YEAR, WorldCase

PLAYED = date(YEAR, 4, 2)  # 世界の試合の日（WorldCase）


class GameNotesTest(WorldCase):
    def test_the_decisions_and_the_home_runs_are_read(self):
        notes = DjangoPennantActivityQuery(self.scope).game_notes([self.pennant_game_id])

        note = notes[self.pennant_game_id]
        # 5対2でホームの勝ち。投手は先発が1人ずつ
        self.assertTrue(note.winning_pitcher.startswith(PENNANT_PLAYER_PREFIX))
        self.assertTrue(note.losing_pitcher.startswith(PENNANT_PLAYER_PREFIX))
        self.assertNotEqual(note.winning_pitcher, note.losing_pitcher)
        self.assertEqual(note.save_pitcher, "")
        self.assertGreaterEqual(len(note.home_runs), 2, "両チームの本塁打を打った選手")

    def test_a_game_without_lines_has_no_note(self):
        self.assertEqual(DjangoPennantActivityQuery(self.scope).game_notes([self.real_game.id]), {})
        self.assertEqual(DjangoPennantActivityQuery(self.scope).game_notes([]), {})

    def test_another_scope_does_not_see_the_game(self):
        """実データの範囲では、世界の試合の見どころは読めない。"""
        self.assertEqual(DjangoPennantActivityQuery(WorldScope.real()).game_notes([self.pennant_game_id]), {})


class PeriodTotalsTest(WorldCase):
    def test_batting_is_summed_per_player_within_the_period(self):
        rows = DjangoPennantActivityQuery(self.scope).batting_between(
            self.pennant_team.id, after=PLAYED - timedelta(days=1), through=PLAYED
        )

        self.assertGreaterEqual(len(rows), 1)
        self.assertTrue(all(row.name.startswith(PENNANT_PLAYER_PREFIX) for row in rows))
        self.assertEqual(sum(row.batting.home_runs for row in rows), 5, "ホームの5本塁打")
        self.assertTrue(all(row.batting.hits >= row.batting.home_runs for row in rows))
        self.assertTrue(
            all(
                row.batting.batting_average == row.batting.hits / row.batting.at_bats
                for row in rows
                if row.batting.at_bats
            )
        )

    def test_the_period_excludes_the_after_day_and_includes_the_through_day(self):
        query = DjangoPennantActivityQuery(self.scope)

        self.assertEqual(query.batting_between(self.pennant_team.id, after=PLAYED, through=PLAYED), [])
        self.assertEqual(query.pitching_between(self.pennant_team.id, after=PLAYED, through=PLAYED), [])
        self.assertNotEqual(
            query.batting_between(self.pennant_team.id, after=PLAYED - timedelta(days=1), through=PLAYED), []
        )

    def test_pitching_is_the_pitchers_of_that_team(self):
        query = DjangoPennantActivityQuery(self.scope)
        window = {"after": PLAYED - timedelta(days=1), "through": PLAYED}

        home = query.pitching_between(self.pennant_team.id, **window)
        away = query.pitching_between(self.pennant_rival.id, **window)

        self.assertEqual([(row.wins, row.losses) for row in home], [(1, 0)])
        self.assertEqual([(row.wins, row.losses) for row in away], [(0, 1)])

    def test_another_team_has_nothing(self):
        rows = DjangoPennantActivityQuery(self.scope).batting_between(
            self.team.id, after=PLAYED - timedelta(days=1), through=PLAYED
        )

        self.assertEqual(rows, [], "実データの球団の打撃は、世界の範囲では読めない")


class GameRowsInPeriodTest(WorldCase):
    def test_after_is_exclusive_and_through_is_inclusive(self):
        query = DjangoGameListQuery(self.scope)

        self.assertEqual(len(query.list_rows(after=PLAYED - timedelta(days=1), through=PLAYED)), 1)
        self.assertEqual(query.list_rows(after=PLAYED), [])
        self.assertEqual(query.list_rows(through=PLAYED - timedelta(days=1)), [])

    def test_the_period_combines_with_the_other_filters(self):
        query = DjangoGameListQuery(self.scope)
        window = {"after": PLAYED - timedelta(days=1), "through": PLAYED}

        self.assertEqual(len(query.list_rows(year=YEAR, team_id=self.pennant_team.id, **window)), 1)
        self.assertEqual(query.list_rows(year=YEAR + 1, **window), [])
