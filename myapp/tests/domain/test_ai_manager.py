"""AI 監督の単体テスト。Django も DB も使わない。

1軍登録（29人・捕手2人以上・外国人の登録枠）、スタメン、ローテーション（中5日）、
継投（抑えはセーブの状況で）、救援の連投（3連投禁止）、代打の条件、外国人の出場枠。
"""

from datetime import date, timedelta
from typing import Any
from unittest import TestCase

from myapp.domain.exceptions import InvalidRoster
from myapp.domain.services import SAVE_LEAD_LIMIT
from myapp.domain.simulation.manager import (
    ACTIVE_PITCHERS,
    ACTIVE_ROSTER_SIZE,
    MAX_PINCH_HITTERS,
    MIN_DAYS_BETWEEN_STARTS,
    STARTER_BATTERS,
    ClubRoster,
    ForeignQuota,
    PinchHitSituation,
    PitchingHistory,
    SimBatter,
    SimPitcher,
    available_relievers,
    choose_active_roster,
    choose_lineup,
    choose_pinch_hitter,
    choose_starter,
    closer_should_enter,
    pick_reliever,
    plan_pitching_staff,
    plays_position,
    reliever_batter_target,
    should_change_pitcher,
    starter_batter_target,
)
from myapp.domain.simulation.randomness import make_random
from myapp.domain.simulation.ratings import BatterRatings, PitcherRatings
from myapp.domain.value_objects import FieldingPosition, Position

FP = FieldingPosition
TODAY = date(2026, 6, 10)


def batter(player_id, position=Position.INFIELDER, *, foreign=False, **ratings) -> SimBatter:
    return SimBatter(player_id, f"野手{player_id}", position, BatterRatings(**ratings), foreign)


def pitcher(player_id, *, foreign=False, **ratings) -> SimPitcher:
    return SimPitcher(player_id, f"投手{player_id}", PitcherRatings(**ratings), foreign)


def full_pool(*, foreign_ids=(), foreign_catcher=False) -> ClubRoster:
    """捕手3・内野8・外野7・指名打者2の野手20人と、投手19人。能力は番号が若いほど高い。"""
    positions = (
        [Position.CATCHER] * 3
        + [Position.INFIELDER] * 8
        + [Position.OUTFIELDER] * 7
        + [Position.DESIGNATED_HITTER] * 2
    )
    batters = []
    for number, position in enumerate(positions, start=1):
        value = 80 - number
        # 捕手は最も弱くしておく（2人は必ず入る規則を確かめるため）
        if position is Position.CATCHER:
            value = 30 - number
        batters.append(
            batter(
                number,
                position,
                foreign=number in foreign_ids,
                contact=value,
                power=value,
                eye=value,
                speed=value,
                fielding=value,
            )
        )
    pitchers = [
        pitcher(
            100 + number,
            foreign=100 + number in foreign_ids,
            stuff=80 - number,
            control=80 - number,
            home_run_avoidance=80 - number,
            stamina=70 - number,
        )
        for number in range(1, 20)
    ]
    return ClubRoster(1, "球団", tuple(batters), tuple(pitchers))


class ActiveRosterTest(TestCase):
    def test_the_active_roster_has_29_players_with_14_pitchers(self):
        active = choose_active_roster(full_pool())
        self.assertEqual(len(active.batters) + len(active.pitchers), ACTIVE_ROSTER_SIZE)
        self.assertEqual(len(active.pitchers), ACTIVE_PITCHERS)

    def test_at_least_two_catchers_are_registered_even_when_they_are_the_weakest(self):
        active = choose_active_roster(full_pool())
        catchers = [b for b in active.batters if b.position is Position.CATCHER]
        self.assertGreaterEqual(len(catchers), 2)

    def test_the_best_players_are_registered(self):
        pool = full_pool()
        active = choose_active_roster(pool)
        best_pitchers = {p.player_id for p in sorted(pool.pitchers, key=lambda p: -p.ratings.stuff)[:14]}
        self.assertEqual({p.player_id for p in active.pitchers}, best_pitchers)

    def test_foreign_players_are_capped_at_the_registration_limit(self):
        pool = full_pool(foreign_ids=(4, 5, 6, 7, 8, 101, 102, 103))
        active = choose_active_roster(pool, foreign_roster_limit=4)
        foreign = [p for p in (*active.batters, *active.pitchers) if p.is_foreign]
        self.assertEqual(len(foreign), 4)
        self.assertEqual(len(active.batters) + len(active.pitchers), ACTIVE_ROSTER_SIZE)

    def test_the_catcher_minimum_survives_the_foreign_cap(self):
        pool = full_pool(foreign_ids=(1, 2))  # 捕手3人のうち強い2人が外国人
        active = choose_active_roster(pool, foreign_roster_limit=1)
        catchers = [b for b in active.batters if b.position is Position.CATCHER]
        self.assertGreaterEqual(len(catchers), 2)
        self.assertEqual(sum(b.is_foreign for b in (*active.batters, *active.pitchers)), 1)
        self.assertEqual(len(active.batters) + len(active.pitchers), ACTIVE_ROSTER_SIZE)

    def test_no_cap_means_all_the_best_foreign_players_stay(self):
        pool = full_pool(foreign_ids=(4, 5, 6, 7, 8))
        active = choose_active_roster(pool, foreign_roster_limit=None)
        self.assertEqual(sum(b.is_foreign for b in active.batters), 5)

    def test_a_small_pool_registers_everyone(self):
        pool = ClubRoster(
            1, "少数", tuple(batter(n) for n in range(1, 12)), tuple(pitcher(100 + n) for n in range(1, 8))
        )
        active = choose_active_roster(pool)
        self.assertEqual((len(active.batters), len(active.pitchers)), (11, 7))


class LineupTest(TestCase):
    def setUp(self):
        self.active = choose_active_roster(full_pool())

    def test_the_lineup_has_nine_distinct_players_in_nine_distinct_positions(self):
        lineup = choose_lineup(self.active.batters, ForeignQuota())
        self.assertEqual(len(lineup), 9)
        self.assertEqual(len({slot.batter.player_id for slot in lineup}), 9)
        self.assertEqual(
            {slot.position for slot in lineup},
            {
                FP.CATCHER,
                FP.FIRST_BASE,
                FP.SECOND_BASE,
                FP.THIRD_BASE,
                FP.SHORTSTOP,
                FP.LEFT_FIELD,
                FP.CENTER_FIELD,
                FP.RIGHT_FIELD,
                FP.DESIGNATED_HITTER,
            },
        )

    def test_fielders_play_positions_that_match_their_registration(self):
        for slot in choose_lineup(self.active.batters, ForeignQuota()):
            self.assertTrue(plays_position(slot.batter, slot.position), f"{slot}")

    def test_the_best_defender_among_infielders_plays_shortstop(self):
        infielders = [
            batter(1, Position.INFIELDER, fielding=40, contact=60),
            batter(2, Position.INFIELDER, fielding=90, contact=50),
            batter(3, Position.INFIELDER, fielding=60, contact=50),
            batter(4, Position.INFIELDER, fielding=50, contact=50),
        ]
        rest = [
            batter(10, Position.CATCHER),
            batter(11, Position.OUTFIELDER),
            batter(12, Position.OUTFIELDER),
            batter(13, Position.OUTFIELDER),
            batter(14, Position.DESIGNATED_HITTER),
        ]
        lineup = choose_lineup(infielders + rest, ForeignQuota())
        shortstop = next(slot for slot in lineup if slot.position is FP.SHORTSTOP)
        self.assertEqual(shortstop.batter.player_id, 2)

    def test_the_designated_hitter_is_the_best_remaining_hitter(self):
        batters = [
            batter(10, Position.CATCHER),
            *[batter(20 + n, Position.INFIELDER, contact=45, power=45, eye=45, speed=45) for n in range(4)],
            *[batter(30 + n, Position.OUTFIELDER) for n in range(3)],
            batter(40, Position.DESIGNATED_HITTER, contact=60, power=60, eye=60, speed=60),
            batter(41, Position.INFIELDER, contact=20, power=20, eye=20, speed=20, fielding=20),
            batter(42, Position.DESIGNATED_HITTER, contact=75, power=75, eye=75, speed=60),
        ]
        lineup = choose_lineup(batters, ForeignQuota())
        designated = next(slot for slot in lineup if slot.position is FP.DESIGNATED_HITTER)
        self.assertEqual(designated.batter.player_id, 42)

    def test_the_batting_order_puts_on_base_hitters_first_and_sluggers_in_the_middle(self):
        batters = [
            batter(1, Position.CATCHER),
            batter(2, Position.INFIELDER, contact=70, eye=80, speed=75, power=30),  # 出塁型
            batter(3, Position.INFIELDER, contact=65, eye=70, speed=60, power=30),
            batter(4, Position.INFIELDER, power=90, contact=60, eye=50, speed=40),  # 長打型
            batter(5, Position.INFIELDER, power=80, contact=55, eye=50, speed=40),
            batter(6, Position.OUTFIELDER, power=40, contact=40, eye=40, speed=40),
            batter(7, Position.OUTFIELDER, power=40, contact=40, eye=40, speed=40),
            batter(8, Position.OUTFIELDER, power=40, contact=40, eye=40, speed=40),
            batter(9, Position.DESIGNATED_HITTER, power=40, contact=40, eye=40, speed=40),
        ]
        order = [slot.batter.player_id for slot in choose_lineup(batters, ForeignQuota())]
        self.assertEqual(order[0], 2)
        self.assertEqual(order[1], 3)
        # 3番は総合、4・5番は長打。長打型の2人は中軸（3〜5番）に入る
        self.assertTrue({4, 5} <= set(order[2:5]))

    def test_foreign_players_in_the_lineup_respect_the_game_limit(self):
        pool = full_pool(foreign_ids=tuple(range(4, 12)))
        active = choose_active_roster(pool, foreign_roster_limit=4)
        for limit in (0, 1, 2, 3):
            lineup = choose_lineup(active.batters, ForeignQuota(limit=limit))
            self.assertLessEqual(sum(slot.batter.is_foreign for slot in lineup), limit, f"limit={limit}")

    def test_the_quota_already_used_by_the_starter_is_counted(self):
        pool = full_pool(foreign_ids=tuple(range(4, 12)))
        active = choose_active_roster(pool, foreign_roster_limit=4)
        quota = ForeignQuota(limit=2, used=1)
        lineup = choose_lineup(active.batters, quota)
        self.assertLessEqual(sum(slot.batter.is_foreign for slot in lineup), 1)

    def test_fewer_than_nine_batters_cannot_make_a_lineup(self):
        with self.assertRaises(InvalidRoster):
            choose_lineup([batter(n) for n in range(1, 9)], ForeignQuota())

    def test_a_shortage_of_outfielders_is_filled_from_the_other_batters(self):
        batters = [
            batter(1, Position.CATCHER),
            *[batter(10 + n, Position.INFIELDER) for n in range(6)],
            batter(30, Position.OUTFIELDER),
            batter(31, Position.DESIGNATED_HITTER),
            batter(32, Position.DESIGNATED_HITTER),
        ]
        lineup = choose_lineup(batters, ForeignQuota())
        self.assertEqual(len({slot.position for slot in lineup}), 9)

    def test_the_catcher_is_always_a_registered_catcher_when_one_exists(self):
        lineup = choose_lineup(self.active.batters, ForeignQuota())
        catcher = next(slot for slot in lineup if slot.position is FP.CATCHER)
        self.assertIs(catcher.batter.position, Position.CATCHER)


class RotationTest(TestCase):
    def setUp(self):
        self.active = choose_active_roster(full_pool())
        self.staff = plan_pitching_staff(self.active.pitchers)

    def test_six_starters_a_closer_two_setup_men_and_the_rest_in_the_middle(self):
        self.assertEqual(len(self.staff.rotation), 6)
        self.assertIsNotNone(self.staff.closer)
        self.assertEqual(len(self.staff.setup), 2)
        self.assertEqual(len(self.staff.rotation) + 1 + len(self.staff.setup) + len(self.staff.middle), 14)
        in_rotation = {p.player_id for p in self.staff.rotation}
        self.assertNotIn(self.staff.closer.player_id, in_rotation)

    def test_the_closer_is_the_best_pitcher_outside_the_rotation(self):
        others = [p for p in self.active.pitchers if p not in self.staff.rotation]
        best = max(others, key=lambda p: p.ratings.pitching_value)
        self.assertEqual(self.staff.closer, best)

    def test_starters_are_spaced_at_least_five_days_apart(self):
        """中5日以上。毎日試合のある30日間に、同じ投手が6日未満で先発しない。"""
        history = PitchingHistory()
        started: dict[int, list[int]] = {}
        for day in range(30):
            today = TODAY + timedelta(days=day)
            starter = choose_starter(self.staff, history, today, ForeignQuota())
            started.setdefault(starter.player_id, []).append(day)
            history.record(today, [starter.player_id])
        for player_id, days in started.items():
            gaps = [later - earlier for earlier, later in zip(days, days[1:], strict=False)]
            self.assertTrue(all(gap >= MIN_DAYS_BETWEEN_STARTS for gap in gaps), f"{player_id}: {days}")
        # 6人が順番に回っている
        self.assertEqual(len(started), 6)

    def test_a_pitcher_who_started_yesterday_is_not_chosen(self):
        history = PitchingHistory()
        first = choose_starter(self.staff, history, TODAY, ForeignQuota())
        history.record(TODAY, [first.player_id])
        second = choose_starter(self.staff, history, TODAY + timedelta(days=1), ForeignQuota())
        self.assertNotEqual(first, second)

    def test_when_nobody_is_rested_the_longest_rested_pitcher_starts(self):
        history = PitchingHistory()
        for offset, starter in enumerate(self.staff.rotation):
            history.record(TODAY - timedelta(days=2 + offset), [starter.player_id])
        # 全員が6日未満のとき（最も前に投げた投手は2+5=7日前なので、これは6日以上）
        chosen = choose_starter(self.staff, history, TODAY, ForeignQuota())
        self.assertEqual(chosen, self.staff.rotation[-1])

    def test_a_foreign_starter_is_skipped_when_the_quota_is_full(self):
        foreign_ace = pitcher(500, foreign=True, stuff=99, control=99, home_run_avoidance=99, stamina=99)
        staff = plan_pitching_staff((foreign_ace, *self.active.pitchers[:13]))
        self.assertIn(foreign_ace, staff.rotation)
        chosen = choose_starter(staff, PitchingHistory(), TODAY, ForeignQuota(limit=1, used=1))
        self.assertNotEqual(chosen, foreign_ace)
        allowed = choose_starter(staff, PitchingHistory(), TODAY, ForeignQuota(limit=1, used=0))
        self.assertEqual(allowed, foreign_ace)

    def test_no_pitchers_cannot_make_a_staff(self):
        with self.assertRaises(InvalidRoster):
            plan_pitching_staff(())


class BullpenAvailabilityTest(TestCase):
    def setUp(self):
        self.staff = plan_pitching_staff(choose_active_roster(full_pool()).pitchers)
        self.closer = self.staff.closer

    def test_a_reliever_who_pitched_the_two_previous_days_cannot_pitch(self):
        history = PitchingHistory()
        history.record(TODAY - timedelta(days=1), [900, self.closer.player_id])
        history.record(TODAY - timedelta(days=2), [901, self.closer.player_id])
        available = available_relievers(self.staff, set(), history, TODAY, ForeignQuota())
        self.assertNotIn(self.closer, available)

    def test_one_or_two_nonconsecutive_days_are_fine(self):
        history = PitchingHistory()
        history.record(TODAY - timedelta(days=1), [900, self.closer.player_id])
        history.record(TODAY - timedelta(days=3), [901, self.closer.player_id])
        self.assertIn(self.closer, available_relievers(self.staff, set(), history, TODAY, ForeignQuota()))

    def test_two_days_ago_and_the_day_before_that_still_leaves_yesterday_free(self):
        history = PitchingHistory()
        history.record(TODAY - timedelta(days=2), [900, self.closer.player_id])
        history.record(TODAY - timedelta(days=3), [901, self.closer.player_id])
        self.assertIn(self.closer, available_relievers(self.staff, set(), history, TODAY, ForeignQuota()))

    def test_pitchers_already_used_in_the_game_are_not_available(self):
        available = available_relievers(self.staff, {self.closer.player_id}, PitchingHistory(), TODAY, ForeignQuota())
        self.assertNotIn(self.closer, available)

    def test_the_foreign_quota_excludes_foreign_relievers(self):
        foreign = pitcher(600, foreign=True, stuff=90, control=90, home_run_avoidance=90)
        staff = plan_pitching_staff((*self.staff.rotation, foreign, *self.staff.middle))
        available = available_relievers(staff, set(), PitchingHistory(), TODAY, ForeignQuota(limit=1, used=1))
        self.assertNotIn(foreign, available)

    def test_when_the_whole_bullpen_is_used_up_nobody_is_available_and_the_pitcher_stays(self):
        used = {p.player_id for p in self.staff.bullpen}
        self.assertEqual(available_relievers(self.staff, used, PitchingHistory(), TODAY, ForeignQuota()), [])

    def test_a_roster_without_a_bullpen_falls_back_to_the_unused_starters(self):
        staff = plan_pitching_staff(self.staff.rotation)
        self.assertEqual(staff.bullpen, ())
        starter = staff.rotation[0]
        available = available_relievers(staff, {starter.player_id}, PitchingHistory(), TODAY, ForeignQuota())
        self.assertEqual(set(available), set(staff.rotation) - {starter})

    def test_history_counts_consecutive_days_before_today(self):
        history = PitchingHistory()
        self.assertEqual(history.consecutive_days_before(1, TODAY), 0)
        for back in (1, 2, 4):
            history.record(TODAY - timedelta(days=back), [9, 1])
        self.assertEqual(history.consecutive_days_before(1, TODAY), 2)
        self.assertEqual(history.days_since_start(9, TODAY), 1)
        self.assertIsNone(history.days_since_start(1, TODAY))


class RelieverChoiceTest(TestCase):
    def setUp(self):
        self.staff = plan_pitching_staff(choose_active_roster(full_pool()).pitchers)
        self.available = list(self.staff.bullpen)
        self.rng = make_random(1)

    def pick(self, *, inning, lead, is_home=True, available=None):
        return pick_reliever(
            self.rng,
            self.staff,
            self.available if available is None else available,
            inning=inning,
            lead=lead,
            is_home=is_home,
        )

    def test_the_closer_pitches_the_ninth_inning_of_a_save_situation(self):
        for lead in range(1, SAVE_LEAD_LIMIT + 1):
            self.assertEqual(self.pick(inning=9, lead=lead), self.staff.closer, f"lead={lead}")

    def test_the_closer_is_not_used_without_a_save_situation(self):
        self.assertNotEqual(self.pick(inning=9, lead=SAVE_LEAD_LIMIT + 1), self.staff.closer)
        self.assertNotEqual(self.pick(inning=9, lead=-1), self.staff.closer)
        self.assertNotEqual(self.pick(inning=8, lead=1), self.staff.closer)

    def test_the_home_team_uses_the_closer_in_a_tied_ninth_but_the_visitor_does_not(self):
        self.assertEqual(self.pick(inning=9, lead=0, is_home=True), self.staff.closer)
        self.assertNotEqual(self.pick(inning=9, lead=0, is_home=False), self.staff.closer)

    def test_the_closer_is_also_used_in_extra_innings(self):
        self.assertEqual(self.pick(inning=11, lead=1), self.staff.closer)

    def test_without_the_closer_the_best_other_reliever_takes_the_save_situation(self):
        others = [p for p in self.available if p != self.staff.closer]
        chosen = self.pick(inning=9, lead=1, available=others)
        self.assertEqual(chosen, max(others, key=lambda p: p.ratings.pitching_value))

    def test_a_close_late_game_uses_the_best_non_closer(self):
        non_closer = [p for p in self.available if p != self.staff.closer]
        best = max(non_closer, key=lambda p: p.ratings.pitching_value)
        self.assertEqual(self.pick(inning=8, lead=1), best)
        self.assertEqual(self.pick(inning=8, lead=0), best)

    def test_a_blowout_is_finished_by_the_weakest_pitcher(self):
        weakest = min(self.available, key=lambda p: p.ratings.pitching_value)
        self.assertEqual(self.pick(inning=8, lead=7), weakest)
        self.assertEqual(self.pick(inning=8, lead=-7), weakest)

    def test_no_available_pitcher_means_no_change(self):
        self.assertIsNone(self.pick(inning=9, lead=1, available=[]))

    def test_the_closer_enters_at_the_start_of_a_save_inning_regardless_of_the_target(self):
        starter = self.staff.rotation[0]
        middle = self.staff.middle[0]
        self.assertTrue(closer_should_enter(self.staff, middle, inning=9, lead=2))
        self.assertFalse(closer_should_enter(self.staff, middle, inning=8, lead=2))
        self.assertFalse(closer_should_enter(self.staff, middle, inning=9, lead=0))
        self.assertFalse(closer_should_enter(self.staff, middle, inning=9, lead=SAVE_LEAD_LIMIT + 1))
        self.assertFalse(closer_should_enter(self.staff, self.staff.closer, inning=9, lead=2))
        self.assertTrue(closer_should_enter(self.staff, starter, inning=9, lead=1))


class ChangePitcherTest(TestCase):
    def change(self, **overrides):
        values: dict[str, Any] = {
            "is_starter": True,
            "faced": 10,
            "target": 24,
            "runs_allowed": 0,
            "at_inning_start": True,
        }
        values.update(overrides)
        return should_change_pitcher(**values)

    def test_a_pitcher_below_the_target_stays(self):
        self.assertFalse(self.change())

    def test_at_the_target_the_pitcher_is_changed_at_an_inning_break(self):
        self.assertTrue(self.change(faced=24))

    def test_in_mid_inning_the_pitcher_is_given_a_little_more_time(self):
        self.assertFalse(self.change(faced=24, at_inning_start=False))
        self.assertFalse(self.change(faced=25, at_inning_start=False))
        self.assertTrue(self.change(faced=26, at_inning_start=False))

    def test_a_starter_who_is_hit_hard_is_changed_earlier(self):
        self.assertFalse(self.change(faced=18, runs_allowed=2))
        self.assertTrue(self.change(faced=20, runs_allowed=4))  # 目標 24 − 4
        self.assertTrue(self.change(faced=22, runs_allowed=3))  # 目標 24 − 2

    def test_a_knocked_out_starter_is_changed_even_in_the_middle_of_an_inning(self):
        self.assertTrue(self.change(faced=9, runs_allowed=5, at_inning_start=False))
        self.assertFalse(self.change(faced=8, runs_allowed=5, at_inning_start=False))

    def test_a_reliever_who_allows_three_runs_is_changed_at_once(self):
        self.assertTrue(self.change(is_starter=False, faced=2, target=4, runs_allowed=3, at_inning_start=False))
        self.assertFalse(self.change(is_starter=False, faced=2, target=4, runs_allowed=2, at_inning_start=False))


class BatterTargetTest(TestCase):
    """受け持ちの目安。エースには長く任せ、救援は1回を投げ切れる人数にする。"""

    SAMPLES = 400

    def mean_starter_target(self, **ratings) -> float:
        rng = make_random(7)
        starter = pitcher(1, **ratings)
        return sum(starter_batter_target(rng, starter) for _ in range(self.SAMPLES)) / self.SAMPLES

    def test_a_starter_with_a_higher_pitching_value_is_left_in_longer(self):
        strong = self.mean_starter_target(stuff=70, control=70, home_run_avoidance=70)
        average = self.mean_starter_target()
        weak = self.mean_starter_target(stuff=30, control=30, home_run_avoidance=30)
        self.assertGreater(strong, average + 4)
        self.assertGreater(average, weak + 4)

    def test_an_average_starter_is_left_in_for_the_baseline_number_of_batters(self):
        self.assertAlmostEqual(self.mean_starter_target(), STARTER_BATTERS, delta=0.6)

    def test_stamina_still_lengthens_the_outing(self):
        self.assertGreater(self.mean_starter_target(stamina=80), self.mean_starter_target(stamina=20) + 8)

    def test_a_reliever_is_left_in_for_about_one_inning(self):
        """3アウトを取るのに要る打者はおよそ4.4人。目安が小さいと、抑えが回の途中で代わって、セーブがつかなくなる。"""
        rng = make_random(7)
        relief = pitcher(2)
        mean = sum(reliever_batter_target(rng, relief) for _ in range(self.SAMPLES)) / self.SAMPLES
        self.assertGreaterEqual(mean, 4.0)
        self.assertLessEqual(mean, 5.0)


class PinchHitTest(TestCase):
    def situation(self, **overrides) -> PinchHitSituation:
        values: dict[str, Any] = {
            "inning": 8,
            "lead": -2,
            "pinch_hitters_used": 0,
            "current": batter(1, Position.OUTFIELDER, contact=40, power=40, eye=40, speed=40),
            "position": FP.LEFT_FIELD,
            "bench": (batter(2, Position.OUTFIELDER, contact=70, power=70, eye=70, speed=50),),
            "quota": ForeignQuota(),
        }
        values.update(overrides)
        return PinchHitSituation(**values)

    def test_a_pinch_hitter_is_sent_when_all_three_conditions_hold(self):
        decision = choose_pinch_hitter(self.situation())
        self.assertIsNotNone(decision)
        self.assertEqual(decision.batter.player_id, 2)

    def test_not_before_the_seventh_inning(self):
        self.assertIsNone(choose_pinch_hitter(self.situation(inning=6)))
        self.assertIsNotNone(choose_pinch_hitter(self.situation(inning=7)))

    def test_only_when_behind(self):
        self.assertIsNone(choose_pinch_hitter(self.situation(lead=0)))
        self.assertIsNone(choose_pinch_hitter(self.situation(lead=3)))

    def test_only_when_the_bench_is_clearly_better(self):
        close = batter(2, Position.OUTFIELDER, contact=42, power=42, eye=42, speed=42)
        self.assertIsNone(choose_pinch_hitter(self.situation(bench=(close,))))
        self.assertIsNone(choose_pinch_hitter(self.situation(bench=())))

    def test_at_most_two_pinch_hitters_per_game(self):
        self.assertIsNotNone(choose_pinch_hitter(self.situation(pinch_hitters_used=MAX_PINCH_HITTERS - 1)))
        self.assertIsNone(choose_pinch_hitter(self.situation(pinch_hitters_used=MAX_PINCH_HITTERS)))

    def test_the_best_hitter_on_the_bench_is_chosen(self):
        good = batter(3, Position.OUTFIELDER, contact=80, power=80, eye=80, speed=50)
        decision = choose_pinch_hitter(self.situation(bench=(self.situation().bench[0], good)))
        self.assertEqual(decision.batter.player_id, 3)

    def test_a_pinch_hitter_who_can_play_the_position_takes_it_over(self):
        decision = choose_pinch_hitter(self.situation())
        self.assertEqual(decision.fielding_position, FP.LEFT_FIELD)
        self.assertIsNone(decision.replacement)

    def test_a_pinch_hitter_who_cannot_play_the_position_is_followed_by_a_defensive_replacement(self):
        catcher_current = batter(1, Position.CATCHER, contact=40, power=40, eye=40, speed=40)
        slugger = batter(2, Position.INFIELDER, contact=80, power=80, eye=80, speed=50)
        backup_catcher = batter(3, Position.CATCHER, contact=35, power=35, eye=35, speed=35)
        decision = choose_pinch_hitter(
            self.situation(current=catcher_current, position=FP.CATCHER, bench=(slugger, backup_catcher))
        )
        self.assertEqual(decision.batter, slugger)
        self.assertEqual(decision.fielding_position, FP.PINCH_HITTER)
        self.assertEqual(decision.replacement, backup_catcher)

    def test_nobody_pinch_hits_for_the_catcher_without_a_backup_catcher(self):
        catcher_current = batter(1, Position.CATCHER, contact=40, power=40, eye=40, speed=40)
        slugger = batter(2, Position.INFIELDER, contact=80, power=80, eye=80, speed=50)
        self.assertIsNone(
            choose_pinch_hitter(self.situation(current=catcher_current, position=FP.CATCHER, bench=(slugger,)))
        )

    def test_anyone_can_pinch_hit_for_the_designated_hitter(self):
        slugger = batter(2, Position.CATCHER, contact=80, power=80, eye=80, speed=50)
        decision = choose_pinch_hitter(self.situation(position=FP.DESIGNATED_HITTER, bench=(slugger,)))
        self.assertEqual(decision.fielding_position, FP.DESIGNATED_HITTER)

    def test_a_foreign_pinch_hitter_is_skipped_when_the_quota_is_full(self):
        foreign = batter(2, Position.OUTFIELDER, foreign=True, contact=90, power=90, eye=90, speed=50)
        domestic = batter(3, Position.OUTFIELDER, contact=70, power=70, eye=70, speed=50)
        full = ForeignQuota(limit=1, used=1)
        decision = choose_pinch_hitter(self.situation(bench=(foreign, domestic), quota=full))
        self.assertEqual(decision.batter, domestic)


class ForeignQuotaTest(TestCase):
    def test_no_limit_allows_everyone(self):
        quota = ForeignQuota()
        for _ in range(10):
            self.assertTrue(quota.allows(True))
            quota.register(True)

    def test_the_limit_counts_only_foreign_players(self):
        quota = ForeignQuota(limit=2)
        quota.register(False)
        self.assertEqual(quota.used, 0)
        quota.register(True)
        quota.register(True)
        self.assertFalse(quota.allows(True))
        self.assertTrue(quota.allows(False))
        self.assertEqual(quota.remaining, 0)

    def test_extra_reserves_room_for_a_second_player(self):
        quota = ForeignQuota(limit=2, used=1)
        self.assertTrue(quota.allows(True))
        self.assertFalse(quota.allows(True, extra=1))
