"""ペナントの日程生成の単体テスト。Django も DB も使わない。"""

import datetime
import random
import time
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from itertools import combinations
from unittest import TestCase

from myapp.domain.exceptions import InvalidSchedule
from myapp.domain.pennant.schedule import (
    Fixture,
    MonthDay,
    ScheduleRules,
    default_opening_day,
    generate_schedule,
    interleague_pairs,
    longest_streak,
    season_schedule,
)
from myapp.domain.simulation.randomness import game_seed, make_random

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
        self.assertEqual(143, self.rules.games_per_team)
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


def _leagues(sizes: Sequence[int]) -> dict[int, list[int]]:
    leagues: dict[int, list[int]] = {}
    next_id = 1
    for i, size in enumerate(sizes):
        leagues[i + 1] = list(range(next_id, next_id + size))
        next_id += size
    return leagues


class MultiLeagueTests(TestCase):
    """1〜8リーグ・球団数の違うリーグでも成り立つ条件。"""

    def _check(self, sizes: Sequence[int], seed: int = 1, season: int = 2026) -> list[Fixture]:
        leagues = _leagues(sizes)
        rules = ScheduleRules()
        fixtures = generate_schedule(leagues, rules, START, random.Random(seed), season=season)
        league_of = {t: lid for lid, teams in leagues.items() for t in teams}
        opponents = set()
        for a, b in interleague_pairs(list(leagues), season):
            opponents.add(frozenset((a, b)))

        games: Counter[int] = Counter()
        seen: set[tuple[datetime.date, int]] = set()
        for f in fixtures:
            self.assertNotEqual(0, f.date.weekday())
            for team in (f.home_team_id, f.visitor_team_id):
                games[team] += 1
                self.assertNotIn((f.date, team), seen)
                seen.add((f.date, team))
        self.assertEqual(dict.fromkeys(league_of, rules.games_per_team), dict(games))
        self.assertEqual(sum(len(t) for t in leagues.values()) * rules.games_per_team // 2, len(fixtures))

        cards = Counter(frozenset((f.home_team_id, f.visitor_team_id)) for f in fixtures)
        home = Counter((f.home_team_id, f.visitor_team_id) for f in fixtures)
        intra_by_league: defaultdict[int, set[int]] = defaultdict(set)
        inter_hosts: defaultdict[frozenset[int], set[int]] = defaultdict(set)
        for f in fixtures:
            if league_of[f.home_team_id] != league_of[f.visitor_team_id]:
                inter_hosts[frozenset((f.home_team_id, f.visitor_team_id))].add(f.home_team_id)
        for a, b in combinations(league_of, 2):
            card = frozenset((a, b))
            la, lb = league_of[a], league_of[b]
            if la == lb:
                intra_by_league[la].add(cards[card])
                # ホームの偏りは相手ごとに1以内
                self.assertLessEqual(abs(home[(a, b)] - home[(b, a)]), 1, (a, b))
            elif frozenset((la, lb)) in opponents:
                self.assertEqual(3, cards[card], (a, b))
                self.assertEqual(1, len(inter_hosts[card]), (a, b))  # 3連戦は同じ球場
            else:
                self.assertEqual(0, cards[card], (a, b))
        for lid, counts in intra_by_league.items():
            self.assertLessEqual(max(counts) - min(counts), 1, lid)  # 相手ごとに1試合差まで
        self.assertLessEqual(longest_streak(fixtures), ScheduleRules().max_streak)
        return fixtures

    def test_one_league_has_no_interleague(self) -> None:
        fixtures = self._check([6])
        self.assertEqual(429, len(fixtures))
        self.assertEqual(
            {28, 29}, set(Counter(frozenset((f.home_team_id, f.visitor_team_id)) for f in fixtures).values())
        )

    def test_two_leagues_match_npb_format(self) -> None:
        self.assertEqual(858, len(self._check([6, 6])))

    def test_three_leagues(self) -> None:
        self.assertEqual(1287, len(self._check([6, 6, 6])))

    def test_four_to_seven_leagues(self) -> None:
        for count in (4, 5, 6, 7):
            self.assertEqual(count * 6 * 143 // 2, len(self._check([6] * count, season=2030 + count)), count)

    def test_eight_leagues_of_six_teams(self) -> None:
        started = time.perf_counter()
        fixtures = self._check([6] * 8)
        self.assertEqual(3432, len(fixtures))
        self.assertLess(time.perf_counter() - started, 3.0)
        # 交流戦 3 × 12 = 36、リーグ内 107 を5相手に 21〜22
        intra = Counter(
            frozenset((f.home_team_id, f.visitor_team_id))
            for f in fixtures
            if (f.home_team_id - 1) // 6 == (f.visitor_team_id - 1) // 6
        )
        self.assertEqual({21, 22}, set(intra.values()))

    def test_leagues_of_different_size(self) -> None:
        self._check([6, 4])
        self._check([6, 4, 6], season=2027)

    def test_leagues_with_odd_number_of_teams_get_byes(self) -> None:
        self._check([5, 3])
        self._check([7, 7], season=2028)

    def test_all_seeds_hold(self) -> None:
        for seed in range(10):
            self._check([6] * 8, seed=seed, season=2026 + seed)

    def test_same_random_gives_same_schedule(self) -> None:
        leagues = _leagues([6] * 8)
        first = generate_schedule(leagues, ScheduleRules(), START, random.Random(5), season=2026)
        second = generate_schedule(leagues, ScheduleRules(), START, random.Random(5), season=2026)
        self.assertEqual(first, second)

    def test_season_changes_the_interleague_opponents(self) -> None:
        leagues = _leagues([6] * 8)

        def inter_cards(season: int) -> set[frozenset[int]]:
            fixtures = generate_schedule(leagues, ScheduleRules(), START, random.Random(5), season=season)
            return {
                frozenset((f.home_team_id, f.visitor_team_id))
                for f in fixtures
                if (f.home_team_id - 1) // 6 != (f.visitor_team_id - 1) // 6
            }

        self.assertNotEqual(inter_cards(2026), inter_cards(2027))
        self.assertEqual(
            len(inter_cards(2026)), 8 * 6 * 6 * 2 // 2
        )  # 各リーグが2リーグと当たる = 8 × 2 / 2 組 × 36 カード


class InterleaguePairsTests(TestCase):
    def test_pairs_are_symmetric_and_each_league_meets_at_most_two(self) -> None:
        for count in range(1, 9):
            ids = list(range(10, 10 + count))
            for season in range(2026, 2036):
                pairs = interleague_pairs(ids, season)
                self.assertEqual(len(pairs), len(set(pairs)))
                degree = Counter(x for pair in pairs for x in pair)
                expected = min(2, count - 1)
                self.assertEqual(dict.fromkeys(ids, expected) if expected else {}, dict(degree), (count, season))
                for a, b in pairs:
                    self.assertNotEqual(a, b)

    def test_two_leagues_meet_every_year_and_one_league_never(self) -> None:
        self.assertEqual([(1, 2)], interleague_pairs([2, 1], 2026))
        self.assertEqual([], interleague_pairs([1], 2026))

    def test_opponents_rotate_year_by_year(self) -> None:
        for count in (4, 5, 6, 7, 8):
            ids = list(range(1, count + 1))
            years = [frozenset(interleague_pairs(ids, season)) for season in range(2026, 2026 + 6)]
            self.assertGreater(len(set(years)), 1, count)
            self.assertTrue(all(years[i] != years[i + 1] for i in range(5)), count)

    def test_three_leagues_always_meet_everyone(self) -> None:
        self.assertEqual({(1, 2), (1, 3), (2, 3)}, set(interleague_pairs([1, 2, 3], 2026)))


class ScheduleOtherRulesTests(TestCase):
    def test_rules_values_are_respected(self) -> None:
        rules = ScheduleRules(games_per_team=40, inter_games=2, series_length=2, rest_weekday=1)
        fixtures = generate_schedule(LEAGUES, rules, START, random.Random(3))
        self.assertEqual(12 * 40 // 2, len(fixtures))
        self.assertTrue(all(f.date.weekday() != 1 for f in fixtures))
        games = Counter(t for f in fixtures for t in (f.home_team_id, f.visitor_team_id))
        self.assertEqual(dict.fromkeys(LEAGUES[1] + LEAGUES[2], 40), dict(games))

    def test_interleague_window_can_be_widened(self) -> None:
        leagues = _leagues([6] * 8)
        narrow = ScheduleRules(interleague_end=MonthDay(6, 30))
        with self.assertRaisesRegex(InvalidSchedule, "交流戦"):
            generate_schedule(leagues, narrow, START, random.Random(0))
        wide = ScheduleRules(interleague_end=MonthDay(7, 31))
        self.assertEqual(3432, len(generate_schedule(leagues, wide, START, random.Random(0))))


class ScheduleInvalidInputTests(TestCase):
    def _generate(
        self,
        leagues: Mapping[int, Sequence[int]],
        rules: ScheduleRules | None = None,
        start: datetime.date = START,
    ) -> list[Fixture]:
        return generate_schedule(leagues, rules or ScheduleRules(), start, random.Random(0))

    def test_no_leagues(self) -> None:
        with self.assertRaisesRegex(InvalidSchedule, "リーグ"):
            self._generate({})

    def test_empty_league(self) -> None:
        with self.assertRaisesRegex(InvalidSchedule, "球団のいない"):
            self._generate({1: [], 2: [1, 2]})

    def test_duplicate_team(self) -> None:
        with self.assertRaisesRegex(InvalidSchedule, "重複"):
            self._generate({1: [1, 2], 2: [2, 3]})

    def test_remainder_that_cannot_be_spread(self) -> None:
        # 5球団だけのリーグは、リーグ内 143 試合を4相手に配ると余り3（奇数 × 奇数の正則グラフは無い）
        with self.assertRaisesRegex(InvalidSchedule, "配れません"):
            self._generate({1: [1, 2, 3, 4, 5]})

    def test_single_team_league_cannot_fill_the_season(self) -> None:
        with self.assertRaises(InvalidSchedule):
            self._generate({1: [1]})

    def test_interleague_larger_than_the_season(self) -> None:
        with self.assertRaisesRegex(InvalidSchedule, "超えます"):
            self._generate(LEAGUES, ScheduleRules(games_per_team=10))

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
            ScheduleRules(games_per_team=0)
        with self.assertRaises(InvalidSchedule):
            ScheduleRules(rest_weekday=7)
        with self.assertRaises(InvalidSchedule):
            ScheduleRules(series_length=1)


class DefaultOpeningDayTests(TestCase):
    def test_is_the_first_friday_on_or_after_march_25(self) -> None:
        # 2026-03-27 は金曜（NPB の2026年開幕と同じ日）
        self.assertEqual(default_opening_day(2026, ScheduleRules()), datetime.date(2026, 3, 27))
        # 3/25 以降で最初の金曜。3/26 が金曜ならその日
        self.assertEqual(default_opening_day(2027, ScheduleRules()), datetime.date(2027, 3, 26))
        self.assertEqual(default_opening_day(2030, ScheduleRules()), datetime.date(2030, 3, 29))

    def test_is_always_a_friday_in_late_march(self) -> None:
        for year in range(2000, 2100):
            day = default_opening_day(year, ScheduleRules())
            self.assertEqual(day.weekday(), 4, year)
            self.assertEqual(day.month, 3, year)
            self.assertGreaterEqual(day.day, 25, year)

    def test_moves_off_the_rest_weekday(self) -> None:
        rules = ScheduleRules(rest_weekday=4)

        day = default_opening_day(2026, rules)

        self.assertEqual(day, datetime.date(2026, 3, 28))
        self.assertNotEqual(day.weekday(), rules.rest_weekday)


class SeasonScheduleTests(TestCase):
    """年ごとの日程。開幕年も翌年も同じ関数で作る。"""

    @staticmethod
    def _pairings(fixtures: list[Fixture]) -> list[tuple[int, int]]:
        return [(f.home_team_id, f.visitor_team_id) for f in fixtures]

    def test_the_same_world_and_year_give_the_same_schedule(self) -> None:
        self.assertEqual(
            season_schedule(LEAGUES, world_seed=5, year=2026), season_schedule(LEAGUES, world_seed=5, year=2026)
        )

    def test_each_year_gets_its_own_schedule(self) -> None:
        first = season_schedule(LEAGUES, world_seed=5, year=2026)
        second = season_schedule(LEAGUES, world_seed=5, year=2027)

        self.assertEqual(len(first), len(second))
        self.assertTrue(all(f.date.year == 2026 for f in first))
        self.assertTrue(all(f.date.year == 2027 for f in second))
        self.assertNotEqual(self._pairings(first), self._pairings(second))

    def test_a_different_world_seed_gives_a_different_schedule(self) -> None:
        self.assertNotEqual(
            season_schedule(LEAGUES, world_seed=5, year=2026), season_schedule(LEAGUES, world_seed=6, year=2026)
        )

    def test_it_opens_on_the_default_opening_day(self) -> None:
        fixtures = season_schedule(LEAGUES, world_seed=5, year=2027)

        self.assertEqual(min(f.date for f in fixtures), default_opening_day(2027, ScheduleRules()))

    def test_the_opening_year_is_what_generate_schedule_made_before(self) -> None:
        """開幕年の日程は、これまでの作り方（開幕日・シード・年）と同じ。同じシードの世界は同じ日程のまま。"""
        rules = ScheduleRules()
        expected = generate_schedule(
            LEAGUES,
            rules,
            default_opening_day(2026, rules),
            make_random(game_seed(5, 2026, "schedule")),
            season=2026,
        )

        self.assertEqual(season_schedule(LEAGUES, world_seed=5, year=2026), expected)
