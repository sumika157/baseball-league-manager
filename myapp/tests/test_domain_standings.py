"""シーズン成績と順位表の単体テスト。Django も DB も使わない。"""

from unittest import TestCase

from myapp.domain import services
from myapp.domain.entities import Team
from myapp.domain.exceptions import InvalidSeason, InvalidStatValue
from myapp.domain.value_objects import Season, TeamRecord


class SeasonTest(TestCase):
    def test_accepts_valid_year(self):
        self.assertEqual(Season(2026).year, 2026)
        self.assertEqual(str(Season(2026)), '2026年')

    def test_string_is_normalised(self):
        self.assertEqual(Season('2026').year, 2026)

    def test_rejects_out_of_range(self):
        with self.assertRaises(InvalidSeason):
            Season(1899)
        with self.assertRaises(InvalidSeason):
            Season(2101)

    def test_rejects_non_numeric(self):
        with self.assertRaises(InvalidSeason):
            Season('令和8年')

    def test_equality_is_by_value(self):
        self.assertEqual(Season(2026), Season(2026))
        self.assertNotEqual(Season(2026), Season(2025))


class TeamRecordTest(TestCase):
    def test_games_played_includes_ties(self):
        self.assertEqual(TeamRecord(wins=80, losses=55, ties=8).games_played, 143)

    def test_winning_percentage_excludes_ties(self):
        """日本プロ野球の規則では勝率の分母に引分を含めない。"""
        record = TeamRecord(wins=80, losses=55, ties=8)
        self.assertAlmostEqual(record.winning_percentage, 80 / 135)

    def test_all_ties_does_not_divide_by_zero(self):
        self.assertEqual(TeamRecord(ties=10).winning_percentage, 0.0)

    def test_no_games_does_not_divide_by_zero(self):
        self.assertEqual(TeamRecord().winning_percentage, 0.0)

    def test_games_behind(self):
        leader = TeamRecord(wins=80, losses=55)
        chaser = TeamRecord(wins=72, losses=63)
        # ((80-72) + (63-55)) / 2 = 8.0
        self.assertEqual(chaser.games_behind(leader), 8.0)

    def test_leader_has_no_games_behind(self):
        leader = TeamRecord(wins=80, losses=55)
        self.assertEqual(leader.games_behind(leader), 0.0)

    def test_negative_is_rejected(self):
        with self.assertRaises(InvalidStatValue):
            TeamRecord(wins=-1)


def _team(name, team_id, records=None) -> Team:
    team = Team(name=name, id=team_id, league_id=1)
    for year, (w, l, t) in (records or {}).items():
        team.record_season(Season(year), TeamRecord(wins=w, losses=l, ties=t))
    return team


class TeamSeasonTest(TestCase):
    def test_record_season(self):
        team = _team('A', 1, {2026: (80, 55, 8)})
        entry = team.season_record(Season(2026))

        self.assertIsNotNone(entry)
        self.assertEqual(entry.record.wins, 80)

    def test_same_season_is_overwritten_not_duplicated(self):
        """同じ年が2件並ぶと順位表が破綻するため、集約側で1件に保つ。"""
        team = _team('A', 1, {2026: (80, 55, 8)})
        team.record_season(Season(2026), TeamRecord(wins=90, losses=45, ties=8))

        self.assertEqual(len(team.seasons), 1)
        self.assertEqual(team.season_record(Season(2026)).record.wins, 90)

    def test_different_seasons_coexist(self):
        team = _team('A', 1, {2025: (70, 65, 8), 2026: (80, 55, 8)})
        self.assertEqual(len(team.seasons), 2)

    def test_seasons_desc(self):
        team = _team('A', 1, {2024: (60, 75, 8), 2026: (80, 55, 8), 2025: (70, 65, 8)})
        self.assertEqual([s.season.year for s in team.seasons_desc()], [2026, 2025, 2024])

    def test_missing_season_returns_none(self):
        self.assertIsNone(_team('A', 1).season_record(Season(2026)))


class StandingsTest(TestCase):
    def setUp(self):
        self.teams = [
            _team('ブラックス', 1, {2026: (80, 55, 8)}),
            _team('イーグルス', 2, {2026: (72, 63, 8)}),
            _team('タイガース', 3, {2026: (65, 70, 8)}),
        ]

    def test_ordered_by_winning_percentage(self):
        rows = services.standings(self.teams, Season(2026))

        self.assertEqual(
            [r.team_name for r in rows], ['ブラックス', 'イーグルス', 'タイガース']
        )
        self.assertEqual([r.rank for r in rows], [1, 2, 3])

    def test_games_behind_is_measured_from_the_leader(self):
        rows = services.standings(self.teams, Season(2026))

        self.assertEqual(rows[0].games_behind, 0.0)
        self.assertEqual(rows[1].games_behind, 8.0)
        self.assertTrue(rows[0].is_leader)

    def test_same_percentage_shares_the_rank(self):
        teams = [
            _team('A', 1, {2026: (70, 70, 3)}),
            _team('B', 2, {2026: (70, 70, 3)}),
        ]
        rows = services.standings(teams, Season(2026))

        self.assertEqual([r.rank for r in rows], [1, 1])

    def test_teams_without_a_record_are_excluded(self):
        """未登録を0勝0敗として並べると、全敗と区別できなくなる。"""
        teams = self.teams + [_team('未登録', 4)]
        rows = services.standings(teams, Season(2026))

        self.assertEqual(len(rows), 3)
        self.assertNotIn('未登録', [r.team_name for r in rows])

    def test_other_seasons_are_not_mixed_in(self):
        teams = [
            _team('A', 1, {2025: (90, 45, 8), 2026: (60, 75, 8)}),
            _team('B', 2, {2026: (80, 55, 8)}),
        ]
        rows = services.standings(teams, Season(2026))

        self.assertEqual([r.team_name for r in rows], ['B', 'A'])
        self.assertEqual(rows[0].record.wins, 80)

    def test_empty_season_returns_empty(self):
        self.assertEqual(services.standings(self.teams, Season(2020)), [])

    def test_recorded_seasons_is_newest_first(self):
        teams = [
            _team('A', 1, {2024: (60, 75, 8), 2026: (80, 55, 8)}),
            _team('B', 2, {2025: (70, 65, 8), 2026: (72, 63, 8)}),
        ]
        self.assertEqual(
            [s.year for s in services.recorded_seasons(teams)], [2026, 2025, 2024]
        )
