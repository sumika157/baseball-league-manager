"""ポストシーズンの方式を年ごとに導く・方式を変えられる条件・個人成績の既定の並び（#39 段階 S4b。#280）。

Django も DB も使わない。

守りたいこと:
- その年の試合から方式を導く（試合なし → なし・CS あり → NPB・日本シリーズだけ → 優勝球団）
- 全リーグが1球団の NPB 方式は「優勝球団」と導くが、勝ち上がりは同じ
- 方式を変えられないのは、いまの年度のポストシーズンが始まっているときと、終了した世界だけ
- 個人成績の既定の並びは、公式戦と同じキーの一覧にある
"""

import unittest
from datetime import date, timedelta

from myapp.domain.exceptions import InvalidPostseason
from myapp.domain.pennant.postseason import (
    DAYS_BEFORE_POSTSEASON,
    FinalStanding,
    LeagueFinish,
    PostseasonBracket,
    PostseasonFormat,
    PostseasonResult,
    played_format,
)
from myapp.domain.pennant.world import postseason_format_change_blocker
from myapp.domain.services.sorting import (
    BATTER_SORT_KEYS,
    DEFAULT_POSTSEASON_BATTER_SORT,
    DEFAULT_POSTSEASON_PITCHER_SORT,
    PITCHER_SORT_KEYS,
)
from myapp.domain.value_objects import TeamRecord

LAST_REGULAR = date(2026, 9, 15)
# 球団 id → リーグ id（1・2・3 はリーグ 100、11・12・13 はリーグ 200）
LEAGUE_OF = {1: 100, 2: 100, 3: 100, 11: 200, 12: 200, 13: 200}


def _game(home: int, away: int, *, day: int = 0) -> PostseasonResult:
    return PostseasonResult(
        played_on=LAST_REGULAR + timedelta(days=DAYS_BEFORE_POSTSEASON + day),
        home_team_id=home,
        away_team_id=away,
        winner_team_id=home,
    )


class PlayedFormatTest(unittest.TestCase):
    def test_no_games_means_no_postseason(self):
        self.assertIs(played_format([], LEAGUE_OF), PostseasonFormat.NONE)

    def test_a_game_between_clubs_of_the_same_league_is_a_climax_series(self):
        games = [_game(11, 1), _game(2, 3)]  # 日本シリーズと CS が混ざっていても、CS があれば NPB

        self.assertIs(played_format(games, LEAGUE_OF), PostseasonFormat.NPB)

    def test_only_games_between_leagues_is_the_champions_format(self):
        self.assertIs(played_format([_game(1, 11), _game(11, 1, day=1)], LEAGUE_OF), PostseasonFormat.CHAMPIONS)

    def test_an_unknown_club_is_rejected(self):
        with self.assertRaises(InvalidPostseason):
            played_format([_game(1, 99)], LEAGUE_OF)

    def test_npb_with_one_club_per_league_derives_the_same_bracket_as_champions(self):
        """全リーグが1球団の NPB 方式は CS が無く、日本シリーズの試合だけになる（`CHAMPIONS` と導く）。

        導いた方式で勝ち上がりを導き直しても、同じ対戦・同じ優勝になる（答えが変わらない）。
        """
        leagues = [
            LeagueFinish(league_id=league_id, standings=(FinalStanding(team_id, TeamRecord(wins=80 - i, losses=60)),))
            for i, (league_id, team_id) in enumerate(((100, 1), (200, 11)))
        ]
        npb = PostseasonBracket.derive(PostseasonFormat.NPB, leagues, [100, 200], LAST_REGULAR)
        results = []
        while (day := npb.next_day()) is not None:
            played = [
                PostseasonResult(
                    played_on=day.played_on,
                    home_team_id=game.home_team_id,
                    away_team_id=game.away_team_id,
                    winner_team_id=game.home_team_id,
                )
                for game in day.games
            ]
            results.extend(played)
            npb = npb.played(played)

        derived = played_format(results, {1: 100, 11: 200})
        champions = PostseasonBracket.derive(derived, leagues, [100, 200], LAST_REGULAR, results)

        self.assertIs(derived, PostseasonFormat.CHAMPIONS)
        self.assertEqual(champions.champion, npb.champion)
        self.assertEqual(
            [(s.name, s.upper_team_id, s.lower_team_id, s.games) for s in champions.series()],
            [(s.name, s.upper_team_id, s.lower_team_id, s.games) for s in npb.series()],
        )


class FormatChangeBlockerTest(unittest.TestCase):
    def test_can_change_before_the_postseason_starts(self):
        self.assertIsNone(postseason_format_change_blocker(started=False, world_over=False))

    def test_cannot_change_once_this_years_postseason_has_started(self):
        reason = postseason_format_change_blocker(started=True, world_over=False)

        self.assertIsNotNone(reason)
        self.assertIn("始まっている", reason or "")

    def test_cannot_change_a_world_that_is_over(self):
        for started in (False, True):
            with self.subTest(started=started):
                reason = postseason_format_change_blocker(started=started, world_over=True)
                self.assertIn("最終シーズンを終えている", reason or "")


class PostseasonStatsSortTest(unittest.TestCase):
    def test_the_defaults_are_among_the_regular_season_keys(self):
        self.assertIn(DEFAULT_POSTSEASON_BATTER_SORT, BATTER_SORT_KEYS)
        self.assertIn(DEFAULT_POSTSEASON_PITCHER_SORT, PITCHER_SORT_KEYS)

    def test_the_defaults_are_counts_in_descending_order(self):
        """試合数が少ない表なので、率でなく積み上げの数（多い順）で並べる。"""
        self.assertEqual(DEFAULT_POSTSEASON_BATTER_SORT, "hits")
        self.assertEqual(DEFAULT_POSTSEASON_PITCHER_SORT, "innings")
        self.assertTrue(BATTER_SORT_KEYS[DEFAULT_POSTSEASON_BATTER_SORT][1])
        self.assertTrue(PITCHER_SORT_KEYS[DEFAULT_POSTSEASON_PITCHER_SORT][1])
