"""試合の組み立て（`assemble_game`）の単体テスト。Django も DB も使わない。

打席の記録とラインアップから、得点・打撃・投球・守備・勝敗が揃った試合ができること。
スコアブックの保存と仮想データの投入が同じ関数を通るので、ここが両方の出典になる。

3回までの、ビジターの 1-0 の勝ち。ビジターの打者は 1〜9（打順と同じ番号）、ホームの打者は 21〜29。
ホームの投手は 100（先発）→101（救援）、ビジターの投手は 200 だけ。
"""

from datetime import date
from unittest import TestCase

from myapp.domain import services
from myapp.domain.entities import PlateAppearance, RunnerAdvance
from myapp.domain.value_objects import (
    Base,
    FieldingPosition,
    GameHeader,
    LineupEntry,
    PlateAppearanceResult,
    Season,
)

P = PlateAppearanceResult
FP = FieldingPosition

HOME, AWAY = 1, 2
HOME_STARTER, HOME_RELIEVER, AWAY_PITCHER = 100, 101, 200

POSITIONS = [
    FP.SHORTSTOP,
    FP.SECOND_BASE,
    FP.FIRST_BASE,
    FP.THIRD_BASE,
    FP.LEFT_FIELD,
    FP.CENTER_FIELD,
    FP.RIGHT_FIELD,
    FP.CATCHER,
    FP.DESIGNATED_HITTER,
]
HOME_CATCHER, AWAY_CATCHER = 28, 8
HOME_SECOND_BASEMAN, HOME_DESIGNATED_HITTER = 22, 29

HEADER = GameHeader(season=Season(2026), played_on=date(2026, 4, 1), home_team_id=HOME, away_team_id=AWAY)


def _lineup() -> list[LineupEntry]:
    return [
        LineupEntry(
            team_id=team_id,
            player_id=offset + order,
            batting_order=order,
            slot_sequence=0,
            fielding_position=position,
        )
        for team_id, offset in ((AWAY, 0), (HOME, 20))
        for order, position in enumerate(POSITIONS, start=1)
    ]


def _plate_appearances() -> list[PlateAppearance]:
    """(回, 裏か, 打順, 結果, 投手) を並べた3回ぶん。ビジターの1番が初回に本塁打を打つ以外は三振。"""
    strikeout = P.STRIKEOUT_SWINGING
    plan = [
        (1, False, 1, P.HOME_RUN, HOME_STARTER),
        (1, False, 2, strikeout, HOME_STARTER),
        (1, False, 3, strikeout, HOME_STARTER),
        (1, False, 4, strikeout, HOME_STARTER),
        (1, True, 1, strikeout, AWAY_PITCHER),
        (1, True, 2, strikeout, AWAY_PITCHER),
        (1, True, 3, strikeout, AWAY_PITCHER),
        (2, False, 5, strikeout, HOME_RELIEVER),
        (2, False, 6, strikeout, HOME_RELIEVER),
        (2, False, 7, strikeout, HOME_RELIEVER),
        (2, True, 4, strikeout, AWAY_PITCHER),
        (2, True, 5, strikeout, AWAY_PITCHER),
        (2, True, 6, strikeout, AWAY_PITCHER),
        (3, False, 8, strikeout, HOME_RELIEVER),
        (3, False, 9, strikeout, HOME_RELIEVER),
        (3, False, 1, strikeout, HOME_RELIEVER),
        (3, True, 7, strikeout, AWAY_PITCHER),
        (3, True, 8, strikeout, AWAY_PITCHER),
        (3, True, 9, strikeout, AWAY_PITCHER),
    ]
    entries = []
    for sequence, (inning, is_bottom, order, result, pitcher) in enumerate(plan, start=1):
        batter = (20 if is_bottom else 0) + order
        entries.append(
            PlateAppearance(
                sequence=sequence,
                inning=inning,
                is_bottom=is_bottom,
                batter_id=batter,
                pitcher_id=pitcher,
                batting_order=order,
                result=result,
                advances=[
                    RunnerAdvance(batter, Base.BATTER, result.default_batter_base, result.default_batter_reason)
                ],
            )
        )
    return entries


class AssembleGameTest(TestCase):
    def setUp(self):
        self.game = services.assemble_game(HEADER, _lineup(), _plate_appearances())

    def test_the_header_and_the_plate_appearances_are_carried_over(self):
        self.assertIsNone(self.game.id)
        self.assertEqual(self.game.season, Season(2026))
        self.assertEqual((self.game.home_team_id, self.game.away_team_id), (HOME, AWAY))
        self.assertEqual(len(self.game.plate_appearances), 19)

    def test_the_header_id_is_kept_when_overwriting_a_saved_game(self):
        saved = GameHeader(season=Season(2026), played_on=date(2026, 4, 1), home_team_id=HOME, away_team_id=AWAY, id=7)
        self.assertEqual(services.assemble_game(saved, _lineup(), _plate_appearances()).id, 7)

    def test_scores_and_line_score_are_derived_from_the_plate_appearances(self):
        self.assertEqual((self.game.away_score, self.game.home_score), (1, 0))
        self.assertEqual(self.game.line_score.away, (1, 0, 0))
        self.assertEqual(self.game.line_score.home, (0, 0, 0))

    def test_every_lineup_slot_gets_a_batting_line_in_lineup_order(self):
        self.assertEqual([row.player_id for row in self.game.batting], [row.player_id for row in _lineup()])
        by_player = {row.player_id: row for row in self.game.batting}
        self.assertEqual(by_player[1].line.home_runs, 1)
        self.assertEqual(by_player[1].line.runs, 1)
        self.assertEqual(by_player[1].line.strikeouts, 1)
        self.assertEqual(by_player[1].team_id, AWAY)
        self.assertEqual(by_player[21].team_id, HOME)
        self.assertEqual(by_player[HOME_CATCHER].fielding_position, FP.CATCHER)

    def test_a_slot_without_plate_appearances_is_kept_as_a_zero_row(self):
        """打席が回らなかった枠も、守備には就いているのでボックススコアから消せない。"""
        lineup = _lineup()[:-1]  # ホームの指名打者を外す代わりに、打席の無い控えを載せる
        lineup.append(
            LineupEntry(
                team_id=HOME,
                player_id=99,
                batting_order=9,
                slot_sequence=1,
                fielding_position=FP.PINCH_HITTER,
                entered_sequence=7,
            )
        )
        game = services.assemble_game(HEADER, lineup, _plate_appearances()[:7])
        bench = next(row for row in game.batting if row.player_id == 99)
        self.assertEqual(bench.line.at_bats, 0)
        self.assertEqual((bench.slot_sequence, bench.batting_order), (1, 9))
        # 出場時刻は呼ぶ側が決めて渡す。組み立てでは導き直さない
        self.assertEqual(bench.entered_sequence, 7)
        self.assertIsNone(next(row for row in game.batting if row.player_id == 1).entered_sequence)

    def test_appearance_order_starts_from_one_for_each_team(self):
        """両チームの投手をまとめて数えると、相手の先発が2番手になってしまう。"""
        orders = {row.player_id: (row.appearance_order, row.entered_inning) for row in self.game.pitching}
        self.assertEqual(orders, {HOME_STARTER: (1, 1), HOME_RELIEVER: (2, 2), AWAY_PITCHER: (1, 1)})

    def test_pitching_lines_are_counted_from_the_plate_appearances(self):
        by_player = {row.player_id: row.line for row in self.game.pitching}
        self.assertEqual(by_player[HOME_STARTER].innings.outs, 3)
        self.assertEqual(by_player[HOME_STARTER].home_runs_allowed, 1)
        self.assertEqual(by_player[HOME_STARTER].runs_allowed, 1)
        self.assertEqual(by_player[HOME_RELIEVER].innings.outs, 6)
        self.assertEqual(by_player[AWAY_PITCHER].innings.outs, 9)
        self.assertEqual(by_player[AWAY_PITCHER].strikeouts, 9)

    def test_decisions_are_the_same_as_calling_pitching_decisions_directly(self):
        """勝敗・セーブ・ホールドは既存の規則（`pitching_decisions`）の結果と一致する。"""
        team_of = services.team_of_players(HEADER, _lineup(), self.game.plate_appearances)
        decisions = services.pitching_decisions(self.game, team_of)
        for outing in self.game.pitching:
            pid = outing.player_id
            wins = decisions.wins_for(pid)
            self.assertEqual(outing.line.wins, wins, pid)
            self.assertEqual(outing.line.losses, decisions.losses_for(pid), pid)
            self.assertEqual(outing.line.saves, decisions.saves_for(pid), pid)
            self.assertEqual(outing.line.holds, decisions.holds_for(pid), pid)
            self.assertEqual(outing.line.starts, 1 if outing.appearance_order == 1 else 0, pid)
            self.assertEqual(outing.line.relief_wins, wins if outing.appearance_order > 1 else 0, pid)

    def test_the_winning_and_the_losing_pitcher_are_decided(self):
        by_player = {row.player_id: row.line for row in self.game.pitching}
        # ビジターが 1-0 で勝つ。決勝点を許したのはホームの先発
        self.assertEqual(by_player[AWAY_PITCHER].wins, 1)
        self.assertEqual(by_player[HOME_STARTER].losses, 1)
        self.assertEqual(by_player[HOME_STARTER].starts, 1)
        self.assertEqual(by_player[HOME_RELIEVER].starts, 0)

    def test_fielding_lines_are_derived_and_pass_the_aggregate_check(self):
        by_player = {row.player_id: row.line for row in self.game.fielding}
        # 経路の無い三振は捕手の刺殺。ビジターの三振もホームの三振も9個
        self.assertEqual(by_player[HOME_CATCHER].putouts, 9)
        self.assertEqual(by_player[AWAY_CATCHER].putouts, 9)
        # 守備に就いた全員が 0 の行で現れる（指名打者は守備に就かない）
        self.assertIn(HOME_SECOND_BASEMAN, by_player)
        self.assertNotIn(HOME_DESIGNATED_HITTER, by_player)
        # 保存前の検査（集約の照合）を通る
        services.ensure_lines_match_plate_appearances(self.game)

    def test_a_game_without_plate_appearances_has_no_lines(self):
        game = services.assemble_game(HEADER, [], [])
        self.assertTrue(game.line_score.is_empty)
        self.assertEqual((game.home_score, game.away_score), (0, 0))
        self.assertEqual(game.pitching, [])
        self.assertEqual(game.fielding, [])


class TeamOfPlayersTest(TestCase):
    def test_pitchers_and_batters_belong_to_the_side_they_played_for(self):
        team_of = services.team_of_players(HEADER, _lineup(), _plate_appearances())
        self.assertEqual(team_of[1], AWAY)
        self.assertEqual(team_of[21], HOME)
        self.assertEqual(team_of[HOME_STARTER], HOME)
        self.assertEqual(team_of[HOME_RELIEVER], HOME)
        self.assertEqual(team_of[AWAY_PITCHER], AWAY)

    def test_a_player_not_in_the_lineup_is_placed_by_the_half_inning(self):
        team_of = services.team_of_players(HEADER, [], _plate_appearances())
        self.assertEqual(team_of[1], AWAY)
        self.assertEqual(team_of[21], HOME)
