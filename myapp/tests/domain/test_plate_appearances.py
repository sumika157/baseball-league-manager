"""打席の記録と、そこからの導出の単体テスト。Django も DB も使わない。"""

from datetime import date
from unittest import TestCase

from myapp.domain import services
from myapp.domain.entities import (
    FieldingError,
    Game,
    PlateAppearance,
    RunnerAdvance,
    RunnerSubstitution,
)
from myapp.domain.exceptions import InvalidPlateAppearance
from myapp.domain.value_objects import (
    AdvanceReason,
    Base,
    ErrorKind,
    FieldingPosition,
    PlateAppearanceResult,
    Season,
)

P = PlateAppearanceResult
R = AdvanceReason

HOME, AWAY = 1, 2
# ホームの投手（ビジターの攻撃で投げる）と、交代で出てくる2人目
STARTER, RELIEVER = 100, 101


def _to(runner_id, frm, to, reason=R.BATTED_BALL, error_index=None) -> RunnerAdvance:
    return RunnerAdvance(runner_id=runner_id, from_base=frm, to_base=to, reason=reason, error_index=error_index)


def _pa(
    sequence,
    order,
    result,
    *,
    inning=1,
    bottom=False,
    batter=None,
    pitcher=STARTER,
    advances=None,
    errors=(),
    substitutions=(),
) -> PlateAppearance:
    """打席を1つ作る。進塁を省いたら打者の既定の到達塁だけを埋める。

    打者の id は既定で打順と同じにし、走者を追う筋が読めるようにしている。
    """
    batter_id = order if batter is None else batter
    if advances is None:
        advances = [_to(batter_id, Base.BATTER, result.default_batter_base, result.default_batter_reason)]
    return PlateAppearance(
        sequence=sequence,
        inning=inning,
        is_bottom=bottom,
        batter_id=batter_id,
        pitcher_id=pitcher,
        batting_order=order,
        result=result,
        advances=list(advances),
        errors=list(errors),
        substitutions=list(substitutions),
    )


def _game(plate_appearances, *, home_score=0, away_score=0) -> Game:
    return Game(
        season=Season(2026),
        played_on=date(2026, 4, 1),
        home_team_id=HOME,
        away_team_id=AWAY,
        home_score=home_score,
        away_score=away_score,
        plate_appearances=list(plate_appearances),
    )


def _half_inning():
    """1回表ぶんの、成立する記録。

    単打 → 二塁打（一塁走者が三塁へ）→ 犠飛（三塁走者が還る）→ 三振 → ゴロアウト。
    1点、3アウト、二塁に残塁1。
    """
    return [
        _pa(1, 1, P.SINGLE),
        _pa(
            2,
            2,
            P.DOUBLE,
            advances=[_to(1, Base.FIRST, Base.THIRD), _to(2, Base.BATTER, Base.SECOND)],
        ),
        _pa(
            3,
            3,
            P.SACRIFICE_FLY,
            advances=[_to(3, Base.BATTER, Base.OUT, R.PUT_OUT), _to(1, Base.THIRD, Base.HOME, R.TAG_UP)],
        ),
        _pa(4, 4, P.STRIKEOUT_SWINGING),
        _pa(5, 5, P.GROUND_OUT),
    ]


class PlateAppearanceResultTest(TestCase):
    def test_walks_and_sacrifices_are_not_at_bats(self):
        for result in (P.WALK, P.INTENTIONAL_WALK, P.HIT_BY_PITCH, P.SACRIFICE_BUNT, P.SACRIFICE_FLY):
            self.assertFalse(result.counts_as_at_bat, result.label)

    def test_strikeouts_and_reaching_on_error_are_at_bats(self):
        for result in (P.STRIKEOUT_LOOKING, P.GROUND_OUT, P.REACHED_ON_ERROR, P.FIELDERS_CHOICE, P.SINGLE):
            self.assertTrue(result.counts_as_at_bat, result.label)

    def test_interference_is_not_an_at_bat(self):
        self.assertFalse(P.CATCHER_INTERFERENCE.counts_as_at_bat)
        self.assertFalse(P.OBSTRUCTION.counts_as_at_bat)

    def test_hits_and_total_bases(self):
        self.assertEqual([result.bases for result in (P.SINGLE, P.DOUBLE, P.TRIPLE, P.HOME_RUN)], [1, 2, 3, 4])
        self.assertEqual(P.WALK.bases, 0)
        self.assertFalse(P.REACHED_ON_ERROR.is_hit)

    def test_intentional_walk_counts_as_a_walk(self):
        self.assertTrue(P.INTENTIONAL_WALK.is_walk)
        self.assertTrue(P.WALK.is_walk)
        self.assertFalse(P.HIT_BY_PITCH.is_walk)

    def test_results_that_retire_the_batter(self):
        for result in (P.STRIKEOUT_SWINGING, P.GROUND_OUT, P.FOUL_FLY_OUT, P.SACRIFICE_BUNT, P.SACRIFICE_FLY):
            self.assertTrue(result.retires_batter, result.label)
        for result in (P.WALK, P.REACHED_ON_ERROR, P.FIELDERS_CHOICE, P.SINGLE):
            self.assertFalse(result.retires_batter, result.label)

    def test_default_batter_destination_and_reason(self):
        self.assertEqual(P.DOUBLE.default_batter_base, Base.SECOND)
        self.assertEqual(P.DOUBLE.default_batter_reason, R.BATTED_BALL)
        self.assertEqual(P.HOME_RUN.default_batter_base, Base.HOME)
        self.assertEqual(P.WALK.default_batter_base, Base.FIRST)
        self.assertEqual(P.WALK.default_batter_reason, R.AWARDED_BASE)
        self.assertEqual(P.REACHED_ON_ERROR.default_batter_reason, R.ERROR)
        self.assertEqual(P.STRIKEOUT_LOOKING.default_batter_base, Base.OUT)
        self.assertEqual(P.STRIKEOUT_LOOKING.default_batter_reason, R.PUT_OUT)

    def test_unknown_label_is_rejected(self):
        with self.assertRaises(InvalidPlateAppearance):
            P.from_label("サイクルヒット")


class AdvanceReasonTest(TestCase):
    def test_reasons_that_record_an_out(self):
        for reason in (R.PUT_OUT, R.CAUGHT_STEALING, R.PICKED_OFF, R.FORCE_OUT, R.THROWN_OUT):
            self.assertTrue(reason.is_out, reason.label)
        for reason in (R.BATTED_BALL, R.STOLEN_BASE, R.ERROR, R.WILD_PITCH):
            self.assertFalse(reason.is_out, reason.label)

    def test_run_batted_in_only_for_the_batters_own_doing(self):
        for reason in (R.BATTED_BALL, R.FORCED, R.TAG_UP):
            self.assertTrue(reason.earns_run_batted_in, reason.label)
        for reason in (R.ERROR, R.WILD_PITCH, R.PASSED_BALL, R.STOLEN_BASE, R.FIELDERS_CHOICE, R.BALK):
            self.assertFalse(reason.earns_run_batted_in, reason.label)

    def test_only_errors_and_passed_balls_make_runs_unearned(self):
        self.assertTrue(R.ERROR.is_unearned_cause)
        self.assertTrue(R.PASSED_BALL.is_unearned_cause)
        # 暴投とボークは投手自身の責任なので自責点に含める
        self.assertFalse(R.WILD_PITCH.is_unearned_cause)
        self.assertFalse(R.BALK.is_unearned_cause)


class RunnerAdvanceTest(TestCase):
    def test_runner_cannot_go_backwards_or_stay(self):
        with self.assertRaises(InvalidPlateAppearance):
            _to(1, Base.SECOND, Base.FIRST)
        with self.assertRaises(InvalidPlateAppearance):
            _to(1, Base.SECOND, Base.SECOND)

    def test_reason_and_destination_must_agree(self):
        with self.assertRaises(InvalidPlateAppearance):
            _to(1, Base.FIRST, Base.SECOND, R.CAUGHT_STEALING)
        with self.assertRaises(InvalidPlateAppearance):
            _to(1, Base.FIRST, Base.OUT, R.STOLEN_BASE)

    def test_runner_already_out_or_home_cannot_advance(self):
        with self.assertRaises(InvalidPlateAppearance):
            _to(1, Base.OUT, Base.FIRST)
        with self.assertRaises(InvalidPlateAppearance):
            _to(1, Base.HOME, Base.FIRST)


class PlateAppearanceTest(TestCase):
    def test_batting_order_must_be_within_the_lineup(self):
        with self.assertRaises(InvalidPlateAppearance):
            _pa(1, 10, P.SINGLE)
        with self.assertRaises(InvalidPlateAppearance):
            _pa(1, 0, P.SINGLE)

    def test_batter_advance_is_required(self):
        with self.assertRaises(InvalidPlateAppearance):
            _pa(1, 1, P.SINGLE, advances=[])

    def test_batter_cannot_have_two_advances(self):
        with self.assertRaises(InvalidPlateAppearance):
            _pa(
                1,
                1,
                P.SINGLE,
                advances=[_to(1, Base.BATTER, Base.FIRST), _to(1, Base.BATTER, Base.SECOND)],
            )

    def test_strikeout_cannot_reach_base(self):
        with self.assertRaises(InvalidPlateAppearance):
            _pa(1, 1, P.STRIKEOUT_LOOKING, advances=[_to(1, Base.BATTER, Base.FIRST)])

    def test_home_run_must_reach_home(self):
        with self.assertRaises(InvalidPlateAppearance):
            _pa(1, 1, P.HOME_RUN, advances=[_to(1, Base.BATTER, Base.THIRD)])

    def test_double_must_reach_second(self):
        with self.assertRaises(InvalidPlateAppearance):
            _pa(1, 1, P.DOUBLE, advances=[_to(1, Base.BATTER, Base.FIRST)])

    def test_hitter_thrown_out_stretching_is_allowed(self):
        entry = _pa(1, 1, P.SINGLE, advances=[_to(1, Base.BATTER, Base.OUT, R.THROWN_OUT)])

        self.assertEqual(entry.outs_recorded, 1)

    def test_reaching_on_error_needs_an_error(self):
        with self.assertRaises(InvalidPlateAppearance):
            _pa(1, 1, P.REACHED_ON_ERROR, advances=[_to(1, Base.BATTER, Base.FIRST, R.ERROR)])

    def test_sacrifice_fly_needs_a_run(self):
        with self.assertRaises(InvalidPlateAppearance):
            _pa(1, 1, P.SACRIFICE_FLY)

    def test_sacrifice_bunt_needs_a_runner_to_advance(self):
        with self.assertRaises(InvalidPlateAppearance):
            _pa(1, 1, P.SACRIFICE_BUNT)

    def test_advance_cannot_point_at_a_missing_error(self):
        with self.assertRaises(InvalidPlateAppearance):
            _pa(1, 1, P.SINGLE, advances=[_to(1, Base.BATTER, Base.FIRST, R.BATTED_BALL, error_index=0)])

    def test_counts_outs_runs_and_runs_batted_in(self):
        entry = _pa(
            1,
            4,
            P.SINGLE,
            advances=[
                _to(4, Base.BATTER, Base.FIRST),
                _to(3, Base.THIRD, Base.HOME),
                _to(2, Base.SECOND, Base.HOME),
            ],
        )

        self.assertEqual(entry.outs_recorded, 0)
        self.assertEqual(entry.runs_scored, 2)
        self.assertEqual(entry.runs_batted_in, 2)
        self.assertFalse(entry.is_double_play)

    def test_run_scored_on_an_error_earns_no_run_batted_in(self):
        entry = _pa(
            1,
            4,
            P.GROUND_OUT,
            advances=[
                _to(4, Base.BATTER, Base.OUT, R.PUT_OUT),
                _to(3, Base.THIRD, Base.HOME, R.ERROR, error_index=0),
            ],
            errors=[FieldingError(player_id=6, position=FieldingPosition.SHORTSTOP, kind=ErrorKind.THROWING)],
        )

        self.assertEqual(entry.runs_scored, 1)
        self.assertEqual(entry.runs_batted_in, 0)

    def test_double_play_earns_no_run_batted_in(self):
        entry = _pa(
            1,
            4,
            P.GROUND_OUT,
            advances=[
                _to(4, Base.BATTER, Base.OUT, R.PUT_OUT),
                _to(1, Base.FIRST, Base.OUT, R.FORCE_OUT),
                _to(3, Base.THIRD, Base.HOME),
            ],
        )

        self.assertTrue(entry.is_double_play)
        self.assertEqual(entry.runs_scored, 1)
        self.assertEqual(entry.runs_batted_in, 0)


class GameConsistencyTest(TestCase):
    def test_a_well_formed_half_inning_passes(self):
        game = _game(_half_inning(), away_score=1)

        game.ensure_plate_appearances_consistent()

    def test_no_plate_appearances_is_allowed(self):
        _game([]).ensure_plate_appearances_consistent()

    def test_sequence_must_not_skip(self):
        game = _game([_pa(1, 1, P.SINGLE), _pa(3, 2, P.STRIKEOUT_LOOKING)])

        with self.assertRaises(InvalidPlateAppearance):
            game.ensure_plate_appearances_consistent()

    def test_batting_order_must_cycle(self):
        game = _game([_pa(1, 1, P.STRIKEOUT_LOOKING), _pa(2, 3, P.STRIKEOUT_LOOKING)])

        with self.assertRaises(InvalidPlateAppearance):
            game.ensure_plate_appearances_consistent()

    def test_batting_order_wraps_from_nine_to_one(self):
        entries = [_pa(index, order, P.STRIKEOUT_LOOKING) for index, order in enumerate([8, 9, 1], start=1)]
        # 3アウトで半回が終わる並びにしてある
        _game(entries).ensure_plate_appearances_consistent()

    def test_two_runners_cannot_share_a_base(self):
        game = _game([_pa(1, 1, P.SINGLE), _pa(2, 2, P.SINGLE, batter=2)])

        with self.assertRaises(InvalidPlateAppearance):
            game.ensure_plate_appearances_consistent()

    def test_runner_must_be_on_the_base_they_leave(self):
        game = _game(
            [
                _pa(1, 1, P.SINGLE),
                _pa(
                    2,
                    2,
                    P.SINGLE,
                    advances=[
                        _to(1, Base.FIRST, Base.SECOND),
                        _to(2, Base.BATTER, Base.FIRST),
                        _to(77, Base.THIRD, Base.HOME),
                    ],
                ),
            ]
        )

        with self.assertRaises(InvalidPlateAppearance):
            game.ensure_plate_appearances_consistent()

    def test_advances_are_applied_from_the_lead_runner(self):
        """打者の進塁を先に書いても、塁の衝突と誤判定されないこと。

        一塁走者が二塁へ進み、打者が一塁に入る場面。書かれた順にそのまま適用すると
        「一塁に走者が2人」と誤って弾かれる（先の塁の走者から適用する必要がある）。
        """
        game = _game(
            [
                _pa(1, 1, P.SINGLE),
                _pa(
                    2,
                    2,
                    P.SINGLE,
                    advances=[_to(2, Base.BATTER, Base.FIRST), _to(1, Base.FIRST, Base.SECOND)],
                ),
            ]
        )

        game.ensure_plate_appearances_consistent()

    def test_half_inning_cannot_have_four_outs(self):
        entries = [_pa(index, index, P.STRIKEOUT_LOOKING) for index in range(1, 5)]
        game = _game(entries)

        with self.assertRaises(InvalidPlateAppearance):
            game.ensure_plate_appearances_consistent()

    def test_derived_runs_must_match_the_final_score(self):
        game = _game(_half_inning(), away_score=0)

        with self.assertRaises(InvalidPlateAppearance):
            game.ensure_plate_appearances_consistent()

    def test_pinch_runner_must_replace_someone_on_that_base(self):
        game = _game([_pa(1, 1, P.SINGLE, substitutions=[RunnerSubstitution(Base.SECOND, 1, 99)])])

        with self.assertRaises(InvalidPlateAppearance):
            game.ensure_plate_appearances_consistent()

    def test_pinch_runner_takes_over_the_base(self):
        game = _game(
            [
                _pa(1, 1, P.SINGLE),
                _pa(
                    2,
                    2,
                    P.HOME_RUN,
                    substitutions=[RunnerSubstitution(Base.FIRST, 1, 99)],
                    advances=[_to(2, Base.BATTER, Base.HOME), _to(99, Base.FIRST, Base.HOME)],
                ),
            ],
            away_score=2,
        )

        game.ensure_plate_appearances_consistent()


class DerivedLineScoreTest(TestCase):
    def test_scoreless_half_innings_are_kept(self):
        entries = _half_inning() + [
            _pa(6, 6, P.STRIKEOUT_LOOKING, inning=2),
            _pa(7, 7, P.STRIKEOUT_LOOKING, inning=2),
            _pa(8, 8, P.STRIKEOUT_LOOKING, inning=2),
        ]

        derived = _game(entries).derived_line_score()

        self.assertEqual(derived.away, (1, 0))
        self.assertEqual(derived.away_total, 1)

    def test_bottom_half_is_shorter_when_the_home_team_does_not_bat(self):
        """ビジターが2回まで攻め、ホームは1回しか攻げていない記録。

        リードしているホームが最終回を攻めずに終わる形。裏は表より短くなる。
        """
        entries = [
            *_half_inning(),
            _pa(6, 1, P.STRIKEOUT_LOOKING, inning=1, bottom=True, pitcher=999),
            _pa(7, 6, P.STRIKEOUT_LOOKING, inning=2),
        ]

        derived = _game(entries).derived_line_score()

        self.assertEqual(derived.away, (1, 0))
        self.assertEqual(derived.home, (0,))

    def test_no_plate_appearances_gives_an_empty_line_score(self):
        self.assertTrue(_game([]).derived_line_score().is_empty)


class BattingLineDerivationTest(TestCase):
    def test_hits_are_split_by_kind(self):
        entries = _half_inning()

        line = services.batting_line_for(entries, batter_id=1)

        self.assertEqual(line.at_bats, 1)
        self.assertEqual(line.singles, 1)
        self.assertEqual(line.hits, 1)

    def test_sacrifice_fly_is_not_an_at_bat_but_earns_a_run_batted_in(self):
        line = services.batting_line_for(_half_inning(), batter_id=3)

        self.assertEqual(line.at_bats, 0)
        self.assertEqual(line.sacrifice_flies, 1)
        self.assertEqual(line.runs_batted_in, 1)

    def test_walks_include_intentional_walks(self):
        entries = [
            _pa(1, 1, P.WALK),
            _pa(2, 2, P.INTENTIONAL_WALK, batter=1, advances=[_to(1, Base.BATTER, Base.FIRST, R.AWARDED_BASE)]),
        ]

        line = services.batting_line_for(entries, batter_id=1)

        self.assertEqual(line.walks, 2)
        self.assertEqual(line.at_bats, 0)

    def test_player_without_plate_appearances_has_an_empty_line(self):
        line = services.batting_line_for(_half_inning(), batter_id=999)

        self.assertEqual(line.at_bats, 0)
        self.assertEqual(line.hits, 0)


class PitchingLineDerivationTest(TestCase):
    def test_innings_come_from_the_outs_recorded(self):
        line = services.pitching_line_for(_half_inning(), pitcher_id=STARTER)

        self.assertEqual(line.innings.outs, 3)

    def test_counts_come_from_the_results_faced(self):
        line = services.pitching_line_for(_half_inning(), pitcher_id=STARTER)

        self.assertEqual(line.hits_allowed, 2)
        self.assertEqual(line.strikeouts, 1)
        self.assertEqual(line.walks_allowed, 0)
        self.assertEqual(line.home_runs_allowed, 0)
        self.assertEqual(line.earned_runs, 1)

    def test_decisions_are_left_to_the_other_service(self):
        line = services.pitching_line_for(_half_inning(), pitcher_id=STARTER)

        self.assertEqual((line.wins, line.losses, line.saves, line.holds), (0, 0, 0, 0))


class RunResponsibilityTest(TestCase):
    def test_run_is_charged_to_the_pitcher_who_allowed_the_runner(self):
        entries = [
            _pa(1, 1, P.SINGLE, pitcher=STARTER),
            _pa(
                2,
                2,
                P.HOME_RUN,
                pitcher=RELIEVER,
                advances=[_to(2, Base.BATTER, Base.HOME), _to(1, Base.FIRST, Base.HOME)],
            ),
        ]

        self.assertEqual(services.runs_allowed_for(entries, STARTER), 1)
        self.assertEqual(services.runs_allowed_for(entries, RELIEVER), 1)

    def test_pinch_runner_inherits_the_responsible_pitcher(self):
        entries = [
            _pa(1, 1, P.SINGLE, pitcher=STARTER),
            _pa(
                2,
                2,
                P.HOME_RUN,
                pitcher=RELIEVER,
                substitutions=[RunnerSubstitution(Base.FIRST, 1, 99)],
                advances=[_to(2, Base.BATTER, Base.HOME), _to(99, Base.FIRST, Base.HOME)],
            ),
        ]

        self.assertEqual(services.runs_allowed_for(entries, STARTER), 1)
        self.assertEqual(services.runs_allowed_for(entries, RELIEVER), 1)

    def test_runner_who_reached_on_an_error_is_unearned(self):
        entries = [
            _pa(
                1,
                1,
                P.REACHED_ON_ERROR,
                advances=[_to(1, Base.BATTER, Base.FIRST, R.ERROR, error_index=0)],
                errors=[FieldingError(player_id=6, position=FieldingPosition.SHORTSTOP, kind=ErrorKind.FIELDING)],
            ),
            _pa(
                2,
                2,
                P.HOME_RUN,
                advances=[_to(2, Base.BATTER, Base.HOME), _to(1, Base.FIRST, Base.HOME)],
            ),
        ]

        self.assertEqual(services.runs_allowed_for(entries, STARTER), 2)
        self.assertEqual(services.earned_runs_for(entries, STARTER), 1)

    def test_run_scored_on_a_passed_ball_is_unearned(self):
        entries = [
            _pa(1, 1, P.SINGLE),
            _pa(
                2,
                2,
                P.STRIKEOUT_SWINGING,
                advances=[
                    _to(2, Base.BATTER, Base.OUT, R.PUT_OUT),
                    _to(1, Base.FIRST, Base.THIRD, R.PASSED_BALL),
                ],
            ),
            _pa(
                3,
                3,
                P.SACRIFICE_FLY,
                advances=[_to(3, Base.BATTER, Base.OUT, R.PUT_OUT), _to(1, Base.THIRD, Base.HOME, R.TAG_UP)],
            ),
        ]

        self.assertEqual(services.runs_allowed_for(entries, STARTER), 1)
        self.assertEqual(services.earned_runs_for(entries, STARTER), 0)

    def test_run_scored_on_a_wild_pitch_is_earned(self):
        entries = [
            _pa(1, 1, P.TRIPLE),
            _pa(
                2,
                2,
                P.STRIKEOUT_SWINGING,
                advances=[
                    _to(2, Base.BATTER, Base.OUT, R.PUT_OUT),
                    _to(1, Base.THIRD, Base.HOME, R.WILD_PITCH),
                ],
            ),
        ]

        self.assertEqual(services.earned_runs_for(entries, STARTER), 1)


class LeftOnBaseTest(TestCase):
    def test_runners_left_at_the_end_of_a_half_inning_are_counted(self):
        self.assertEqual(services.left_on_base(_half_inning(), is_bottom=False), 1)

    def test_the_other_side_has_none(self):
        self.assertEqual(services.left_on_base(_half_inning(), is_bottom=True), 0)


class FieldingErrorTest(TestCase):
    def test_errors_are_counted_per_player(self):
        entries = [
            _pa(
                1,
                1,
                P.REACHED_ON_ERROR,
                advances=[_to(1, Base.BATTER, Base.FIRST, R.ERROR, error_index=0)],
                errors=[FieldingError(player_id=6, position=FieldingPosition.SHORTSTOP, kind=ErrorKind.THROWING)],
            ),
        ]

        self.assertEqual(services.errors_for(entries, player_id=6), 1)
        self.assertEqual(services.errors_for(entries, player_id=4), 0)
