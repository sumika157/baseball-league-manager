"""打席からの導出（スコア・打撃と投手の成績・残塁・明細との照合・併殺の定義）の単体テスト。Django も DB も使わない。"""

from datetime import date
from unittest import TestCase

from myapp.domain import services
from myapp.domain.entities import Game
from myapp.domain.exceptions import InvalidPlateAppearance, InvalidStatValue
from myapp.domain.value_objects import Base, BattingLine, GameStage, InningsPitched, PitchingLine, Season

from .pa_builders import AWAY, HOME, STARTER, P, R, _game, _half_inning, _pa, _to


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


class LeftOnBaseTest(TestCase):
    def test_runners_left_at_the_end_of_a_half_inning_are_counted(self):
        self.assertEqual(services.left_on_base(_half_inning(), is_bottom=False), 1)

    def test_the_other_side_has_none(self):
        self.assertEqual(services.left_on_base(_half_inning(), is_bottom=True), 0)


class LinesMatchPlateAppearancesTest(TestCase):
    """明細と打席の照合。

    打撃・投球の明細は打席から導ける値だが、通算成績の集計のために保存もしている
    （自責点は SQL で集計できないため）。同じ事実が2か所にあるので、集約が照合する。
    """

    def _game_with_lines(self):
        game = _game(_half_inning(), away_score=1)
        for batter in (1, 2, 3, 4, 5):
            game.record_batting(
                batter,
                services.batting_line_for(game.plate_appearances, batter),
                batting_order=batter,
                team_id=game.away_team_id,
            )
        game.record_pitching(STARTER, services.pitching_line_for(game.plate_appearances, STARTER))
        services.record_derived_fielding(game)
        return game

    def test_derived_lines_pass(self):
        services.ensure_lines_match_plate_appearances(self._game_with_lines())

    def test_a_game_without_plate_appearances_is_left_alone(self):
        """経過を記録しない試合では照合するものが無い。"""
        game = Game(
            season=Season(2026),
            played_on=date(2026, 4, 1),
            home_team_id=HOME,
            away_team_id=AWAY,
            stage=GameStage.REGULAR,
        )
        game.record_batting(1, BattingLine(at_bats=4, singles=2), team_id=game.home_team_id)

        services.ensure_lines_match_plate_appearances(game)

    def test_a_batting_line_that_drifted_is_rejected(self):
        game = self._game_with_lines()
        game.record_batting(1, BattingLine(at_bats=4, home_runs=3), batting_order=1, team_id=game.home_team_id)

        with self.assertRaises(InvalidPlateAppearance):
            services.ensure_lines_match_plate_appearances(game)

    def test_a_pitching_line_that_drifted_is_rejected(self):
        game = self._game_with_lines()
        game.record_pitching(STARTER, PitchingLine(innings=InningsPitched(outs=27)))

        with self.assertRaises(InvalidPlateAppearance):
            services.ensure_lines_match_plate_appearances(game)

    def test_a_batter_missing_from_the_box_score_is_rejected(self):
        """打席に立ったのに明細が無い選手がいると、その成績が消える。"""
        game = self._game_with_lines()
        game.batting = [entry for entry in game.batting if entry.player_id != 3]

        with self.assertRaises(InvalidPlateAppearance):
            services.ensure_lines_match_plate_appearances(game)


class DerivedBattingCountsTest(TestCase):
    """打席から数えられるようになった項目。手入力していた頃は無かったもの。"""

    def test_a_run_is_counted_for_the_runner_not_the_batter(self):
        """得点は自分の打席の外で起きる。単打で出た走者が、次の打者の本塁打で還る。"""
        entries = [
            _pa(1, 1, P.SINGLE),
            _pa(
                2,
                2,
                P.HOME_RUN,
                advances=[_to(1, Base.FIRST, Base.HOME), _to(2, Base.BATTER, Base.HOME)],
            ),
        ]

        self.assertEqual(services.batting_line_for(entries, 1).runs, 1)
        self.assertEqual(services.batting_line_for(entries, 2).runs, 1)
        # 打点は打った側にだけ2つ付く
        self.assertEqual(services.batting_line_for(entries, 2).runs_batted_in, 2)
        self.assertEqual(services.batting_line_for(entries, 1).runs_batted_in, 0)

    def test_stolen_bases_come_from_the_reason(self):
        entries = [
            _pa(1, 1, P.SINGLE),
            _pa(
                2,
                2,
                P.STRIKEOUT_SWINGING,
                advances=[_to(1, Base.FIRST, Base.SECOND, R.STOLEN_BASE), _to(2, Base.BATTER, Base.OUT, R.PUT_OUT)],
            ),
        ]

        line = services.batting_line_for(entries, 1)
        self.assertEqual((line.stolen_bases, line.caught_stealing), (1, 0))
        self.assertAlmostEqual(line.stolen_base_percentage, 1.0)

    def test_caught_stealing_is_counted_too(self):
        entries = [
            _pa(1, 1, P.SINGLE),
            _pa(
                2,
                2,
                P.WALK,
                advances=[
                    _to(1, Base.FIRST, Base.OUT, R.CAUGHT_STEALING),
                    _to(2, Base.BATTER, Base.FIRST, R.AWARDED_BASE),
                ],
            ),
        ]

        line = services.batting_line_for(entries, 1)
        self.assertEqual((line.stolen_bases, line.caught_stealing), (0, 1))
        self.assertAlmostEqual(line.stolen_base_percentage, 0.0)

    def test_strikeouts_and_sacrifices_are_counted(self):
        entries = [
            _pa(1, 1, P.SINGLE),
            _pa(
                2,
                2,
                P.SACRIFICE_BUNT,
                advances=[_to(2, Base.BATTER, Base.OUT, R.PUT_OUT), _to(1, Base.FIRST, Base.SECOND)],
            ),
            _pa(3, 3, P.STRIKEOUT_LOOKING),
        ]

        self.assertEqual(services.batting_line_for(entries, 2).sacrifice_bunts, 1)
        self.assertEqual(services.batting_line_for(entries, 3).strikeouts, 1)

    def test_a_sacrifice_bunt_counts_toward_the_qualifying_line(self):
        """規定打席の分母に犠打が入る（打数には入らない）。"""
        line = BattingLine(at_bats=3, sacrifice_bunts=1)

        self.assertEqual(line.at_bats, 3)
        self.assertEqual(line.plate_appearances, 4)
        self.assertEqual(line.plate_appearances_for_obp, 3)

    def test_intentional_walks_are_a_subset_of_walks(self):
        entries = [_pa(1, 1, P.INTENTIONAL_WALK)]

        line = services.batting_line_for(entries, 1)
        self.assertEqual((line.walks, line.intentional_walks), (1, 1))
        with self.assertRaises(InvalidStatValue):
            BattingLine(walks=1, intentional_walks=2)

    def test_a_double_play_is_charged_to_the_batter(self):
        entries = [
            _pa(1, 1, P.SINGLE),
            _pa(
                2,
                2,
                P.GROUND_OUT,
                advances=[
                    _to(1, Base.FIRST, Base.OUT, R.FORCE_OUT),
                    _to(2, Base.BATTER, Base.OUT, R.PUT_OUT),
                ],
            ),
        ]

        self.assertEqual(services.batting_line_for(entries, 2).double_plays, 1)
        self.assertEqual(services.batting_line_for(entries, 1).double_plays, 0)


class DoublePlayDefinitionTest(TestCase):
    """併殺は「打者への守備で2つ取った」こと。走塁のアウトは数えない。"""

    def _strikeout_with_caught_stealing(self):
        return _pa(
            1,
            1,
            P.STRIKEOUT_SWINGING,
            advances=[
                _to(9, Base.FIRST, Base.OUT, R.CAUGHT_STEALING),
                _to(1, Base.BATTER, Base.OUT, R.PUT_OUT),
            ],
        )

    def test_a_strikeout_with_a_caught_stealing_is_not_a_double_play(self):
        entry = self._strikeout_with_caught_stealing()

        self.assertEqual(entry.outs_recorded, 2)
        self.assertFalse(entry.is_double_play)
        self.assertEqual(services.batting_line_for([entry], 1).double_plays, 0)

    def test_a_ground_ball_that_retires_two_is_a_double_play(self):
        entry = _pa(
            1,
            1,
            P.GROUND_OUT,
            advances=[
                _to(9, Base.FIRST, Base.OUT, R.FORCE_OUT),
                _to(1, Base.BATTER, Base.OUT, R.PUT_OUT),
            ],
        )

        self.assertTrue(entry.is_double_play)

    def test_a_run_on_a_caught_stealing_play_still_earns_a_run_batted_in(self):
        """走塁のアウトで打点が消えないこと（併殺の間の得点だけが対象）。"""
        entry = _pa(
            1,
            1,
            P.SACRIFICE_FLY,
            advances=[
                _to(9, Base.FIRST, Base.OUT, R.CAUGHT_STEALING),
                _to(8, Base.THIRD, Base.HOME, R.TAG_UP),
                _to(1, Base.BATTER, Base.OUT, R.PUT_OUT),
            ],
        )

        self.assertEqual(entry.runs_batted_in, 1)
