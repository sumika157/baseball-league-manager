"""打席から導く守備の明細と、その不変条件・照合・ラインアップの行の単体テスト。Django も DB も使わない。

ビジターが表、ホームが裏に攻める。守備を見るのは主にホーム（表の間に守る）で、
ホームの打者は id 21〜29、投手は 100・101。ビジターの打者は 1〜9、投手は 200。

**ラインアップの行にはチームが無い**ので、選手が打席に現れて初めてどちらの側か分かる。
そのためホームの打者を一巡させる（`_Book.home_bats_around`）ところから始める。
"""

from unittest import TestCase

from myapp.domain import services
from myapp.domain.entities import FieldingError, Game, GameBatting
from myapp.domain.exceptions import InvalidGame, InvalidPlateAppearance
from myapp.domain.value_objects import Base, BattingLine, ErrorKind, FieldingLine

from .fielding_builders import (
    AWAY,
    CATCHER,
    CENTER,
    DH,
    FIRST,
    FP,
    HOME,
    LEFT,
    RELIEVER,
    RIGHT,
    SECOND,
    SHORT,
    STARTER,
    THIRD,
    P,
    R,
    _Book,
    _game,
    _move,
)


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
