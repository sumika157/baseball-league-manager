"""未記録の試合（打席も打撃・投球の明細も無い試合）の判定と、集計での扱い。Django も DB も使わない。

登録しただけの試合は 0-0 で入っているが、引分ではない。勝敗・順位・対戦成績・月別成績に数えない。
"""

from datetime import date
from unittest import TestCase

from myapp.domain import services
from myapp.domain.entities import Game, Team
from myapp.domain.value_objects import BattingLine, InningsPitched, PitchingLine, Season

HOME, AWAY = 1, 2


def _game(home_score=0, away_score=0, *, day=1, month=4, season=2026, record=None) -> Game:
    """record は "batting" / "pitching" / None（未記録）。"""
    game = Game(
        season=Season(season),
        played_on=date(season, month, day),
        home_team_id=HOME,
        away_team_id=AWAY,
        home_score=home_score,
        away_score=away_score,
    )
    if record == "batting":
        game.record_batting(10, BattingLine(at_bats=3), team_id=HOME)
    elif record == "pitching":
        game.record_pitching(20, PitchingLine(innings=InningsPitched.from_notation("9.0")))
    return game


def _teams() -> list[Team]:
    return [Team(id=HOME, name="ホーム", league_id=1), Team(id=AWAY, name="ビジター", league_id=1)]


class IsRecordedTest(TestCase):
    def test_a_game_without_any_lines_is_unrecorded(self):
        self.assertFalse(_game().is_recorded)

    def test_a_score_alone_does_not_make_it_recorded(self):
        """得点があっても明細が無ければ未記録（何が起きたか分からない）。"""
        self.assertFalse(_game(4, 2).is_recorded)

    def test_a_batting_line_makes_it_recorded(self):
        self.assertTrue(_game(record="batting").is_recorded)

    def test_a_pitching_line_makes_it_recorded(self):
        self.assertTrue(_game(record="pitching").is_recorded)

    def test_a_game_without_plate_appearances_but_with_lines_is_recorded(self):
        """打席を記録する前の古い試合（明細だけがある）は記録済み。"""
        game = _game(3, 1, record="batting")

        self.assertEqual(game.plate_appearances, [])
        self.assertTrue(game.is_recorded)

    def test_the_hint_is_used_when_the_lines_were_not_read(self):
        """明細を読まずに作った集約は、参照クエリが教えた答えに従う。"""
        light = Game(
            season=Season(2026),
            played_on=date(2026, 4, 1),
            home_team_id=HOME,
            away_team_id=AWAY,
            recorded_hint=True,
        )

        self.assertTrue(light.is_recorded)


class UnrecordedGamesAreNotCountedTest(TestCase):
    def test_team_record_skips_unrecorded_games(self):
        games = [_game(5, 3, record="batting"), _game(), _game(2, 2, day=3)]

        record = services.team_record(games, HOME)

        self.assertEqual((record.wins, record.losses, record.ties), (1, 0, 0))

    def test_a_scoreless_unrecorded_game_is_not_a_tie(self):
        record = services.team_record([_game(), _game()], HOME)

        self.assertEqual((record.games_played, record.ties), (0, 0))

    def test_a_recorded_scoreless_game_is_still_a_tie(self):
        record = services.team_record([_game(record="batting")], HOME)

        self.assertEqual(record.ties, 1)

    def test_standings_do_not_list_teams_with_only_unrecorded_games(self):
        self.assertEqual(services.standings(_teams(), [_game()]), [])

    def test_standings_ignore_unrecorded_games_for_a_team_that_also_played(self):
        rows = services.standings(_teams(), [_game(5, 3, record="batting"), _game(day=2)])

        self.assertEqual([row.record.games_played for row in rows], [1, 1])

    def test_head_to_head_skips_unrecorded_games(self):
        record = services.head_to_head([_game(4, 1, record="pitching"), _game(day=2)], HOME, AWAY)

        self.assertEqual((record.wins, record.games_played), (1, 1))

    def test_matchups_skip_unrecorded_games(self):
        rows = services.matchups(_teams(), [_game(4, 1, record="batting"), _game(day=2)])

        self.assertEqual([row.total.games_played for row in rows], [1, 1])

    def test_team_monthly_splits_skip_unrecorded_games(self):
        splits = services.team_monthly_splits(
            [_game(4, 1, record="batting"), _game(day=2), _game(day=1, month=5)], HOME, {10}
        )

        self.assertEqual([(s.month, s.record.games_played) for s in splits], [(4, 1)])

    def test_a_season_with_only_unrecorded_games_is_not_a_season(self):
        games = [_game(season=2025), _game(season=2026, record="batting")]

        self.assertEqual([s.year for s in services.seasons_of(games)], [2026])

    def test_recorded_games_keeps_only_recorded_ones(self):
        recorded = _game(record="batting")

        self.assertEqual(services.recorded_games([_game(), recorded, _game(day=2)]), [recorded])
