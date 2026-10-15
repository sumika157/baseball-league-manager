"""投手の疲労（#97）。疲労は保存せず直近の登板と対戦打者数から導き、能力の目減りとして率に効かせる。

AI の普段の起用（6人ローテ・目安＋猶予の内側の交代・救援は2連投まで）では疲労がほとんど付かないこと、
酷使（中4日以下の先発・ブルペンが尽きたときの続投・救援の3連投や詰まった登板）に効くことを確かめる。
救援の連投の上限はスタミナで変わる（55以上は3連投まで。段階2）。
"""

from dataclasses import replace
from datetime import date, timedelta
from unittest import TestCase

from myapp.domain.simulation.engine import SimulatedGame, simulate_game
from myapp.domain.simulation.fatigue import (
    FATIGUE_WINDOW_DAYS,
    HIGH_STAMINA_RELIEVER,
    MAX_FATIGUE_POINTS,
    MID_INNING_GRACE,
    MIN_DAYS_BETWEEN_STARTS,
    FatigueLevel,
    PitcherWorkload,
    PitchingHistory,
    consecutive_points,
    fatigued,
    in_game_fatigue_points,
    max_consecutive_days,
    pregame_fatigue_points,
    recent_appearance_points,
    stamina_factor,
    starter_rest_points,
)
from myapp.domain.simulation.levels import UsageTally
from myapp.domain.simulation.manager import (
    ClubOrders,
    ForeignQuota,
    PitchingStaff,
    available_relievers,
    choose_active_roster,
    plan_pitching_staff,
)
from myapp.domain.simulation.odds import matchup
from myapp.domain.simulation.randomness import game_seed, make_random
from myapp.domain.simulation.ratings import BatterRatings, GrowthType, PitcherRatings
from myapp.domain.simulation.samples import average_club, round_robin_days, spread_league
from myapp.domain.value_objects import ForeignPlayerRules, GameStage

TODAY = date(2026, 5, 10)
OPENING_DAY = date(2026, 4, 1)


def workload(days_since_start: int | None, consecutive_days: int = 0, recent_appearances: int = 0) -> PitcherWorkload:
    return PitcherWorkload(
        days_since_start=days_since_start, consecutive_days=consecutive_days, recent_appearances=recent_appearances
    )


class StarterRestTest(TestCase):
    def test_five_days_of_rest_or_more_is_no_fatigue(self):
        """AI の6人ローテは中5日を割らないので、普段の先発は疲労0。"""
        for days in (MIN_DAYS_BETWEEN_STARTS, MIN_DAYS_BETWEEN_STARTS + 1, 30):
            self.assertEqual(starter_rest_points(days), 0, days)

    def test_a_pitcher_who_has_not_started_is_fresh(self):
        self.assertEqual(starter_rest_points(None), 0)

    def test_the_shorter_the_rest_the_heavier_the_fatigue(self):
        self.assertEqual(starter_rest_points(5), 3, "中4日")
        self.assertEqual(starter_rest_points(4), 6, "中3日")
        self.assertEqual(starter_rest_points(3), 10, "中2日")
        self.assertEqual(starter_rest_points(1), 10, "中2日より短くても、いちばん重い点のまま")
        points = [starter_rest_points(days) for days in range(1, MIN_DAYS_BETWEEN_STARTS + 1)]
        self.assertEqual(points, sorted(points, reverse=True))

    def test_rest_counts_only_when_the_pitcher_starts(self):
        self.assertEqual(pregame_fatigue_points(workload(5), as_starter=True, stamina=50), 3)
        self.assertEqual(pregame_fatigue_points(workload(5), as_starter=False, stamina=50), 0)

    def test_stamina_does_not_change_the_rest_points(self):
        """スタミナは先発の受け持ちの長さに効いているので、休養の点にはかけない（二重に効かせない）。"""
        for stamina in (1, 50, 100):
            self.assertEqual(pregame_fatigue_points(workload(5), as_starter=True, stamina=stamina), 3)


class InGameFatigueTest(TestCase):
    def test_no_fatigue_within_the_target_and_the_grace(self):
        """AI は目安＋猶予の手前で代えるので、普段の交代では試合中の疲労が付かない。"""
        target = 24
        for faced in range(target + MID_INNING_GRACE):
            self.assertEqual(in_game_fatigue_points(faced, target), 0, faced)

    def test_each_batter_past_the_target_and_the_grace_adds_a_point(self):
        target = 24
        threshold = target + MID_INNING_GRACE
        self.assertEqual(in_game_fatigue_points(threshold, target), 1)
        self.assertEqual(in_game_fatigue_points(threshold + 4, target), 5)


class FatiguedRatingsTest(TestCase):
    RATINGS = PitcherRatings(stuff=60, control=55, home_run_avoidance=50, stamina=70, growth=GrowthType.LATE)

    def test_no_fatigue_keeps_the_same_ratings(self):
        self.assertIs(fatigued(self.RATINGS, 0), self.RATINGS)

    def test_fatigue_lowers_stuff_control_and_avoidance_but_keeps_stamina_and_growth(self):
        tired = fatigued(self.RATINGS, 6)
        self.assertEqual((tired.stuff, tired.control, tired.home_run_avoidance), (54, 49, 44))
        self.assertEqual(tired.stamina, 70)
        self.assertIs(tired.growth, GrowthType.LATE)

    def test_fatigue_is_capped(self):
        self.assertEqual(fatigued(self.RATINGS, 100), fatigued(self.RATINGS, MAX_FATIGUE_POINTS))
        self.assertEqual(fatigued(self.RATINGS, MAX_FATIGUE_POINTS).stuff, 60 - MAX_FATIGUE_POINTS)

    def test_ratings_never_fall_below_one(self):
        weak = PitcherRatings(stuff=3, control=1, home_run_avoidance=10)
        tired = fatigued(weak, MAX_FATIGUE_POINTS)
        self.assertEqual((tired.stuff, tired.control, tired.home_run_avoidance), (1, 1, 1))

    def test_a_tired_pitcher_strikes_out_fewer_and_walks_and_gives_up_more_home_runs(self):
        """率への効き方は matchup の β のまま（新しい率の式を作らない）。"""
        batter = BatterRatings()
        fresh = matchup(batter, PitcherRatings())
        tired = matchup(batter, fatigued(PitcherRatings(), 10))
        self.assertLess(tired.strikeout, fresh.strikeout)
        self.assertGreater(tired.walk, fresh.walk)
        self.assertGreater(tired.home_run, fresh.home_run)
        self.assertEqual(tired.in_play_hit, fresh.in_play_hit, "インプレーの安打は守備と打者の能力だけで決まる")


class FatigueLevelTest(TestCase):
    def test_the_boundaries(self):
        self.assertIs(FatigueLevel.from_points(0), FatigueLevel.NONE)
        self.assertIs(FatigueLevel.from_points(1), FatigueLevel.LIGHT)
        self.assertIs(FatigueLevel.from_points(starter_rest_points(5)), FatigueLevel.LIGHT, "中4日は軽い")
        self.assertIs(FatigueLevel.from_points(starter_rest_points(4)), FatigueLevel.HEAVY, "中3日は重い")
        self.assertIs(FatigueLevel.from_points(MAX_FATIGUE_POINTS), FatigueLevel.HEAVY)

    def test_labels_are_japanese(self):
        self.assertEqual([level.label for level in FatigueLevel], ["なし", "軽い", "重い"])


class WorkloadTest(TestCase):
    def test_the_workload_comes_from_the_history(self):
        history = PitchingHistory()
        history.record(TODAY - timedelta(days=5), [1, 2])
        history.record(TODAY - timedelta(days=1), [3, 2])
        self.assertEqual(history.workload(1, TODAY), workload(5, recent_appearances=1))
        self.assertEqual(history.workload(2, TODAY), workload(None, consecutive_days=1, recent_appearances=2))
        self.assertEqual(history.workload(9, TODAY), workload(None))


# --- エンジン ---------------------------------------------------------------------------


def _single_pitcher_side(team_id: int):
    """投手が先発1人だけの球団。ブルペンが無いので、交代できずに最後まで投げる。"""
    roster = choose_active_roster(average_club(team_id), rules=ForeignPlayerRules.UNLIMITED)
    starter = roster.pitchers[0]
    staff = PitchingStaff(rotation=(starter,), closer=None, setup=(), middle=())
    return replace(roster, pitchers=(starter,)), ClubOrders(staff=staff), starter


class EngineFatigueTest(TestCase):
    def setUp(self):
        self.home, self.home_orders, self.starter = _single_pitcher_side(1)
        self.away = choose_active_roster(average_club(2), rules=ForeignPlayerRules.UNLIMITED)

    def _play(self, history: PitchingHistory | None, seed: int = 5) -> SimulatedGame:
        return simulate_game(
            make_random(seed),
            self.home,
            self.away,
            stage=GameStage.REGULAR,
            played_on=TODAY,
            history=history,
            home_orders=self.home_orders,
            foreign_rules=ForeignPlayerRules.UNLIMITED,
        )

    @staticmethod
    def _home_outing(played: SimulatedGame):
        return next(o for o in played.outings if o.team_id == 1)

    def test_a_starter_on_short_rest_pitches_tired_from_the_first_batter(self):
        history = PitchingHistory()
        history.record(TODAY - timedelta(days=3), [self.starter.player_id])
        played = self._play(history)

        outing = self._home_outing(played)
        self.assertEqual(outing.workload.days_since_start, 3)
        self.assertEqual(outing.fatigue_before, 10)
        faced_by_home = [
            f for pa, f in zip(played.plate_appearances, played.pitcher_fatigue, strict=True) if not pa.is_bottom
        ]
        self.assertEqual(faced_by_home[0], 10)

    def test_a_busy_reliever_pitches_tired_with_his_own_stamina(self):
        """救援の登板前の疲労は、その救援のスタミナで、救援として（先発の休養を数えずに）数える。"""
        roster = choose_active_roster(average_club(1), rules=ForeignPlayerRules.UNLIMITED)
        # 先発と救援でスタミナを大きく変えておく（取り違えると点が変わる）
        starter = replace(roster.pitchers[0], ratings=replace(roster.pitchers[0].ratings, stamina=90))
        reliever = replace(roster.pitchers[1], ratings=replace(roster.pitchers[1].ratings, stamina=30))
        staff = PitchingStaff(rotation=(starter,), closer=None, setup=(), middle=(reliever,))
        home = replace(roster, pitchers=(starter, reliever))
        history = PitchingHistory()
        # 4日前に先発（先発として数えると中3日の6点が付く）、3日前と前日に救援 → 窓の中で3日
        history.record(TODAY - timedelta(days=4), [reliever.player_id])
        history.record(TODAY - timedelta(days=3), [900, reliever.player_id])
        history.record(TODAY - timedelta(days=1), [901, reliever.player_id])
        expected = pregame_fatigue_points(
            history.workload(reliever.player_id, TODAY), as_starter=False, stamina=reliever.ratings.stamina
        )

        played = simulate_game(
            make_random(5),
            home,
            self.away,
            stage=GameStage.REGULAR,
            played_on=TODAY,
            history=history,
            home_orders=ClubOrders(staff=staff),
            foreign_rules=ForeignPlayerRules.UNLIMITED,
        )

        (outing,) = [o for o in played.outings if o.pitcher_id == reliever.player_id]
        self.assertFalse(outing.is_starter)
        self.assertEqual(expected, 3, "2点 × スタミナ30の係数1.4 = 2.8")
        self.assertEqual(outing.fatigue_before, expected)

    def test_the_tired_start_plays_out_differently_with_the_same_random_numbers(self):
        """疲労が打席の確率まで届いていること（同じ乱数でも打席の列が食い違う）。"""
        rested = PitchingHistory()
        rested.record(TODAY - timedelta(days=10), [self.starter.player_id])
        tired = PitchingHistory()
        tired.record(TODAY - timedelta(days=3), [self.starter.player_id])

        self.assertNotEqual(self._play(rested).plate_appearances, self._play(tired).plate_appearances)

    def test_without_history_there_is_no_rest_fatigue(self):
        played = self._play(None)
        self.assertEqual(self._home_outing(played).fatigue_before, 0)

    def test_staying_in_past_the_planned_target_adds_fatigue_batter_by_batter(self):
        """ブルペンが尽きて目安を延ばしても、試合中の疲労は当初の目安で数える。"""
        played = self._play(None)
        outing = self._home_outing(played)
        self.assertGreater(outing.faced, outing.planned_target + MID_INNING_GRACE, "投手1人なので最後まで投げる")

        faced_by_home = [
            f for pa, f in zip(played.plate_appearances, played.pitcher_fatigue, strict=True) if not pa.is_bottom
        ]
        expected = [
            min(MAX_FATIGUE_POINTS, in_game_fatigue_points(faced, outing.planned_target))
            for faced in range(len(faced_by_home))
        ]
        self.assertEqual(faced_by_home, expected)
        self.assertGreater(faced_by_home[-1], 0)


def _season(orders_for: dict[int, ClubOrders] | None = None, days: int = 28):
    """能力を散らした6球団で、毎日1試合ずつ回す（AI の6人ローテなら中5日を割らない）。"""
    rng = make_random(20260801)
    rosters = {
        club.team_id: choose_active_roster(club, rules=ForeignPlayerRules.NPB) for club in spread_league(rng, 6)
    }
    orders_for = orders_for or {}
    history = PitchingHistory()
    usage = UsageTally()
    games: list[SimulatedGame] = []
    for day_index, day in enumerate(round_robin_days(list(rosters), days)):
        for home_id, away_id in day:
            played = simulate_game(
                make_random(game_seed(1, 2026, f"{day_index}-{home_id}")),
                rosters[home_id],
                rosters[away_id],
                stage=GameStage.REGULAR,
                played_on=OPENING_DAY + timedelta(days=day_index),
                history=history,
                home_orders=orders_for.get(home_id),
                away_orders=orders_for.get(away_id),
                foreign_rules=ForeignPlayerRules.UNLIMITED,
            )
            usage.add(played)
            games.append(played)
    return rosters, games, usage


class UsualUsageTest(TestCase):
    """AI の普段の起用: 先発に疲労は付かず、重い疲労はほとんど無い。

    救援の詰まった登板には軽い疲労が付く（打席の約5%。段階2）。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.rosters, cls.games, cls.usage = _season()

    def test_ai_starters_are_never_tired_before_the_game(self):
        starts = [o for g in self.games for o in g.outings if o.is_starter]
        self.assertGreater(len(starts), 100)
        self.assertTrue(all(o.fatigue_before == 0 for o in starts))

    def test_heavy_fatigue_is_rare_and_light_fatigue_comes_from_busy_relievers(self):
        """AI は重い救援を後回しにするので、重い疲労はほとんど無い。軽い疲労は救援の詰まった登板で付く。"""
        rows = {row.key: row.value for row in self.usage.rows()}
        self.assertEqual(rows["short_rest_starts"], 0.0)
        self.assertLess(rows["heavy_fatigue"], 0.01)
        self.assertGreater(rows["light_fatigue"], 0.01, "救援の詰まった登板（5日で3日目など）に軽い疲労が付く")
        self.assertLess(rows["light_fatigue"], 0.15)

    def test_only_high_stamina_relievers_pitch_three_straight_days(self):
        stamina = {p.player_id: p.ratings.stamina for r in self.rosters.values() for p in r.pitchers}
        three_day = [o for g in self.games for o in g.outings if o.workload.consecutive_days == 2]
        self.assertTrue(three_day, "スタミナ55以上の救援は3連投目も投げる")
        self.assertTrue(all(stamina[o.pitcher_id] >= HIGH_STAMINA_RELIEVER for o in three_day))


class FiveManRotationTest(TestCase):
    """手動の5人ローテは、毎日の試合で中4日の先発が出て、疲労（軽い）が付く。"""

    def test_a_five_man_rotation_pitches_on_short_rest_and_tired(self):
        rng = make_random(20260801)
        roster = choose_active_roster(spread_league(rng, 6)[0], rules=ForeignPlayerRules.NPB)
        automatic = plan_pitching_staff(roster.pitchers)
        five = ClubOrders(staff=plan_pitching_staff(roster.pitchers, rotation=automatic.rotation[:5]))

        _, games, usage = _season({roster.team_id: five})

        rows = {row.key: row.value for row in usage.rows()}
        self.assertGreater(rows["short_rest_starts"], 0.0)
        tired = [
            o
            for g in games
            for o in g.outings
            if o.team_id == roster.team_id and o.is_starter and o.fatigue_before > 0
        ]
        self.assertTrue(tired)
        self.assertTrue(all(o.workload.days_since_start == MIN_DAYS_BETWEEN_STARTS - 1 for o in tired), "中4日")


class UsageTallyTest(TestCase):
    def test_three_days_in_a_row_are_counted_on_the_third_day(self):
        usage = UsageTally()
        day = date(2026, 4, 1)
        usage.pitched_days = {
            1: {day, day + timedelta(days=1), day + timedelta(days=2), day + timedelta(days=3)},  # 4連投 → 2
            2: {day, day + timedelta(days=1), day + timedelta(days=3)},  # 2連投まで → 0
        }
        self.assertEqual(usage.three_day_streaks, 2)

    def test_an_empty_tally_reads_zero(self):
        self.assertTrue(all(row.value == 0.0 for row in UsageTally().rows()))


# --- 救援の連投と直近の登板（段階2） -------------------------------------------------------


class ReliefLoadPointsTest(TestCase):
    def test_up_to_two_straight_days_is_no_fatigue_and_the_third_is(self):
        self.assertEqual([consecutive_points(days) for days in (0, 1)], [0, 0])
        self.assertEqual(consecutive_points(2), 5, "3連投目")

    def test_up_to_two_appearances_in_the_window_is_no_fatigue(self):
        self.assertEqual([recent_appearance_points(days) for days in (0, 1, 2)], [0, 0, 0])
        self.assertEqual(recent_appearance_points(3), 2)
        self.assertEqual(recent_appearance_points(4), 4)
        self.assertEqual(recent_appearance_points(FATIGUE_WINDOW_DAYS), 4)

    def test_the_stamina_factor_is_one_at_fifty_and_clamped(self):
        self.assertEqual(stamina_factor(50), 1.0)
        self.assertEqual(stamina_factor(100), 0.5)
        self.assertEqual(stamina_factor(1), 1.5)
        factors = [stamina_factor(stamina) for stamina in range(1, 101)]
        self.assertEqual(factors, sorted(factors, reverse=True), "スタミナが高いほど軽い")

    def test_relief_points_are_scaled_by_stamina_and_rounded_half_up(self):
        def points(stamina, consecutive=0, recent=0):
            return pregame_fatigue_points(
                workload(None, consecutive_days=consecutive, recent_appearances=recent),
                as_starter=False,
                stamina=stamina,
            )

        self.assertEqual(points(36, recent=3), 3, "2 × 1.28 = 2.56")
        self.assertEqual(points(55, consecutive=2, recent=2), 5, "5 × 0.9 = 4.5 は切り上げ")
        self.assertEqual(points(55, consecutive=2, recent=3), 6, "(5 + 2) × 0.9 = 6.3")
        self.assertIs(FatigueLevel.from_points(points(55, consecutive=2, recent=3)), FatigueLevel.HEAVY)
        self.assertEqual(points(50, recent=2), 0, "2連投・5日で2日までは普段の起用")

    def test_a_starter_gets_both_rest_and_load_points(self):
        self.assertEqual(pregame_fatigue_points(workload(5, recent_appearances=3), as_starter=True, stamina=50), 3 + 2)


class ConsecutiveDayLimitTest(TestCase):
    def test_only_high_stamina_pitchers_may_pitch_a_third_straight_day(self):
        self.assertEqual(max_consecutive_days(HIGH_STAMINA_RELIEVER - 1), 2)
        self.assertEqual(max_consecutive_days(HIGH_STAMINA_RELIEVER), 3)
        self.assertEqual(max_consecutive_days(100), 3)

    def test_can_pitch_on_follows_the_stamina_limit(self):
        history = PitchingHistory()
        for back in (1, 2):
            history.record(TODAY - timedelta(days=back), [1])
        self.assertFalse(history.can_pitch_on(1, TODAY, stamina=HIGH_STAMINA_RELIEVER - 1))
        self.assertTrue(history.can_pitch_on(1, TODAY, stamina=HIGH_STAMINA_RELIEVER))
        history.record(TODAY - timedelta(days=3), [1])
        self.assertFalse(history.can_pitch_on(1, TODAY, stamina=100), "4連投はスタミナが高くてもしない")


class WindowTest(TestCase):
    """窓の外の登板は数えない（1日ずつ進めても、まとめて進めても同じ答えにするため）。"""

    def test_the_edge_of_the_window_counts_and_the_day_before_does_not(self):
        history = PitchingHistory()
        history.record(TODAY - timedelta(days=FATIGUE_WINDOW_DAYS), [1])
        history.record(TODAY - timedelta(days=FATIGUE_WINDOW_DAYS + 1), [2])
        self.assertEqual(history.workload(1, TODAY).recent_appearances, 1)
        self.assertEqual(history.workload(2, TODAY).recent_appearances, 0)

    def test_the_streak_is_counted_inside_the_window(self):
        history = PitchingHistory()
        for back in range(1, FATIGUE_WINDOW_DAYS + 3):
            history.record(TODAY - timedelta(days=back), [1])
        self.assertEqual(history.consecutive_days_before(1, TODAY), FATIGUE_WINDOW_DAYS)


class TiredRelieverChoiceTest(TestCase):
    """AI は疲労が重い救援を、ほかに投げられる投手がいる限り出さない。"""

    def setUp(self):
        staff = plan_pitching_staff(choose_active_roster(average_club(1), rules=ForeignPlayerRules.UNLIMITED).pitchers)
        # 抑えだけスタミナを下げる（窓の中で4日投げると 4 × 1.5 = 6点で重い）
        assert staff.closer is not None
        self.closer = replace(staff.closer, ratings=replace(staff.closer.ratings, stamina=25))
        self.staff = replace(staff, closer=self.closer)
        self.history = PitchingHistory()
        for back in (1, 3, 4, 5):
            self.history.record(TODAY - timedelta(days=back), [900, self.closer.player_id])

    def test_a_heavily_tired_closer_is_left_out_while_others_can_pitch(self):
        self.assertTrue(self.history.can_pitch_on(self.closer.player_id, TODAY, stamina=25))
        available = available_relievers(self.staff, set(), self.history, TODAY, ForeignQuota())
        self.assertNotIn(self.closer, available)
        self.assertTrue(available)

    def test_when_everyone_else_is_used_the_tired_closer_still_pitches(self):
        used = {p.player_id for p in self.staff.bullpen if p is not self.closer}
        self.assertEqual(available_relievers(self.staff, used, self.history, TODAY, ForeignQuota()), [self.closer])

    def test_a_lightly_tired_reliever_is_chosen_as_usual(self):
        history = PitchingHistory()
        for back in (1, 3, 4):
            history.record(TODAY - timedelta(days=back), [900, self.closer.player_id])  # 3日 → 2 × 1.5 = 3点
        self.assertIn(self.closer, available_relievers(self.staff, set(), history, TODAY, ForeignQuota()))
