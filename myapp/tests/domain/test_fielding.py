"""守備成績（FieldingLine）と、打席ごとの守備の記録（刺殺・補殺・処理者）の単体テスト。Django も DB も使わない。

ビジターが表、ホームが裏に攻める。守備を見るのは主にホーム（表の間に守る）で、
ホームの打者は id 21〜29、投手は 100・101。ビジターの打者は 1〜9、投手は 200。

**ラインアップの行にはチームが無い**ので、選手が打席に現れて初めてどちらの側か分かる。
そのためホームの打者を一巡させる（`_Book.home_bats_around`）ところから始める。
"""

from datetime import date
from unittest import TestCase

from myapp.domain import services
from myapp.domain.entities import FieldingError, Game
from myapp.domain.exceptions import InvalidStatValue
from myapp.domain.value_objects import Base, BattingLine, ErrorKind, FieldingLine, GameStage, Season

from .fielding_builders import (
    AWAY,
    CATCHER,
    DH,
    FIRST,
    FP,
    HOME,
    HOME_LINEUP,
    LEFT,
    RELIEVER,
    SHORT,
    STARTER,
    P,
    R,
    _Book,
    _game,
    _move,
)


class FieldingLineTest(TestCase):
    def test_chances_and_percentage(self):
        line = FieldingLine(putouts=30, assists=10, errors=2)
        self.assertEqual(line.total_chances, 42)
        self.assertAlmostEqual(line.fielding_percentage, 40 / 42)

    def test_no_chances_means_zero_percentage(self):
        self.assertEqual(FieldingLine().fielding_percentage, 0.0)

    def test_negative_counts_are_rejected(self):
        for name in ("putouts", "assists", "errors", "double_plays_turned"):
            with self.subTest(name=name), self.assertRaises(InvalidStatValue):
                FieldingLine(**{name: -1})

    def test_addition_sums_every_field(self):
        total = FieldingLine(1, 2, 3, 4) + FieldingLine(10, 20, 30, 40)
        self.assertEqual(total, FieldingLine(11, 22, 33, 44))

    def test_percentage_is_recomputed_from_summed_counts(self):
        """率の平均ではなく、足した実数から求め直す。"""
        perfect = FieldingLine(putouts=1)  # 1.000
        flawed = FieldingLine(putouts=1, errors=3)  # 0.250
        self.assertAlmostEqual(FieldingLine.total([perfect, flawed]).fielding_percentage, 2 / 5)

    def test_adding_something_else_is_not_supported(self):
        with self.assertRaises(TypeError):
            FieldingLine() + BattingLine()  # type: ignore[operator]


class FieldingCreditsTest(TestCase):
    """打球の処理経路の読み方。位置のままで、選手にはまだ直さない。"""

    @staticmethod
    def _credits(result, path=(), advances=None):
        book = _Book()
        book.add(result=result, fielded_by=path, advances=advances)
        return services.fielding_credits(book.entries[0])

    def test_last_position_gets_the_putout_and_earlier_ones_the_assists(self):
        credits = self._credits(P.GROUND_OUT, (FP.SHORTSTOP, FP.FIRST_BASE))
        self.assertEqual(credits.putouts, (FP.FIRST_BASE,))
        self.assertEqual(credits.assists, (FP.SHORTSTOP,))
        self.assertEqual(credits.double_play, ())

    def test_a_single_position_is_a_putout_only(self):
        credits = self._credits(P.FLY_OUT, (FP.CENTER_FIELD,))
        self.assertEqual(credits.putouts, (FP.CENTER_FIELD,))
        self.assertEqual(credits.assists, ())

    def test_sacrifice_bunt_pitcher_to_first(self):
        credits = self._credits(P.GROUND_OUT, (FP.PITCHER, FP.FIRST_BASE))
        self.assertEqual((credits.putouts, credits.assists), ((FP.FIRST_BASE,), (FP.PITCHER,)))

    def test_double_play_gives_the_relay_a_putout_and_everyone_a_participation(self):
        """6-4-3: 遊が補殺、二が刺殺＋補殺、一が刺殺。"""
        path = (FP.SHORTSTOP, FP.SECOND_BASE, FP.FIRST_BASE)
        credits = self._credits(
            P.GROUND_OUT,
            path,
            advances=[_move(1, Base.BATTER, Base.OUT, R.PUT_OUT), _move(9, Base.FIRST, Base.OUT, R.FORCE_OUT)],
        )
        self.assertEqual(set(credits.putouts), {FP.SECOND_BASE, FP.FIRST_BASE})
        self.assertEqual(credits.assists, (FP.SHORTSTOP, FP.SECOND_BASE))
        self.assertEqual(credits.double_play, path)

    def test_a_two_position_double_play_credits_the_second_to_last_position_too(self):
        credits = self._credits(
            P.GROUND_OUT,
            (FP.SECOND_BASE, FP.FIRST_BASE),
            advances=[_move(1, Base.BATTER, Base.OUT, R.PUT_OUT), _move(9, Base.FIRST, Base.OUT, R.FORCE_OUT)],
        )
        self.assertEqual(credits.putouts, (FP.FIRST_BASE, FP.SECOND_BASE))
        self.assertEqual(credits.assists, (FP.SECOND_BASE,))

    def test_an_unassisted_double_play_gives_both_putouts_to_the_same_position(self):
        credits = self._credits(
            P.LINE_OUT,
            (FP.FIRST_BASE,),
            advances=[_move(1, Base.BATTER, Base.OUT, R.PUT_OUT), _move(9, Base.FIRST, Base.OUT, R.FORCE_OUT)],
        )
        self.assertEqual(credits.putouts, (FP.FIRST_BASE, FP.FIRST_BASE))
        self.assertEqual(credits.assists, ())

    def test_strikeout_without_a_path_is_the_catchers_putout(self):
        for result in (P.STRIKEOUT_SWINGING, P.STRIKEOUT_LOOKING):
            with self.subTest(result=result):
                credits = self._credits(result)
                self.assertEqual(credits.putouts, (FP.CATCHER,))
                self.assertEqual(credits.assists, ())

    def test_dropped_third_strike_thrown_to_first_follows_the_path(self):
        credits = self._credits(P.STRIKEOUT_SWINGING, (FP.CATCHER, FP.FIRST_BASE))
        self.assertEqual((credits.putouts, credits.assists), ((FP.FIRST_BASE,), (FP.CATCHER,)))

    def test_an_out_with_no_recorded_path_credits_nobody(self):
        self.assertEqual(self._credits(P.GROUND_OUT), services.FieldingCredits())

    def test_results_where_the_batter_is_not_out_credit_nobody_even_with_a_path(self):
        path = (FP.SHORTSTOP, FP.FIRST_BASE)
        for result in (P.SINGLE, P.WALK, P.HIT_BY_PITCH, P.CATCHER_INTERFERENCE):
            with self.subTest(result=result):
                self.assertEqual(self._credits(result, path), services.FieldingCredits())

    def test_home_run_credits_nobody(self):
        self.assertEqual(self._credits(P.HOME_RUN, (FP.CENTER_FIELD,)), services.FieldingCredits())

    def test_reaching_on_an_error_credits_nobody(self):
        book = _Book()
        book.add(
            result=P.REACHED_ON_ERROR,
            fielded_by=(FP.SHORTSTOP, FP.FIRST_BASE),
            errors=[FieldingError(SHORT, FP.SHORTSTOP, ErrorKind.THROWING)],
        )
        self.assertEqual(services.fielding_credits(book.entries[0]), services.FieldingCredits())

    def test_fielders_choice_credits_the_play_that_retired_the_runner(self):
        credits = self._credits(
            P.FIELDERS_CHOICE,
            (FP.SHORTSTOP, FP.SECOND_BASE),
            advances=[
                _move(9, Base.FIRST, Base.OUT, R.FORCE_OUT),
                _move(1, Base.BATTER, Base.FIRST, R.FIELDERS_CHOICE),
            ],
        )
        self.assertEqual((credits.putouts, credits.assists), ((FP.SECOND_BASE,), (FP.SHORTSTOP,)))

    def test_fielders_choice_without_an_out_credits_nobody(self):
        credits = self._credits(P.FIELDERS_CHOICE, (FP.SHORTSTOP, FP.SECOND_BASE))
        self.assertEqual(credits, services.FieldingCredits())


class FielderResolutionTest(TestCase):
    """守備位置を、その打席の時点でそこに就いている選手に引く規則。"""

    def test_starters_hold_their_positions_and_the_pitcher_comes_from_the_plate_appearance(self):
        book = _Book()
        book.home_bats_around()
        sequence = book.add(pitcher=RELIEVER)
        alignment = services.fielders_by_plate_appearance(_game(book))[sequence]
        self.assertEqual(alignment[FP.SHORTSTOP], SHORT)
        self.assertEqual(alignment[FP.CATCHER], CATCHER)
        self.assertEqual(alignment[FP.PITCHER], RELIEVER)

    def test_pitcher_follows_each_change_of_pitcher(self):
        book = _Book()
        book.home_bats_around()
        early = book.add(pitcher=STARTER)
        late = book.add(pitcher=RELIEVER)
        alignments = services.fielders_by_plate_appearance(_game(book))
        self.assertEqual(alignments[early][FP.PITCHER], STARTER)
        self.assertEqual(alignments[late][FP.PITCHER], RELIEVER)

    def test_the_designated_hitter_is_never_a_fielder(self):
        book = _Book()
        book.home_bats_around()
        sequence = book.add()
        alignment = services.fielders_by_plate_appearance(_game(book))[sequence]
        self.assertNotIn(DH, alignment.values())
        self.assertNotIn(FP.DESIGNATED_HITTER, alignment)

    def test_the_batting_side_does_not_field(self):
        """表の間に守っているのはホーム。ビジターの打者は含まれない。"""
        book = _Book()
        book.home_bats_around()
        sequence = book.add(order=1)
        alignment = services.fielders_by_plate_appearance(_game(book))[sequence]
        self.assertNotIn(1, alignment.values())

    def test_a_pinch_hitter_takes_the_post_but_does_not_field(self):
        """代打が出た後、その位置は空く（交代の守備が記録されていない限り）。"""
        book = _Book()
        book.home_bats_around()
        before = book.add()
        entered = book.add(bottom=True, order=3, batter=40, slot=1, result=P.STRIKEOUT_LOOKING)
        after = book.add()
        game = _game(book, extra=[(40, 3, 1, FP.PINCH_HITTER, entered)])
        alignments = services.fielders_by_plate_appearance(game)
        self.assertEqual(alignments[before][FP.FIRST_BASE], FIRST)
        self.assertNotIn(FP.FIRST_BASE, alignments[after])
        self.assertNotIn(40, alignments[after].values())

    def test_a_defensive_replacement_after_a_pinch_hitter_takes_over_from_the_recorded_entry(self):
        """代打 → 守備交代。交代した選手は、記録された打席から守備に就く。"""
        book = _Book()
        book.home_bats_around()
        before = book.add()
        pinch = book.add(bottom=True, order=3, batter=40, slot=1, result=P.STRIKEOUT_LOOKING)
        after = book.add()
        later = book.add()
        game = _game(
            book,
            extra=[(40, 3, 1, FP.PINCH_HITTER, pinch), (41, 3, 2, FP.FIRST_BASE, after)],
        )
        alignments = services.fielders_by_plate_appearance(game)
        self.assertEqual(alignments[before][FP.FIRST_BASE], FIRST)
        self.assertEqual(alignments[after][FP.FIRST_BASE], 41)
        self.assertEqual(alignments[later][FP.FIRST_BASE], 41)

    def test_a_replacement_takes_over_exactly_at_the_recorded_plate_appearance(self):
        """前任の最後の打席ではなく、記録された出場の打席で入れ替わる。"""
        book = _Book()
        book.home_bats_around()  # 左翼手 25 の最後の打席は 5 番目
        early = book.add()  # 25 が打席に立った後もしばらく守っている
        still = book.add()
        entered = book.add()
        game = _game(book, extra=[(45, 5, 1, FP.LEFT_FIELD, entered)])
        alignments = services.fielders_by_plate_appearance(game)
        self.assertEqual(alignments[early][FP.LEFT_FIELD], LEFT)
        self.assertEqual(alignments[still][FP.LEFT_FIELD], LEFT)
        self.assertEqual(alignments[entered][FP.LEFT_FIELD], 45)

    def test_a_replacement_who_never_batted_is_still_placed(self):
        """打席にも走者にも現れない選手でも、チームと出場した打席が記録されていれば守備に就く。"""
        book = _Book()
        before = book.add()
        entered = book.add()
        game = _game(book, extra=[(45, 5, 1, FP.LEFT_FIELD, entered)])
        alignments = services.fielders_by_plate_appearance(game)
        self.assertEqual(alignments[before][FP.LEFT_FIELD], LEFT)
        self.assertEqual(alignments[entered][FP.LEFT_FIELD], 45)

    def test_a_replacement_whose_entry_is_unknown_is_skipped_along_with_the_player_he_replaced(self):
        """出場した打席が不明なら、その選手も、その枠の前任者の位置も解決しない（推測しない）。"""
        book = _Book()
        book.home_bats_around()
        first = book.add()
        last = book.add()
        game = _game(book, extra=[(45, 5, 1, FP.LEFT_FIELD, None)])
        alignments = services.fielders_by_plate_appearance(game)
        for sequence in (first, last):
            self.assertNotIn(FP.LEFT_FIELD, alignments[sequence])
            self.assertNotIn(45, alignments[sequence].values())
            self.assertNotIn(LEFT, alignments[sequence].values())
        # 他の位置は解決される
        self.assertEqual(alignments[last][FP.SHORTSTOP], SHORT)

    def test_two_players_at_the_same_position_at_once_are_not_resolved(self):
        """同じ時点で同じ位置に2人いるなら、後勝ちで選ばず解決しない。"""
        book = _Book()
        sequence = book.add()
        lineup = [*HOME_LINEUP[:8], (29, FP.CENTER_FIELD)]  # 9番が中堅手。6番の中堅手と重なる
        alignments = services.fielders_by_plate_appearance(_game(book, lineup=lineup))
        self.assertNotIn(FP.CENTER_FIELD, alignments[sequence])
        self.assertEqual(alignments[sequence][FP.SHORTSTOP], SHORT)

    def test_the_team_comes_from_the_lineup_not_from_the_plate_appearances(self):
        """ホームの選手がどの打席にも現れなくても、ホームの守備に就く。"""
        book = _Book()
        sequence = book.add()
        alignment = services.fielders_by_plate_appearance(_game(book))[sequence]
        self.assertEqual(alignment[FP.SHORTSTOP], SHORT)

    def test_a_game_without_a_lineup_still_resolves_the_pitcher(self):
        book = _Book()
        sequence = book.add(pitcher=RELIEVER)
        game = Game(
            season=Season(2026),
            played_on=date(2026, 4, 1),
            home_team_id=HOME,
            away_team_id=AWAY,
            stage=GameStage.REGULAR,
            plate_appearances=book.entries,
        )
        self.assertEqual(services.fielders_by_plate_appearance(game)[sequence], {FP.PITCHER: RELIEVER})
