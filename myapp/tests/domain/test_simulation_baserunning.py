"""進塁の組み立て（`baserunning`）と、打球の処理経路（`fielding`）の単体テスト。

乱数は決めた列を差し込む。`random()` が返す値で、確率の判定がどちらに倒れるかを決める
（0.0 なら「起きる」側、0.99 なら「起きない」側）。Django も DB も使わない。
"""

from collections import Counter
from unittest import TestCase

from myapp.domain.simulation.baserunning import DEFAULT_RULES, adjusted, build_advances
from myapp.domain.simulation.fielding import draw_error, fielded_path, team_defense
from myapp.domain.simulation.randomness import make_random
from myapp.domain.value_objects import AdvanceReason, Base, ErrorKind, FieldingPosition, PlateAppearanceResult

P = PlateAppearanceResult
R = AdvanceReason
FP = FieldingPosition


class ScriptedRandom:
    def __init__(self, *values: float) -> None:
        self.values = values
        self.index = 0

    def random(self) -> float:
        value = self.values[self.index % len(self.values)]
        self.index += 1
        return value


def advances(result, occupied, *, outs=0, rng=None, speeds=None, batter=99):
    occupied = dict(occupied)
    made = build_advances(rng or ScriptedRandom(0.99), DEFAULT_RULES, result, batter, occupied, outs, speeds or {})
    return made, occupied


def moves(made):
    return sorted((a.runner_id, a.from_base.name, a.to_base.name, a.reason.name) for a in made)


class AdvanceTest(TestCase):
    def test_a_home_run_clears_the_bases(self):
        made, bases = advances(P.HOME_RUN, {Base.FIRST: 1, Base.THIRD: 3})
        self.assertEqual(sum(a.has_scored for a in made), 3)
        self.assertEqual(bases, {})

    def test_a_walk_forces_only_the_runners_in_a_chain(self):
        made, bases = advances(P.WALK, {Base.FIRST: 1, Base.THIRD: 3})
        self.assertEqual(moves(made), [(1, "FIRST", "SECOND", "FORCED"), (99, "BATTER", "FIRST", "AWARDED_BASE")])
        self.assertEqual(bases, {Base.FIRST: 99, Base.SECOND: 1, Base.THIRD: 3})

    def test_a_bases_loaded_walk_forces_in_a_run(self):
        made, _ = advances(P.WALK, {Base.FIRST: 1, Base.SECOND: 2, Base.THIRD: 3})
        self.assertEqual(sum(a.has_scored for a in made), 1)

    def test_a_double_play_removes_the_runner_on_first_and_the_batter(self):
        made, bases = advances(P.GROUND_OUT, {Base.FIRST: 1}, outs=0, rng=ScriptedRandom(0.0))
        self.assertEqual(sum(a.is_out for a in made), 2)
        self.assertEqual(bases, {})

    def test_no_double_play_with_two_outs(self):
        made, _ = advances(P.GROUND_OUT, {Base.FIRST: 1}, outs=2, rng=ScriptedRandom(0.0))
        self.assertEqual(sum(a.is_out for a in made), 1)

    def test_with_two_outs_a_ground_out_never_moves_the_runners(self):
        """2アウトのゴロでは、打者がアウトになって回が終わるので、走者は進まず得点も入らない。"""
        made, bases = advances(P.GROUND_OUT, {Base.THIRD: 3}, outs=2, rng=ScriptedRandom(0.0))
        self.assertEqual(moves(made), [(99, "BATTER", "OUT", "PUT_OUT")])
        self.assertEqual(bases, {Base.THIRD: 3})

    def test_a_ground_out_can_move_the_runners_up_with_fewer_than_two_outs(self):
        # 併殺にならず（0.99）、進塁する（0.0 < 0.2）ように、列を並べる
        made, bases = advances(P.GROUND_OUT, {Base.SECOND: 2}, outs=0, rng=ScriptedRandom(0.0))
        self.assertIn((2, "SECOND", "THIRD", "BATTED_BALL"), moves(made))
        self.assertEqual(bases, {Base.THIRD: 2})

    def test_a_sacrifice_fly_scores_the_runner_on_third(self):
        made, bases = advances(P.SACRIFICE_FLY, {Base.THIRD: 3})
        self.assertEqual(moves(made), [(3, "THIRD", "HOME", "TAG_UP"), (99, "BATTER", "OUT", "PUT_OUT")])
        self.assertEqual(bases, {})

    def test_a_sacrifice_bunt_moves_the_runners_up(self):
        made, bases = advances(P.SACRIFICE_BUNT, {Base.FIRST: 1, Base.SECOND: 2})
        self.assertEqual(bases, {Base.SECOND: 1, Base.THIRD: 2})
        self.assertEqual(sum(a.is_out for a in made), 1)

    def test_a_fielders_choice_retires_the_runner_on_first_and_puts_the_batter_there(self):
        made, bases = advances(P.FIELDERS_CHOICE, {Base.FIRST: 1})
        self.assertEqual(moves(made), [(1, "FIRST", "OUT", "FORCE_OUT"), (99, "BATTER", "FIRST", "FIELDERS_CHOICE")])
        self.assertEqual(bases, {Base.FIRST: 99})

    def test_reaching_on_an_error_ties_every_advance_to_the_error(self):
        made, _ = advances(P.REACHED_ON_ERROR, {Base.FIRST: 1})
        self.assertTrue(all(a.error_index == 0 and a.reason is R.ERROR for a in made))

    def test_a_single_scores_the_runner_from_third(self):
        made, _ = advances(P.SINGLE, {Base.THIRD: 3})
        self.assertIn((3, "THIRD", "HOME", "BATTED_BALL"), moves(made))

    def test_a_triple_scores_everyone(self):
        made, bases = advances(P.TRIPLE, {Base.FIRST: 1, Base.SECOND: 2})
        self.assertEqual(sum(a.has_scored for a in made), 2)
        self.assertEqual(bases, {Base.THIRD: 99})

    def test_a_runner_never_jumps_a_blocked_base(self):
        """前の走者が止まっていれば、後ろの走者も手前で止まる。"""
        for seed in range(200):
            _, bases = advances(P.SINGLE, {Base.FIRST: 1, Base.SECOND: 2, Base.THIRD: 3}, rng=make_random(seed))
            self.assertEqual(len(bases), len(set(bases.values())))

    def test_a_steal_is_tried_only_on_a_plate_appearance_without_contact(self):
        made, _ = advances(P.STRIKEOUT_SWINGING, {Base.FIRST: 1}, rng=ScriptedRandom(0.0))
        self.assertIn((1, "FIRST", "SECOND", "STOLEN_BASE"), moves(made))
        made, _ = advances(P.SINGLE, {Base.FIRST: 1}, rng=ScriptedRandom(0.0))
        self.assertNotIn(R.STOLEN_BASE, {a.reason for a in made})

    def test_no_steal_with_two_outs_or_a_runner_on_second(self):
        for occupied, outs in (({Base.FIRST: 1}, 2), ({Base.FIRST: 1, Base.SECOND: 2}, 0)):
            made, _ = advances(P.STRIKEOUT_SWINGING, occupied, outs=outs, rng=ScriptedRandom(0.0))
            self.assertNotIn(R.STOLEN_BASE, {a.reason for a in made})
            self.assertNotIn(R.CAUGHT_STEALING, {a.reason for a in made})

    def test_a_failed_steal_retires_the_runner(self):
        # 試みる（0.0）が、成功しない（0.99）
        made, bases = advances(P.STRIKEOUT_SWINGING, {Base.FIRST: 1}, rng=ScriptedRandom(0.0, 0.99))
        self.assertIn((1, "FIRST", "OUT", "CAUGHT_STEALING"), moves(made))
        self.assertEqual(bases, {})

    def test_faster_runners_steal_more_often(self):
        def attempts(speed):
            count = 0
            rng = make_random(5)
            for _ in range(3000):
                made, _ = advances(P.WALK, {Base.FIRST: 1}, rng=rng, speeds={1: speed})
                count += any(a.reason in (R.STOLEN_BASE, R.CAUGHT_STEALING) for a in made)
            return count

        self.assertGreater(attempts(80), attempts(50))
        self.assertGreater(attempts(50), attempts(20))

    def test_slower_batters_hit_into_more_double_plays(self):
        def double_plays(speed):
            count = 0
            rng = make_random(9)
            for _ in range(3000):
                made, _ = advances(P.GROUND_OUT, {Base.FIRST: 1}, rng=rng, speeds={99: speed})
                count += sum(a.is_out for a in made) == 2
            return count

        self.assertGreater(double_plays(20), double_plays(80))

    def test_adjusted_is_neutral_at_average_speed_and_monotonic(self):
        self.assertAlmostEqual(adjusted(0.3, 0.5, 50), 0.3)
        self.assertGreater(adjusted(0.3, 0.5, 80), 0.3)
        self.assertLess(adjusted(0.3, 0.5, 20), 0.3)
        self.assertEqual(adjusted(0.0, 0.5, 80), 0.0)
        self.assertEqual(adjusted(1.0, 0.5, 80), 1.0)


class FieldedPathTest(TestCase):
    def sample(self, result, *, double_play=False, n=2000):
        rng = make_random(13)
        return Counter(fielded_path(rng, result, double_play=double_play) for _ in range(n))

    def test_results_without_a_fielding_play_have_no_path(self):
        rng = make_random(1)
        for result in (
            P.SINGLE,
            P.DOUBLE,
            P.TRIPLE,
            P.HOME_RUN,
            P.WALK,
            P.INTENTIONAL_WALK,
            P.HIT_BY_PITCH,
            P.STRIKEOUT_LOOKING,
            P.STRIKEOUT_SWINGING,
            P.REACHED_ON_ERROR,
        ):
            self.assertEqual(fielded_path(rng, result), ())

    def test_ground_outs_are_spread_over_several_paths(self):
        paths = self.sample(P.GROUND_OUT)
        self.assertGreaterEqual(len(paths), 5)
        self.assertEqual(paths.most_common(1)[0][0], (FP.SHORTSTOP, FP.FIRST_BASE))

    def test_double_play_paths_have_three_players_and_end_at_first(self):
        paths = self.sample(P.GROUND_OUT, double_play=True)
        self.assertGreaterEqual(len(paths), 4)
        self.assertTrue(all(len(path) == 3 and path[-1] is FP.FIRST_BASE for path in paths))

    def test_sacrifice_flies_are_caught_by_outfielders(self):
        outfield = {FP.LEFT_FIELD, FP.CENTER_FIELD, FP.RIGHT_FIELD}
        self.assertTrue(all(path[0] in outfield for path in self.sample(P.SACRIFICE_FLY)))

    def test_every_position_on_a_path_takes_the_field(self):
        for result in (P.GROUND_OUT, P.FLY_OUT, P.LINE_OUT, P.FOUL_FLY_OUT, P.SACRIFICE_BUNT, P.FIELDERS_CHOICE):
            for path in self.sample(result, n=300):
                self.assertTrue(all(position.takes_the_field for position in path), path)

    def test_the_same_random_source_gives_the_same_path(self):
        first = [fielded_path(make_random(2), P.FLY_OUT) for _ in range(3)]
        self.assertEqual(first[0], first[1])


class DefenseAndErrorTest(TestCase):
    ALL = [
        (FP.CATCHER, 50),
        (FP.FIRST_BASE, 50),
        (FP.SECOND_BASE, 50),
        (FP.THIRD_BASE, 50),
        (FP.SHORTSTOP, 50),
        (FP.LEFT_FIELD, 50),
        (FP.CENTER_FIELD, 50),
        (FP.RIGHT_FIELD, 50),
        (FP.DESIGNATED_HITTER, 99),
    ]

    def test_team_defense_ignores_the_designated_hitter(self):
        self.assertAlmostEqual(team_defense(self.ALL), 50.0)

    def test_important_positions_weigh_more(self):
        better_short = [(p, 80 if p is FP.SHORTSTOP else r) for p, r in self.ALL]
        better_first = [(p, 80 if p is FP.FIRST_BASE else r) for p, r in self.ALL]
        self.assertGreater(team_defense(better_short), team_defense(better_first))

    def test_team_defense_stays_inside_the_rating_range_when_positions_are_missing(self):
        self.assertAlmostEqual(team_defense([(FP.SHORTSTOP, 70)]), 70.0)
        self.assertAlmostEqual(team_defense([]), 50.0)

    def test_poor_fielders_commit_more_errors(self):
        fielders = [(FP.SHORTSTOP, 1, 20), (FP.SECOND_BASE, 2, 80), (FP.LEFT_FIELD, 3, 50), (FP.PITCHER, 4, 50)]
        rng = make_random(21)
        committed = Counter(draw_error(rng, fielders).player_id for _ in range(4000))
        self.assertGreater(committed[1], committed[2] * 3)

    def test_the_error_names_the_position_the_fielder_holds(self):
        fielders = [(FP.SHORTSTOP, 1, 50), (FP.LEFT_FIELD, 3, 50)]
        rng = make_random(4)
        for _ in range(200):
            error = draw_error(rng, fielders)
            self.assertEqual({1: FP.SHORTSTOP, 3: FP.LEFT_FIELD}[error.player_id], error.position)

    def test_outfielders_drop_fly_balls_and_infielders_do_not(self):
        rng = make_random(8)
        outfield_kinds = {draw_error(rng, [(FP.CENTER_FIELD, 1, 50)]).kind for _ in range(300)}
        infield_kinds = {draw_error(rng, [(FP.SHORTSTOP, 1, 50)]).kind for _ in range(300)}
        self.assertIn(ErrorKind.DROPPED_FLY, outfield_kinds)
        self.assertNotIn(ErrorKind.DROPPED_FLY, infield_kinds)
