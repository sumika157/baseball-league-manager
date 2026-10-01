"""守備成績（FieldingLine）と、打席からの導出の単体テスト。Django も DB も使わない。

ビジターが表、ホームが裏に攻める。守備を見るのは主にホーム（表の間に守る）で、
ホームの打者は id 21〜29、投手は 100・101。ビジターの打者は 1〜9、投手は 200。

**ラインアップの行にはチームが無い**ので、選手が打席に現れて初めてどちらの側か分かる。
そのためホームの打者を一巡させる（`_Book.home_bats_around`）ところから始める。
"""

from datetime import date
from unittest import TestCase

from myapp.domain import services
from myapp.domain.entities import FieldingError, Game, GameBatting, PlateAppearance, RunnerAdvance
from myapp.domain.exceptions import InvalidGame, InvalidPlateAppearance, InvalidStatValue
from myapp.domain.value_objects import (
    AdvanceReason,
    Base,
    BattingLine,
    ErrorKind,
    FieldingLine,
    FieldingPosition,
    PlateAppearanceResult,
    Season,
)

P = PlateAppearanceResult
R = AdvanceReason
FP = FieldingPosition

HOME, AWAY = 1, 2
STARTER, RELIEVER, AWAY_PITCHER = 100, 101, 200

# 打順 → (選手 id, 守備位置)。指名打者制で、投手は打順にいない
HOME_LINEUP = [
    (21, FP.SHORTSTOP),
    (22, FP.SECOND_BASE),
    (23, FP.FIRST_BASE),
    (24, FP.THIRD_BASE),
    (25, FP.LEFT_FIELD),
    (26, FP.CENTER_FIELD),
    (27, FP.RIGHT_FIELD),
    (28, FP.CATCHER),
    (29, FP.DESIGNATED_HITTER),
]
SHORT, SECOND, FIRST, THIRD, LEFT, CENTER, RIGHT, CATCHER, DH = (player for player, _ in HOME_LINEUP)


def _move(runner_id, frm, to, reason) -> RunnerAdvance:
    return RunnerAdvance(runner_id=runner_id, from_base=frm, to_base=to, reason=reason)


class _Book:
    """打席を積んでいく。通し番号は積んだ順に振り、`add` が返す。"""

    def __init__(self) -> None:
        self.entries: list[PlateAppearance] = []

    def add(
        self,
        *,
        bottom=False,
        order=1,
        result=P.GROUND_OUT,
        batter=None,
        pitcher=None,
        slot=0,
        fielded_by=(),
        advances=None,
        errors=(),
    ) -> int:
        sequence = len(self.entries) + 1
        batter_id = batter if batter is not None else (20 if bottom else 0) + order
        if advances is None:
            advances = [_move(batter_id, Base.BATTER, result.default_batter_base, result.default_batter_reason)]
        self.entries.append(
            PlateAppearance(
                sequence=sequence,
                inning=1,
                is_bottom=bottom,
                batter_id=batter_id,
                pitcher_id=pitcher if pitcher is not None else (AWAY_PITCHER if bottom else STARTER),
                batting_order=order,
                slot_sequence=slot,
                result=result,
                fielded_by=tuple(fielded_by),
                advances=list(advances),
                errors=list(errors),
            )
        )
        return sequence

    def home_bats_around(self) -> None:
        """ホームの打者を一巡させる。これでホームの全員が「裏に攻める側」だと分かる。"""
        for order in range(1, 10):
            self.add(bottom=True, order=order, result=P.STRIKEOUT_SWINGING)


def _game(book: _Book, *, extra=(), lineup=None) -> Game:
    """記録から試合を作る。

    extra は途中出場の行 (選手, 打順, 交代の順, 守備位置, 出場した打席)。ホームの行として足す。
    lineup はホームのスタメンを差し替えるとき（打順 → (選手 id, 守備位置)）。
    """
    game = Game(
        season=Season(2026),
        played_on=date(2026, 4, 1),
        home_team_id=HOME,
        away_team_id=AWAY,
        plate_appearances=list(book.entries),
    )
    for order, (player, position) in enumerate(lineup or HOME_LINEUP, start=1):
        game.record_batting(player, BattingLine(), team_id=HOME, batting_order=order, fielding_position=position)
    for player, order, slot, position, entered in extra:
        game.record_batting(
            player,
            BattingLine(),
            team_id=HOME,
            batting_order=order,
            slot_sequence=slot,
            fielding_position=position,
            entered_sequence=entered,
        )
    # 打席に立った選手は打撃成績の行が要る（照合が見る）。ビジターの打者は id が 20 未満
    recorded = {entry.player_id for entry in game.batting}
    for batter in {entry.batter_id for entry in book.entries} - recorded:
        game.record_batting(batter, BattingLine(), team_id=HOME if batter >= 20 else AWAY)
    for entry in game.batting:
        entry.line = services.batting_line_for(game.plate_appearances, entry.player_id)
    return game


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
            plate_appearances=book.entries,
        )
        self.assertEqual(services.fielders_by_plate_appearance(game)[sequence], {FP.PITCHER: RELIEVER})


class FieldingLinesTest(TestCase):
    def _lines(self, book, **kwargs):
        return services.fielding_lines_for(_game(book, **kwargs))

    def test_ground_out_6_3(self):
        book = _Book()
        book.home_bats_around()
        book.add(fielded_by=(FP.SHORTSTOP, FP.FIRST_BASE))
        lines = self._lines(book)
        self.assertEqual(lines[SHORT], FieldingLine(assists=1))
        self.assertEqual(lines[FIRST], FieldingLine(putouts=1))
        self.assertEqual(lines[SECOND], FieldingLine())

    def test_fly_out_to_center(self):
        book = _Book()
        book.home_bats_around()
        book.add(result=P.FLY_OUT, fielded_by=(FP.CENTER_FIELD,))
        self.assertEqual(self._lines(book)[CENTER], FieldingLine(putouts=1))

    def test_sacrifice_bunt_1_3(self):
        book = _Book()
        book.home_bats_around()
        book.add(
            result=P.SACRIFICE_BUNT,
            fielded_by=(FP.PITCHER, FP.FIRST_BASE),
            advances=[_move(1, Base.BATTER, Base.OUT, R.PUT_OUT), _move(2, Base.FIRST, Base.SECOND, R.BATTED_BALL)],
        )
        lines = self._lines(book)
        self.assertEqual(lines[STARTER], FieldingLine(assists=1))
        self.assertEqual(lines[FIRST], FieldingLine(putouts=1))

    def test_double_play_6_4_3(self):
        book = _Book()
        book.home_bats_around()
        book.add(
            fielded_by=(FP.SHORTSTOP, FP.SECOND_BASE, FP.FIRST_BASE),
            advances=[_move(1, Base.BATTER, Base.OUT, R.PUT_OUT), _move(2, Base.FIRST, Base.OUT, R.FORCE_OUT)],
        )
        lines = self._lines(book)
        self.assertEqual(lines[SHORT], FieldingLine(assists=1, double_plays_turned=1))
        self.assertEqual(lines[SECOND], FieldingLine(putouts=1, assists=1, double_plays_turned=1))
        self.assertEqual(lines[FIRST], FieldingLine(putouts=1, double_plays_turned=1))
        self.assertEqual(lines[THIRD], FieldingLine())

    def test_a_player_appearing_twice_in_a_double_play_is_counted_once_for_it(self):
        """3-6-3: 一塁手は補殺と刺殺を1つずつ、併殺参加は1つ。"""
        book = _Book()
        book.home_bats_around()
        book.add(
            fielded_by=(FP.FIRST_BASE, FP.SHORTSTOP, FP.FIRST_BASE),
            advances=[_move(1, Base.BATTER, Base.OUT, R.PUT_OUT), _move(2, Base.FIRST, Base.OUT, R.FORCE_OUT)],
        )
        lines = self._lines(book)
        self.assertEqual(lines[FIRST], FieldingLine(putouts=1, assists=1, double_plays_turned=1))
        self.assertEqual(lines[SHORT], FieldingLine(putouts=1, assists=1, double_plays_turned=1))

    def test_strikeout_is_the_catchers_putout(self):
        book = _Book()
        book.home_bats_around()
        book.add(result=P.STRIKEOUT_SWINGING)
        book.add(result=P.STRIKEOUT_LOOKING)
        self.assertEqual(self._lines(book)[CATCHER], FieldingLine(putouts=2))

    def test_errors_are_counted_from_the_error_records(self):
        book = _Book()
        book.home_bats_around()
        book.add(
            result=P.REACHED_ON_ERROR,
            advances=[_move(1, Base.BATTER, Base.FIRST, R.ERROR)],
            errors=[FieldingError(SECOND, FP.SECOND_BASE, ErrorKind.FIELDING)],
        )
        lines = self._lines(book)
        self.assertEqual(lines[SECOND], FieldingLine(errors=1))
        self.assertEqual(lines[SECOND].total_chances, 1)
        self.assertEqual(lines[SECOND].fielding_percentage, 0.0)

    def test_an_error_by_someone_not_in_the_lineup_is_still_counted(self):
        book = _Book()
        book.home_bats_around()
        book.add(
            result=P.REACHED_ON_ERROR,
            advances=[_move(1, Base.BATTER, Base.FIRST, R.ERROR)],
            errors=[FieldingError(77, FP.SECOND_BASE, ErrorKind.FIELDING)],
        )
        self.assertEqual(self._lines(book)[77], FieldingLine(errors=1))

    def test_everyone_who_took_the_field_has_a_row_even_without_chances(self):
        book = _Book()
        book.home_bats_around()
        book.add(pitcher=RELIEVER)
        lines = self._lines(book)
        for player in (SHORT, SECOND, FIRST, THIRD, LEFT, CENTER, RIGHT, CATCHER, RELIEVER):
            with self.subTest(player=player):
                self.assertIn(player, lines)
        self.assertNotIn(DH, lines)

    def test_an_unresolvable_position_is_skipped_without_an_error(self):
        """代打が居座って一塁が空いた後の 6-3。補殺だけが付き、刺殺は誰にも付かない。"""
        book = _Book()
        book.home_bats_around()
        entered = book.add(bottom=True, order=3, batter=40, slot=1, result=P.STRIKEOUT_LOOKING)
        book.add(fielded_by=(FP.SHORTSTOP, FP.FIRST_BASE))
        lines = self._lines(book, extra=[(40, 3, 1, FP.PINCH_HITTER, entered)])
        self.assertEqual(lines[SHORT], FieldingLine(assists=1))
        self.assertEqual(sum(line.putouts for line in lines.values()), 0)

    def test_a_game_without_plate_appearances_has_no_fielding(self):
        self.assertEqual(self._lines(_Book()), {})

    def test_line_for_one_player_defaults_to_an_empty_line(self):
        book = _Book()
        book.home_bats_around()
        book.add(fielded_by=(FP.SHORTSTOP, FP.FIRST_BASE))
        game = _game(book)
        self.assertEqual(services.fielding_line_for(game, SHORT), FieldingLine(assists=1))
        self.assertEqual(services.fielding_line_for(game, 9999), FieldingLine())

    def test_both_teams_are_derived_independently(self):
        """裏の守備はビジターの側。ビジターのラインアップがあればそちらにも付く。"""
        book = _Book()
        book.home_bats_around()
        book.add(order=1, fielded_by=(FP.SHORTSTOP, FP.FIRST_BASE))
        for order in range(1, 10):
            book.add(order=order, result=P.STRIKEOUT_SWINGING)
        book.add(bottom=True, order=1, batter=21, result=P.FLY_OUT, fielded_by=(FP.LEFT_FIELD,))
        game = _game(book)
        # ビジターの左翼手を 3 番目の打者の id とは別に与える
        game.record_batting(7, BattingLine(), team_id=AWAY, batting_order=7, fielding_position=FP.LEFT_FIELD)
        for entry in game.batting:
            entry.line = services.batting_line_for(game.plate_appearances, entry.player_id)
        self.assertEqual(services.fielding_line_for(game, 7), FieldingLine(putouts=1))


class FieldingInvariantTest(TestCase):
    """打球・三振でアウトになった数と、守備側の刺殺の合計が一致すること。"""

    @staticmethod
    def _expected_outs(entries) -> int:
        """守備が取ったアウト。凡打・三振・犠打・犠飛は1つ、併殺は2つ、野選は走者の封殺1つ。"""
        total = 0
        for entry in entries:
            if entry.result.retires_batter:
                total += 2 if entry.is_double_play else 1
            elif entry.result is P.FIELDERS_CHOICE:
                total += sum(1 for a in entry.advances if a.is_out and not a.is_batter)
        return total

    def test_putouts_add_up_to_the_outs_made_by_fielding(self):
        book = _Book()

        def double(batter):
            return [_move(batter, Base.BATTER, Base.OUT, R.PUT_OUT), _move(9, Base.FIRST, Base.OUT, R.FORCE_OUT)]

        book.add(order=1, fielded_by=(FP.SHORTSTOP, FP.FIRST_BASE))  # 6-3
        book.add(order=2, result=P.STRIKEOUT_SWINGING)  # 捕手
        book.add(order=3, result=P.FLY_OUT, fielded_by=(FP.CENTER_FIELD,))  # 8
        book.add(order=4, fielded_by=(FP.SHORTSTOP, FP.SECOND_BASE, FP.FIRST_BASE), advances=double(4))  # 6-4-3
        book.add(order=5, fielded_by=(FP.SECOND_BASE, FP.FIRST_BASE), advances=double(5))  # 4-3 併殺
        book.add(order=6, result=P.LINE_OUT, fielded_by=(FP.FIRST_BASE,), advances=double(6))  # 3 単独併殺
        book.add(
            order=7,
            result=P.FIELDERS_CHOICE,
            fielded_by=(FP.SHORTSTOP, FP.SECOND_BASE),
            advances=[
                _move(9, Base.FIRST, Base.OUT, R.FORCE_OUT),
                _move(7, Base.BATTER, Base.FIRST, R.FIELDERS_CHOICE),
            ],
        )
        book.add(order=8, result=P.FLY_OUT, fielded_by=(FP.RIGHT_FIELD,))
        game = _game(book)

        lines = services.fielding_lines_for(game)

        home_putouts = sum(
            line.putouts for player, line in lines.items() if player >= 21 or player in (STARTER, RELIEVER)
        )
        self.assertEqual(home_putouts, self._expected_outs(book.entries))
        self.assertEqual(self._expected_outs(book.entries), 1 + 1 + 1 + 2 + 2 + 2 + 1 + 1)


class FieldingConsistencyTest(TestCase):
    """保存前の照合。守備成績は打席から導いた値と一致していなければならない。"""

    def _recorded_game(self) -> Game:
        book = _Book()
        book.home_bats_around()
        book.add(fielded_by=(FP.SHORTSTOP, FP.FIRST_BASE))
        game = _game(book)
        services.record_derived_fielding(game)
        return game

    def test_derived_lines_pass(self):
        services.ensure_lines_match_plate_appearances(self._recorded_game())

    def test_a_game_with_plate_appearances_but_no_fielding_is_rejected(self):
        """呼び忘れたまま保存すると、既存の守備行が全部消える。だから弾く。"""
        game = self._recorded_game()
        game.fielding.clear()
        with self.assertRaisesRegex(InvalidPlateAppearance, "守備成績"):
            services.ensure_lines_match_plate_appearances(game)

    def test_a_rewritten_line_is_rejected(self):
        game = self._recorded_game()
        game.record_fielding(SHORT, FieldingLine(assists=5))
        with self.assertRaisesRegex(InvalidPlateAppearance, "守備成績が打席の記録と一致しません"):
            services.ensure_lines_match_plate_appearances(game)

    def test_a_missing_fielder_is_rejected(self):
        game = self._recorded_game()
        game.fielding = [entry for entry in game.fielding if entry.player_id != SECOND]
        with self.assertRaisesRegex(InvalidPlateAppearance, "守備成績がありません"):
            services.ensure_lines_match_plate_appearances(game)

    def test_a_row_for_someone_who_never_fielded_must_be_empty(self):
        game = self._recorded_game()
        game.record_fielding(9999, FieldingLine(putouts=1))
        with self.assertRaises(InvalidPlateAppearance):
            services.ensure_lines_match_plate_appearances(game)

    def test_an_error_by_someone_other_than_the_fielder_at_that_position_is_rejected(self):
        book = _Book()
        book.add(
            result=P.REACHED_ON_ERROR,
            advances=[_move(1, Base.BATTER, Base.FIRST, R.ERROR)],
            errors=[FieldingError(77, FP.SECOND_BASE, ErrorKind.FIELDING)],  # 二塁手は 22
        )
        game = _game(book)
        services.record_derived_fielding(game)
        with self.assertRaisesRegex(InvalidGame, "失策の守備者"):
            services.ensure_lines_match_plate_appearances(game)

    def test_an_error_by_the_fielder_at_that_position_passes(self):
        book = _Book()
        book.add(
            result=P.REACHED_ON_ERROR,
            advances=[_move(1, Base.BATTER, Base.FIRST, R.ERROR)],
            errors=[FieldingError(SECOND, FP.SECOND_BASE, ErrorKind.FIELDING)],
        )
        game = _game(book)
        services.record_derived_fielding(game)
        services.ensure_lines_match_plate_appearances(game)

    def test_an_error_at_an_unresolvable_position_is_not_compared(self):
        """出場した打席が不明で位置が解決できないときは、誰の失策でも比べない。"""
        book = _Book()
        book.add(
            result=P.REACHED_ON_ERROR,
            advances=[_move(1, Base.BATTER, Base.FIRST, R.ERROR)],
            errors=[FieldingError(77, FP.LEFT_FIELD, ErrorKind.FIELDING)],
        )
        game = _game(book, extra=[(45, 5, 1, FP.LEFT_FIELD, None)])
        services.record_derived_fielding(game)
        services.ensure_lines_match_plate_appearances(game)


class LineupEntryTest(TestCase):
    """ラインアップの行が持つチームと出場した打席の検査。"""

    def test_a_starter_cannot_have_an_entry(self):
        with self.assertRaises(InvalidGame):
            GameBatting(player_id=1, line=BattingLine(), team_id=HOME, slot_sequence=0, entered_sequence=3)

    def test_the_entry_must_be_positive(self):
        with self.assertRaises(InvalidGame):
            GameBatting(player_id=1, line=BattingLine(), team_id=HOME, slot_sequence=1, entered_sequence=0)

    def test_the_team_must_be_one_of_the_two(self):
        game = _game(_Book())
        with self.assertRaises(InvalidGame):
            game.record_batting(1, BattingLine(), team_id=99)

    def test_an_entry_beyond_the_recorded_plate_appearances_is_rejected(self):
        book = _Book()
        book.add()
        game = _game(book, extra=[(45, 5, 1, FP.LEFT_FIELD, 50)])
        services.record_derived_fielding(game)
        with self.assertRaises(InvalidGame):
            services.ensure_lines_match_plate_appearances(game)

    def test_a_later_substitute_cannot_enter_before_an_earlier_one(self):
        book = _Book()
        for _ in range(5):
            book.add()
        game = _game(book, extra=[(45, 5, 1, FP.LEFT_FIELD, 4), (46, 5, 2, FP.LEFT_FIELD, 2)])
        services.record_derived_fielding(game)
        with self.assertRaises(InvalidGame):
            services.ensure_lines_match_plate_appearances(game)

    def test_a_player_who_appears_after_his_replacement_entered_is_rejected(self):
        """交代で退いたはずの選手が、後任の出場後に打席に立つ記録は成立しない。"""
        book = _Book()
        book.home_bats_around()  # 左翼手 25 は 5 番目の打席（通し番号 5）に立つ
        game = _game(book, extra=[(45, 5, 1, FP.LEFT_FIELD, 3)])
        services.record_derived_fielding(game)
        with self.assertRaisesRegex(InvalidGame, "退いたはずの選手"):
            services.ensure_lines_match_plate_appearances(game)

    def test_a_replacement_entering_after_the_last_appearance_passes(self):
        book = _Book()
        book.home_bats_around()
        entered = book.add()
        game = _game(book, extra=[(45, 5, 1, FP.LEFT_FIELD, entered)])
        services.record_derived_fielding(game)
        services.ensure_lines_match_plate_appearances(game)
