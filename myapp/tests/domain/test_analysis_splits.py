"""期間別成績（選手の月別・年別、チームの月別）のドメインサービスの単体テスト。

DB も Django も使わない。集計の規則そのものを確かめる。
"""

from unittest import TestCase

from myapp.domain import services
from myapp.domain.value_objects import BattingLine, InningsPitched, PitchingLine

from .analysis_builders import DRAGONS, SWALLOWS, TIGERS, _game


class MonthlySplitTest(TestCase):
    PLAYER = 10

    def test_grouped_by_month_in_chronological_order(self):
        games = [
            _game(
                TIGERS,
                DRAGONS,
                1,
                0,
                month=5,
                day=1,
                batting={
                    self.PLAYER: BattingLine(at_bats=4, singles=2),
                },
            ),
            _game(
                TIGERS,
                DRAGONS,
                1,
                0,
                month=4,
                day=1,
                batting={
                    self.PLAYER: BattingLine(at_bats=3, singles=1),
                },
            ),
            _game(
                TIGERS,
                DRAGONS,
                1,
                0,
                month=4,
                day=2,
                batting={
                    self.PLAYER: BattingLine(at_bats=2, home_runs=1),
                },
            ),
        ]

        splits = services.monthly_splits(games, self.PLAYER)

        self.assertEqual([s.label for s in splits], ["2026年4月", "2026年5月"])
        april = splits[0]
        self.assertEqual(april.appearances, 2)
        self.assertEqual(april.batting.at_bats, 5)
        self.assertEqual(april.batting.hits, 2)

    def test_rate_is_recalculated_from_the_monthly_total(self):
        """月の率は、その月の合計から計算し直す（試合ごとの率の平均ではない）。"""
        games = [
            _game(
                TIGERS,
                DRAGONS,
                1,
                0,
                month=4,
                day=1,
                batting={
                    self.PLAYER: BattingLine(at_bats=1, singles=1),
                },
            ),
            _game(
                TIGERS,
                DRAGONS,
                1,
                0,
                month=4,
                day=2,
                batting={
                    self.PLAYER: BattingLine(at_bats=3),
                },
            ),
        ]

        april = services.monthly_splits(games, self.PLAYER)[0]

        # 打率の平均なら .500 になるが、合計から求めれば 1/4 = .250
        self.assertAlmostEqual(april.batting.batting_average, 0.25)

    def test_same_month_of_different_seasons_is_not_merged(self):
        games = [
            _game(
                TIGERS,
                DRAGONS,
                1,
                0,
                season=2025,
                month=4,
                batting={
                    self.PLAYER: BattingLine(at_bats=4, singles=1),
                },
            ),
            _game(
                TIGERS,
                DRAGONS,
                1,
                0,
                season=2026,
                month=4,
                batting={
                    self.PLAYER: BattingLine(at_bats=4, singles=3),
                },
            ),
        ]

        splits = services.monthly_splits(games, self.PLAYER)

        self.assertEqual([s.label for s in splits], ["2025年4月", "2026年4月"])

    def test_months_without_appearance_are_omitted(self):
        """出場していない月は行を作らない。0 の行は休養と記録漏れを区別できない。"""
        games = [
            _game(
                TIGERS,
                DRAGONS,
                1,
                0,
                month=4,
                batting={
                    self.PLAYER: BattingLine(at_bats=4, singles=1),
                },
            ),
            _game(TIGERS, DRAGONS, 1, 0, month=5),
        ]

        splits = services.monthly_splits(games, self.PLAYER)

        self.assertEqual([s.label for s in splits], ["2026年4月"])

    def test_pitching_is_split_too(self):
        games = [
            _game(
                TIGERS,
                DRAGONS,
                1,
                0,
                month=4,
                day=1,
                pitching={
                    self.PLAYER: PitchingLine(innings=InningsPitched.from_notation("5.2"), earned_runs=1),
                },
            ),
            _game(
                TIGERS,
                DRAGONS,
                1,
                0,
                month=4,
                day=2,
                pitching={
                    self.PLAYER: PitchingLine(innings=InningsPitched.from_notation("3.1"), earned_runs=2),
                },
            ),
        ]

        april = services.monthly_splits(games, self.PLAYER)[0]

        # 5.2 + 3.1 = 9.0（アウト数で足すので 8.3 にはならない）
        self.assertEqual(str(april.pitching.innings), "9.0")
        self.assertAlmostEqual(april.pitching.earned_run_average, 3.0)


class YearlySplitTest(TestCase):
    """選手の年度別成績。月別と同じ束ね方で、単位が年になる。"""

    PLAYER = 10

    def test_grouped_by_year_in_chronological_order(self):
        games = [
            _game(
                TIGERS,
                DRAGONS,
                1,
                0,
                season=2026,
                month=4,
                batting={self.PLAYER: BattingLine(at_bats=4, singles=2)},
            ),
            _game(
                TIGERS,
                DRAGONS,
                1,
                0,
                season=2025,
                month=9,
                batting={self.PLAYER: BattingLine(at_bats=3, singles=1)},
            ),
        ]

        splits = services.yearly_splits(games, self.PLAYER)

        self.assertEqual([s.label for s in splits], ["2025年", "2026年"])

    def test_months_of_the_same_year_are_merged(self):
        """月別と違い、同じ年の別の月は1行にまとまる。"""
        games = [
            _game(
                TIGERS,
                DRAGONS,
                1,
                0,
                month=4,
                day=1,
                batting={self.PLAYER: BattingLine(at_bats=3, singles=1)},
            ),
            _game(
                TIGERS,
                DRAGONS,
                1,
                0,
                month=5,
                day=1,
                batting={self.PLAYER: BattingLine(at_bats=1, home_runs=1)},
            ),
        ]

        splits = services.yearly_splits(games, self.PLAYER)

        self.assertEqual(len(splits), 1)
        self.assertEqual(splits[0].appearances, 2)
        self.assertEqual(splits[0].batting.at_bats, 4)

    def test_rate_is_recalculated_from_the_yearly_total(self):
        """年の率は、その年の合計から計算し直す（月々の率の平均ではない）。"""
        games = [
            _game(
                TIGERS,
                DRAGONS,
                1,
                0,
                month=4,
                batting={self.PLAYER: BattingLine(at_bats=1, singles=1)},
            ),
            _game(
                TIGERS,
                DRAGONS,
                1,
                0,
                month=5,
                batting={self.PLAYER: BattingLine(at_bats=3)},
            ),
        ]

        year = services.yearly_splits(games, self.PLAYER)[0]

        # 月ごとの打率（1.000 と .000）の平均なら .500 になるが、合計なら 1/4 = .250
        self.assertAlmostEqual(year.batting.batting_average, 0.25)

    def test_years_without_appearance_are_omitted(self):
        """出場していない年は行を作らない（月別と同じ規則）。"""
        games = [
            _game(
                TIGERS,
                DRAGONS,
                1,
                0,
                season=2025,
                month=4,
                batting={self.PLAYER: BattingLine(at_bats=4, singles=1)},
            ),
            _game(TIGERS, DRAGONS, 1, 0, season=2026, month=4),
        ]

        splits = services.yearly_splits(games, self.PLAYER)

        self.assertEqual([s.label for s in splits], ["2025年"])

    def test_pitching_innings_are_summed_as_outs(self):
        games = [
            _game(
                TIGERS,
                DRAGONS,
                1,
                0,
                month=4,
                pitching={self.PLAYER: PitchingLine(innings=InningsPitched.from_notation("5.2"), earned_runs=1)},
            ),
            _game(
                TIGERS,
                DRAGONS,
                1,
                0,
                month=8,
                pitching={self.PLAYER: PitchingLine(innings=InningsPitched.from_notation("3.1"), earned_runs=2)},
            ),
        ]

        year = services.yearly_splits(games, self.PLAYER)[0]

        self.assertEqual(str(year.pitching.innings), "9.0")
        self.assertAlmostEqual(year.pitching.earned_run_average, 3.0)


class TeamMonthlySplitTest(TestCase):
    """チームの月別成績。選手の月別成績と対になる、チーム単位の推移。"""

    OURS, THEIRS = {10, 11}, {20}

    def test_record_and_totals_per_month(self):
        games = [
            _game(
                TIGERS,
                DRAGONS,
                5,
                3,
                month=4,
                day=1,
                batting={
                    10: BattingLine(at_bats=4, singles=2),
                    11: BattingLine(at_bats=3, home_runs=1),
                },
            ),
            _game(
                TIGERS,
                DRAGONS,
                1,
                2,
                month=4,
                day=2,
                batting={
                    10: BattingLine(at_bats=4, singles=1),
                },
            ),
            _game(
                TIGERS,
                DRAGONS,
                0,
                0,
                month=5,
                day=1,
                batting={
                    10: BattingLine(at_bats=4, singles=3),
                },
            ),
        ]

        splits = services.team_monthly_splits(games, TIGERS, self.OURS)

        self.assertEqual([s.label for s in splits], ["2026年4月", "2026年5月"])
        april = splits[0]
        self.assertEqual((april.record.wins, april.record.losses), (1, 1))
        self.assertEqual(april.batting.at_bats, 11)
        self.assertEqual(april.batting.hits, 4)
        self.assertEqual(splits[1].record.ties, 1)

    def test_opponent_lines_are_not_counted(self):
        """試合の明細には相手の選手も入っている。自チームの分だけを足す。"""
        games = [
            _game(
                TIGERS,
                DRAGONS,
                1,
                0,
                month=4,
                batting={
                    10: BattingLine(at_bats=4, singles=1),
                    20: BattingLine(at_bats=4, singles=4),
                },
            ),
        ]

        april = services.team_monthly_splits(games, TIGERS, self.OURS)[0]

        self.assertEqual(april.batting.at_bats, 4)
        self.assertEqual(april.batting.hits, 1)

    def test_rate_is_recalculated_from_the_monthly_total(self):
        games = [
            _game(
                TIGERS,
                DRAGONS,
                1,
                0,
                month=4,
                day=1,
                batting={
                    10: BattingLine(at_bats=1, singles=1),
                },
            ),
            _game(
                TIGERS,
                DRAGONS,
                1,
                0,
                month=4,
                day=2,
                batting={
                    10: BattingLine(at_bats=3),
                },
            ),
        ]

        april = services.team_monthly_splits(games, TIGERS, self.OURS)[0]

        # 率の平均なら .500 だが、合計から求めれば 1/4 = .250
        self.assertAlmostEqual(april.batting.batting_average, 0.25)

    def test_games_of_other_teams_are_excluded(self):
        games = [
            _game(TIGERS, DRAGONS, 1, 0, month=4),
            _game(DRAGONS, SWALLOWS, 5, 0, month=4, day=2),
        ]

        april = services.team_monthly_splits(games, TIGERS, self.OURS)[0]

        self.assertEqual(april.games_played, 1)

    def test_same_month_of_different_seasons_is_not_merged(self):
        games = [
            _game(TIGERS, DRAGONS, 1, 0, season=2025, month=4),
            _game(TIGERS, DRAGONS, 1, 0, season=2026, month=4),
        ]

        splits = services.team_monthly_splits(games, TIGERS, self.OURS)

        self.assertEqual([s.label for s in splits], ["2025年4月", "2026年4月"])

    def test_months_without_games_are_omitted(self):
        """試合の無い月は行を作らない。選手の月別成績と同じ規則。"""
        self.assertEqual(services.team_monthly_splits([], TIGERS, self.OURS), [])

    def test_innings_are_added_as_outs(self):
        games = [
            _game(
                TIGERS,
                DRAGONS,
                1,
                0,
                month=4,
                day=1,
                pitching={
                    11: PitchingLine(innings=InningsPitched.from_notation("5.2")),
                },
            ),
            _game(
                TIGERS,
                DRAGONS,
                1,
                0,
                month=4,
                day=2,
                pitching={
                    11: PitchingLine(innings=InningsPitched.from_notation("5.2")),
                },
            ),
        ]

        april = services.team_monthly_splits(games, TIGERS, self.OURS)[0]

        # 5.2 + 5.2 = 11.1（10.4 にはならない）
        self.assertEqual(str(april.pitching.innings), "11.1")
