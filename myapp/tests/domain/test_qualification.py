"""規定打席・規定投球回とチーム成績の単体テスト。Django も DB も使わない。"""

from unittest import TestCase

from myapp.domain import services
from myapp.domain.entities import Player
from myapp.domain.value_objects import (
    BattingLine,
    InningsPitched,
    JerseyNumber,
    PitchingLine,
    Position,
)


def _batter(name, number, **line) -> Player:
    return Player(
        name=name,
        number=JerseyNumber(number),
        position=Position.INFIELDER,
        id=number,
        batting=BattingLine(**line),
    )


def _pitcher(name, number, notation="0.0", **line) -> Player:
    return Player(
        name=name,
        number=JerseyNumber(number),
        position=Position.PITCHER,
        id=number,
        pitching=PitchingLine(innings=InningsPitched.from_notation(notation), **line),
    )


class RequiredThresholdTest(TestCase):
    def test_plate_appearances_follow_the_npb_rule(self):
        """規定打席は 試合数 × 3.1。端数は切り上げる。"""
        self.assertEqual(services.required_plate_appearances(143), 444)
        self.assertEqual(services.required_plate_appearances(10), 31)
        self.assertEqual(services.required_plate_appearances(0), 0)

    def test_innings_follow_the_npb_rule(self):
        """規定投球回は 試合数 × 1.0。アウト数で表す。"""
        self.assertEqual(services.required_outs(143), 429)
        self.assertEqual(services.required_outs(10), 30)


class QualifiedBattersTest(TestCase):
    def setUp(self):
        # 10試合 → 規定打席は31打席
        self.games = 10
        self.regular = _batter("規定到達", 1, at_bats=35, singles=10)
        self.few = _batter("少打席", 2, at_bats=1, singles=1)

    def _team_games(self, *players):
        return {p.id: self.games for p in players}

    def test_a_single_hit_does_not_top_the_leaderboard(self):
        """1打数1安打の選手が打率10割で首位に立たないこと。"""
        players = [self.regular, self.few]

        leaders = services.leaders_by_batting_average(players, team_games=self._team_games(*players))

        self.assertEqual([r.player.name for r in leaders], ["規定到達"])

    def test_qualified_batters_need_enough_plate_appearances(self):
        players = [self.regular, self.few]

        qualified = services.qualified_batters(players, team_games=self._team_games(*players))

        self.assertEqual([p.name for p in qualified], ["規定到達"])

    def test_walks_count_toward_plate_appearances(self):
        """打席は打数だけでなく四球・死球・犠飛も含む。"""
        patient = _batter("選球眼", 3, at_bats=20, singles=5, walks=15)

        qualified = services.qualified_batters([patient], team_games={patient.id: self.games})

        self.assertEqual([p.name for p in qualified], ["選球眼"])

    def test_without_team_games_only_minimum_at_bats_applies(self):
        """試合数を渡さない場合は従来どおり最低打数だけで絞る。"""
        qualified = services.qualified_batters([self.regular, self.few])

        self.assertEqual(len(qualified), 2)

    def test_nobody_qualifies_when_too_few_games_played(self):
        players = [self.few]
        self.games = 143

        self.assertEqual(services.qualified_batters(players, team_games=self._team_games(*players)), [])

    def test_home_run_leaders_ignore_the_threshold(self):
        """本塁打は本数そのものが記録なので規定を設けない。"""
        slugger = _batter("代打の切り札", 4, at_bats=5, home_runs=3)

        leaders = services.leaders_by_home_runs([slugger, self.regular])

        self.assertEqual(leaders[0].player.name, "代打の切り札")


class QualifiedPitchersTest(TestCase):
    def test_short_relief_does_not_qualify(self):
        """1イニングだけの好投で防御率の首位に立たないこと。"""
        starter = _pitcher("先発", 18, "60.0", earned_runs=20)
        reliever = _pitcher("抑え", 19, "1.0", earned_runs=0)
        team_games = {starter.id: 30, reliever.id: 30}

        leaders = services.leaders_by_era([starter, reliever], team_games=team_games)

        self.assertEqual([r.player.name for r in leaders], ["先発"])

    def test_without_team_games_only_unpitched_is_excluded(self):
        starter = _pitcher("先発", 18, "60.0", earned_runs=20)
        never = _pitcher("未登板", 20, "0.0")

        qualified = services.qualified_pitchers([starter, never])

        self.assertEqual([p.name for p in qualified], ["先発"])


class TeamTotalsTest(TestCase):
    def test_batting_is_summed_across_the_roster(self):
        players = [
            _batter("A", 1, at_bats=10, singles=3),
            _batter("B", 2, at_bats=10, home_runs=2),
        ]

        total = services.team_batting(players)

        self.assertEqual(total.at_bats, 20)
        self.assertEqual(total.hits, 5)
        self.assertAlmostEqual(total.batting_average, 0.25)

    def test_rates_are_recomputed_not_averaged(self):
        """選手ごとの率を平均しても正しいチーム率にはならない。"""
        players = [
            _batter("多打数", 1, at_bats=100, singles=20),  # .200
            _batter("少打数", 2, at_bats=1, singles=1),  # 1.000
        ]

        total = services.team_batting(players)

        # 率の平均なら .600 だが、正しくは 21/101
        self.assertAlmostEqual(total.batting_average, 21 / 101)

    def test_pitching_innings_are_added_as_outs(self):
        players = [
            _pitcher("A", 11, "5.2", earned_runs=2),
            _pitcher("B", 12, "5.2", earned_runs=1),
        ]

        total = services.team_pitching(players)

        self.assertEqual(str(total.innings), "11.1")
        self.assertEqual(total.earned_runs, 3)

    def test_empty_roster(self):
        self.assertEqual(services.team_batting([]).at_bats, 0)
        self.assertEqual(services.team_pitching([]).innings.outs, 0)
