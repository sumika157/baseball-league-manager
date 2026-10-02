"""ペナントの日程生成の単体テスト。Django も DB も使わない。"""

import datetime
import random
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from unittest import TestCase

from myapp.domain.exceptions import InvalidSchedule
from myapp.domain.pennant.schedule import (
    Fixture,
    MonthDay,
    ScheduleRules,
    generate_schedule,
    longest_streak,
)

LEAGUES = {1: [1, 2, 3, 4, 5, 6], 2: [11, 12, 13, 14, 15, 16]}
START = datetime.date(2026, 3, 27)  # 金曜


def _league_of(team_id: int) -> int:
    return 1 if team_id in LEAGUES[1] else 2


def _is_interleague(f: Fixture) -> bool:
    return _league_of(f.home_team_id) != _league_of(f.visitor_team_id)


class ScheduleShapeTests(TestCase):
    rules: ScheduleRules
    fixtures: list[Fixture]
    teams: list[int]

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.rules = ScheduleRules()
        cls.fixtures = generate_schedule(LEAGUES, cls.rules, START, random.Random(2026))
        cls.teams = LEAGUES[1] + LEAGUES[2]

    def test_total_is_858_and_each_team_plays_143(self) -> None:
        self.assertEqual(858, len(self.fixtures))
        self.assertEqual(143, self.rules.games_per_team(6))
        games: Counter[int] = Counter()
        for f in self.fixtures:
            games[f.home_team_id] += 1
            games[f.visitor_team_id] += 1
        self.assertEqual(dict.fromkeys(self.teams, 143), dict(games))

    def test_no_team_plays_twice_on_a_day(self) -> None:
        seen: set[tuple[datetime.date, int]] = set()
        for f in self.fixtures:
            for team in (f.home_team_id, f.visitor_team_id):
                self.assertNotIn((f.date, team), seen)
                seen.add((f.date, team))

    def test_card_game_counts(self) -> None:
        cards = Counter(frozenset((f.home_team_id, f.visitor_team_id)) for f in self.fixtures)
        for a in self.teams:
            for b in self.teams:
                if a >= b:
                    continue
                expected = 25 if _league_of(a) == _league_of(b) else 3
                self.assertEqual(expected, cards[frozenset((a, b))], (a, b))

    def test_intra_league_home_is_12_or_13(self) -> None:
        home = Counter((f.home_team_id, f.visitor_team_id) for f in self.fixtures)
        for a in self.teams:
            for b in self.teams:
                if a != b and _league_of(a) == _league_of(b):
                    self.assertIn(home[(a, b)], (12, 13), (a, b))
                    self.assertEqual(25, home[(a, b)] + home[(b, a)])

    def test_interleague_is_balanced_for_every_team_and_league(self) -> None:
        inter = [f for f in self.fixtures if _is_interleague(f)]
        self.assertEqual(108, len(inter))
        home = Counter(f.home_team_id for f in inter)
        self.assertEqual(dict.fromkeys(self.teams, 9), dict(home))
        by_league = Counter(_league_of(f.home_team_id) for f in inter)
        self.assertEqual({1: 54, 2: 54}, dict(by_league))
        # 1カード3試合は同じ球場で行う（ホームが割れない）
        card_hosts: defaultdict[frozenset[int], set[int]] = defaultdict(set)
        for f in inter:
            card_hosts[frozenset((f.home_team_id, f.visitor_team_id))].add(f.home_team_id)
        self.assertTrue(all(len(hosts) == 1 for hosts in card_hosts.values()))

    def test_no_games_on_monday(self) -> None:
        self.assertTrue(all(f.date.weekday() != 0 for f in self.fixtures))

    def test_interleague_is_within_the_window(self) -> None:
        dates = [f.date for f in self.fixtures if _is_interleague(f)]
        self.assertGreaterEqual(min(dates), datetime.date(2026, 5, 24))
        self.assertLessEqual(max(dates), datetime.date(2026, 6, 30))

    def test_interleague_is_a_contiguous_block(self) -> None:
        inter_days = {f.date for f in self.fixtures if _is_interleague(f)}
        others = {f.date for f in self.fixtures if not _is_interleague(f)}
        self.assertEqual(18, len(inter_days))
        self.assertFalse(inter_days & others)
        self.assertTrue(any(d < min(inter_days) for d in others))
        self.assertTrue(any(d > max(inter_days) for d in others))

    def test_season_runs_from_opening_day_to_september(self) -> None:
        dates = [f.date for f in self.fixtures]
        self.assertEqual(START, min(dates))
        self.assertLessEqual(max(dates), datetime.date(2026, 10, 10))

    def test_sorted_by_date(self) -> None:
        dates = [f.date for f in self.fixtures]
        self.assertEqual(sorted(dates), dates)

    def test_home_and_away_streaks_stay_short(self) -> None:
        self.assertLessEqual(longest_streak(self.fixtures), 9)

    def test_consecutive_playing_days_stay_within_a_week(self) -> None:
        days: defaultdict[int, set[datetime.date]] = defaultdict(set)
        for f in self.fixtures:
            days[f.home_team_id].add(f.date)
            days[f.visitor_team_id].add(f.date)
        for team, played in days.items():
            run = longest = 0
            day = START
            while day <= max(played):
                run = run + 1 if day in played else 0
                longest = max(longest, run)
                day += datetime.timedelta(days=1)
            self.assertLessEqual(longest, 6, team)

    def test_same_card_is_not_played_in_a_row_across_series(self) -> None:
        # 同じカードの連戦は3試合まで（周の境目で6連戦にならない）
        by_team: defaultdict[int, list[tuple[datetime.date, int]]] = defaultdict(list)
        for f in self.fixtures:
            by_team[f.home_team_id].append((f.date, f.visitor_team_id))
            by_team[f.visitor_team_id].append((f.date, f.home_team_id))
        for team, games in by_team.items():
            games.sort()
            run = 0
            previous = None
            for _, opponent in games:
                run = run + 1 if opponent == previous else 1
                previous = opponent
                self.assertLessEqual(run, 3, team)


class ScheduleReproducibilityTests(TestCase):
    def test_same_random_sequence_gives_same_schedule(self) -> None:
        first = generate_schedule(LEAGUES, ScheduleRules(), START, random.Random(7))
        second = generate_schedule(LEAGUES, ScheduleRules(), START, random.Random(7))
        self.assertEqual(first, second)

    def test_different_random_sequence_gives_different_schedule(self) -> None:
        first = generate_schedule(LEAGUES, ScheduleRules(), START, random.Random(7))
        second = generate_schedule(LEAGUES, ScheduleRules(), START, random.Random(8))
        self.assertNotEqual(first, second)

    def test_only_random_is_used_from_the_source(self) -> None:
        class OnlyRandom:
            def __init__(self) -> None:
                self._rng = random.Random(1)

            def random(self) -> float:
                return self._rng.random()

        self.assertEqual(858, len(generate_schedule(LEAGUES, ScheduleRules(), START, OnlyRandom())))

    def test_degenerate_random_source_still_terminates(self) -> None:
        class Zero:
            def random(self) -> float:
                return 0.0

        self.assertEqual(858, len(generate_schedule(LEAGUES, ScheduleRules(), START, Zero())))

    def test_many_seeds_hold_every_invariant(self) -> None:
        for seed in range(30):
            fixtures = generate_schedule(LEAGUES, ScheduleRules(), START, random.Random(seed))
            self.assertEqual(858, len(fixtures))
            self.assertLessEqual(longest_streak(fixtures), 9)
            seen = {(f.date, t) for f in fixtures for t in (f.home_team_id, f.visitor_team_id)}
            self.assertEqual(2 * 858, len(seen))


class ScheduleOtherSizesTests(TestCase):
    def test_four_teams_per_league(self) -> None:
        leagues = {1: [1, 2, 3, 4], 2: [5, 6, 7, 8]}
        rules = ScheduleRules()
        fixtures = generate_schedule(leagues, rules, START, random.Random(3))
        self.assertEqual(8 * rules.games_per_team(4) // 2, len(fixtures))
        games = Counter(t for f in fixtures for t in (f.home_team_id, f.visitor_team_id))
        self.assertEqual({t: rules.games_per_team(4) for t in range(1, 9)}, dict(games))

    def test_rules_values_are_respected(self) -> None:
        rules = ScheduleRules(intra_games=4, inter_games=2, series_length=2, rest_weekday=1)
        fixtures = generate_schedule(LEAGUES, rules, START, random.Random(3))
        self.assertEqual(12 * rules.games_per_team(6) // 2, len(fixtures))
        self.assertTrue(all(f.date.weekday() != 1 for f in fixtures))


class ScheduleInvalidInputTests(TestCase):
    def _generate(
        self,
        leagues: Mapping[int, Sequence[int]],
        rules: ScheduleRules | None = None,
        start: datetime.date = START,
    ) -> list[Fixture]:
        return generate_schedule(leagues, rules or ScheduleRules(), start, random.Random(0))

    def test_odd_number_of_teams(self) -> None:
        with self.assertRaisesRegex(InvalidSchedule, "偶数"):
            self._generate({1: [1, 2, 3, 4, 5], 2: [6, 7, 8, 9, 10]})

    def test_single_league(self) -> None:
        with self.assertRaisesRegex(InvalidSchedule, "2リーグ"):
            self._generate({1: [1, 2, 3, 4, 5, 6]})

    def test_three_leagues(self) -> None:
        with self.assertRaises(InvalidSchedule):
            self._generate({1: [1, 2], 2: [3, 4], 3: [5, 6]})

    def test_leagues_of_different_size(self) -> None:
        with self.assertRaisesRegex(InvalidSchedule, "球団数が違う"):
            self._generate({1: [1, 2, 3, 4], 2: [5, 6]})

    def test_empty_league(self) -> None:
        with self.assertRaises(InvalidSchedule):
            self._generate({1: [], 2: []})

    def test_duplicate_team(self) -> None:
        with self.assertRaisesRegex(InvalidSchedule, "重複"):
            self._generate({1: [1, 2], 2: [2, 3]})

    def test_opening_day_on_rest_weekday(self) -> None:
        with self.assertRaisesRegex(InvalidSchedule, "休み"):
            self._generate(LEAGUES, start=datetime.date(2026, 3, 30))

    def test_interleague_does_not_fit_in_the_window(self) -> None:
        rules = ScheduleRules(interleague_end=MonthDay(5, 30))
        with self.assertRaisesRegex(InvalidSchedule, "交流戦"):
            self._generate(LEAGUES, rules)

    def test_fixture_with_same_team(self) -> None:
        with self.assertRaises(InvalidSchedule):
            Fixture(START, 1, 1)

    def test_invalid_rules(self) -> None:
        with self.assertRaises(InvalidSchedule):
            ScheduleRules(intra_games=0)
        with self.assertRaises(InvalidSchedule):
            ScheduleRules(rest_weekday=7)
