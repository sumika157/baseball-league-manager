"""見本のリーグ（`domain/sample/`。W5 #81）。DB 不要。

期待値の出典は #81 の計画（2. やること・推奨で進めるもの 2〜6・7. テスト計画）: 1リーグ6球団・各25人（投手12・捕手2・
内野手6・外野手5。全員支配下）・外国人は1球団2人で外国人枠の内側・1球団12試合（全36試合、月曜は休み、開幕は
`default_opening_day`）・試合はすべて見本の年の今日より前・同じ key と年なら同じ見本・乱数は `random()` だけ。
順位表とタイトルを空にしないため、規定打席（試合数 × 3.1）と規定投球回（試合数 × 1.0）に届く選手がいること。
点数・打率などの具体的な値は固定しない。
"""

from __future__ import annotations

import unittest
from collections import Counter
from datetime import date, timedelta
from unittest import mock

from myapp.domain.entities import Game
from myapp.domain.pennant.club_plan import ClubMember, ensure_roster_playable
from myapp.domain.pennant.schedule import MONDAY, Fixture
from myapp.domain.sample import league as sample
from myapp.domain.sample.league import (
    SAMPLE_GAMES_PER_TEAM,
    SAMPLE_PLAYERS_PER_TEAM,
    SAMPLE_SEASON_DAYS,
    SAMPLE_TEAM_COUNT,
    SampleLeague,
    draw_sample_league,
    opening_day,
    play_sample_games,
    sample_club,
    sample_fixtures,
    sample_year,
)
from myapp.domain.sample.names import SAMPLE_CLUBS
from myapp.domain.services.rankings import required_outs, required_plate_appearances
from myapp.domain.services.scoring import ensure_lines_match_plate_appearances
from myapp.domain.value_objects import (
    DEVELOPMENTAL_MIN_NUMBER,
    AcquisitionRoute,
    ContractStatus,
    ForeignPlayerRules,
    JerseyNumber,
    Position,
    StadiumProfile,
)

from .doubles import OnlyRandom

YEAR = 2025
KEY = "sample-key"
# 保存した球団の id の代わり（T2 では保存後の id が入る）
TEAM_IDS = (11, 12, 13, 14, 15, 16)


def play(league: SampleLeague, *, key: str = KEY, team_ids=TEAM_IDS, fixtures=None):
    clubs = [
        sample_club(team, team_id=team_id, player_ids=[team_id * 100 + i for i in range(len(team.players))])
        for team_id, team in zip(team_ids, league.teams, strict=True)
    ]
    if fixtures is None:
        fixtures = sample_fixtures(team_ids, key=key, year=league.year)
    capacities = {team_id: team.stadium.profile.capacity for team_id, team in zip(team_ids, league.teams, strict=True)}
    return play_sample_games(
        clubs, fixtures, key=key, year=league.year, foreign_rules=league.foreign_rules, capacities=capacities
    )


def box(game: Game) -> tuple:
    """試合の比べる形（日付・対戦・得点・打席の数）。"""
    return (
        game.played_on,
        game.home_team_id,
        game.away_team_id,
        game.home_score,
        game.away_score,
        len(game.plate_appearances),
    )


class SampleLeagueShapeTest(unittest.TestCase):
    league: SampleLeague

    """名簿・リーグ・球場の形。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.league = draw_sample_league(KEY, YEAR)

    def test_one_league_of_six_teams_with_their_own_stadiums(self) -> None:
        league = self.league
        self.assertEqual(len(league.teams), SAMPLE_TEAM_COUNT)
        self.assertEqual(len({team.name for team in league.teams}), SAMPLE_TEAM_COUNT)
        self.assertEqual(len({stadium.name for stadium in league.stadiums}), SAMPLE_TEAM_COUNT)
        self.assertEqual(league.year, YEAR)
        # 外国人枠と支配下の登録枠は NPB の既定（リーグの既定値と同じ）
        self.assertEqual(league.foreign_rules, ForeignPlayerRules.NPB)
        self.assertEqual(league.registered_player_limit, 70)

    def test_stadium_choices_come_from_the_value_object(self) -> None:
        """屋根・グラウンドは `StadiumProfile` の選択肢の中（作るときに `StadiumProfile` が検査する）。"""
        for stadium in self.league.stadiums:
            with self.subTest(stadium=stadium.name):
                self.assertIn(stadium.profile.roof, StadiumProfile.ROOFS)
                self.assertIn(stadium.profile.surface, StadiumProfile.SURFACES)
                self.assertGreater(stadium.profile.capacity or 0, 0)
                self.assertTrue(stadium.profile.city)

    def test_each_team_has_25_players_by_position(self) -> None:
        expected = {Position.PITCHER: 12, Position.CATCHER: 2, Position.INFIELDER: 6, Position.OUTFIELDER: 5}
        self.assertEqual(sum(expected.values()), SAMPLE_PLAYERS_PER_TEAM)
        for team in self.league.teams:
            with self.subTest(team=team.name):
                self.assertEqual(len(team.players), SAMPLE_PLAYERS_PER_TEAM)
                self.assertEqual(Counter(p.position for p in team.players), expected)

    def test_numbers_are_unique_registered_numbers_within_each_team(self) -> None:
        """全員支配下なので、背番号は 00・0〜99（3桁は育成の番号）。球団の中で重ならない。"""
        for team in self.league.teams:
            numbers = [p.number for p in team.players]
            with self.subTest(team=team.name):
                self.assertEqual(len(numbers), len(set(numbers)))
                self.assertTrue(all(int(n) < DEVELOPMENTAL_MIN_NUMBER for n in numbers))
                for player in team.players:
                    player.contract.ensure_number_fits(JerseyNumber(player.number))

    def test_names_are_unique_across_the_league(self) -> None:
        names = [p.name for team in self.league.teams for p in team.players]
        self.assertEqual(len(names), len(set(names)))

    def test_back_names_mark_the_same_surname_within_a_team(self) -> None:
        """同じ球団に同じ苗字の選手がいれば名前の頭文字を付ける（「K.SATO」。`generator.back_name`）。いなければ苗字だけ。"""
        for index in range(20):
            for team in draw_sample_league(f"key-{index}", YEAR).teams:
                surnames = Counter(p.profile.back_name.split(".")[-1] for p in team.players)
                for player in team.players:
                    back_name = player.profile.back_name
                    with self.subTest(key=index, team=team.name, back_name=back_name):
                        self.assertTrue(back_name)
                        shared = surnames[back_name.split(".")[-1]] > 1
                        self.assertEqual("." in back_name, shared)

    def test_two_foreign_players_per_team_within_the_quota(self) -> None:
        for team in self.league.teams:
            foreign = [p.position for p in team.players if p.is_foreign]
            with self.subTest(team=team.name):
                self.assertEqual(len(foreign), 2)
                # 1軍（25人は全員1軍）に置いても枠の内側
                self.league.foreign_rules.ensure_players_within(foreign, subject="1軍登録")

    def test_everyone_is_registered_and_joined_by_the_sample_year(self) -> None:
        """全員支配下。日本人はドラフト（本指名）、外国人は新外国人で入り、加入は見本の年まで（その年の試合に出られる）。"""
        for team in self.league.teams:
            for player in team.players:
                with self.subTest(team=team.name, player=player.name):
                    self.assertIs(player.contract, ContractStatus.REGISTERED)
                    self.assertLessEqual(player.from_year, YEAR)
                    if player.is_foreign:
                        self.assertEqual(player.acquired_via, AcquisitionRoute.NEW_FOREIGN)
                    else:
                        self.assertEqual(player.acquired_via, AcquisitionRoute.DRAFT)
                        assert player.profile.draft is not None
                        self.assertFalse(player.profile.draft.is_development)

    def test_names_are_fictional_and_fixed(self) -> None:
        """リーグ・チーム・球場の名前は domain に置いた固定の名前（key で変わらない）。"""
        other = draw_sample_league("another-key", YEAR)
        self.assertEqual([t.name for t in self.league.teams], [club.team for club in SAMPLE_CLUBS])
        self.assertEqual([t.name for t in other.teams], [club.team for club in SAMPLE_CLUBS])
        self.assertEqual(self.league.name, other.name)


class SampleLeagueRandomnessTest(unittest.TestCase):
    def test_the_same_key_and_year_give_the_same_sample(self) -> None:
        self.assertEqual(draw_sample_league(KEY, YEAR), draw_sample_league(KEY, YEAR))
        self.assertEqual(sample_fixtures(TEAM_IDS, key=KEY, year=YEAR), sample_fixtures(TEAM_IDS, key=KEY, year=YEAR))

    def test_a_different_key_changes_the_players(self) -> None:
        def names(key: str) -> list[str]:
            return [p.name for team in draw_sample_league(key, YEAR).teams for p in team.players]

        self.assertNotEqual(names(KEY), names("another-key"))
        self.assertNotEqual(names(KEY), [p.name for t in draw_sample_league(KEY, YEAR + 1).teams for p in t.players])

    def test_runs_on_random_only(self) -> None:
        """名簿・日程・試合のどれも `random()` だけを使う（`OnlyRandom` は他のメソッドを持たない）。"""
        with mock.patch.object(sample, "make_random", OnlyRandom):
            league = draw_sample_league(KEY, YEAR)
            fixtures = sample_fixtures(TEAM_IDS, key=KEY, year=YEAR)
            games = play(league, fixtures=[f for f in fixtures if f.date == fixtures[0].date])
        self.assertEqual(len(games), SAMPLE_TEAM_COUNT // 2)

    def test_rosters_are_playable_for_many_keys(self) -> None:
        """どの key でも、どの球団の名簿も試合を組める（`ensure_roster_playable`。試合は回さない）。"""
        for index in range(200):
            league = draw_sample_league(f"key-{index}", YEAR)
            for team in league.teams:
                members = [
                    ClubMember(player_id=i, name=p.name, position=p.position, counts_as_foreign=p.is_foreign)
                    for i, p in enumerate(team.players)
                ]
                with self.subTest(key=index, team=team.name):
                    ensure_roster_playable(team.name, members, foreign_rules=league.foreign_rules)

    def test_drawing_checks_every_roster(self) -> None:
        """組み立ての中で、6球団の名簿を `ensure_roster_playable` に通す（試合を組めない名簿を返さない）。"""
        with mock.patch.object(sample, "ensure_roster_playable", wraps=ensure_roster_playable) as check:
            draw_sample_league(KEY, YEAR)
        self.assertEqual(check.call_count, SAMPLE_TEAM_COUNT)


class SampleScheduleTest(unittest.TestCase):
    fixtures: list[Fixture]

    @classmethod
    def setUpClass(cls) -> None:
        cls.fixtures = sample_fixtures(TEAM_IDS, key=KEY, year=YEAR)

    def test_36_games_with_12_per_team(self) -> None:
        self.assertEqual(len(self.fixtures), SAMPLE_TEAM_COUNT * SAMPLE_GAMES_PER_TEAM // 2)
        appearances = Counter(team for f in self.fixtures for team in (f.home_team_id, f.visitor_team_id))
        self.assertEqual(appearances, Counter(dict.fromkeys(TEAM_IDS, SAMPLE_GAMES_PER_TEAM)))

    def test_every_pair_meets(self) -> None:
        """1リーグの総当たり（12試合を5つの相手に2〜3試合ずつ）。"""
        pairs = Counter(frozenset((f.home_team_id, f.visitor_team_id)) for f in self.fixtures)
        self.assertEqual(len(pairs), SAMPLE_TEAM_COUNT * (SAMPLE_TEAM_COUNT - 1) // 2)
        self.assertTrue(all(2 <= count <= 3 for count in pairs.values()))

    def test_opens_on_the_default_opening_day_and_rests_on_mondays(self) -> None:
        dates = sorted(f.date for f in self.fixtures)
        self.assertEqual(dates[0], opening_day(YEAR))
        # 開幕は3月25日以降で最初の金曜（`default_opening_day`）
        self.assertEqual(dates[0].weekday(), 4)
        self.assertGreaterEqual(dates[0], date(YEAR, 3, 25))
        self.assertTrue(all(day.weekday() != MONDAY for day in dates))
        # 1球団1日1試合
        for day in set(dates):
            teams = [t for f in self.fixtures if f.date == day for t in (f.home_team_id, f.visitor_team_id)]
            self.assertEqual(len(teams), len(set(teams)))

    def test_ends_within_the_season_span_for_many_keys(self) -> None:
        """最後の試合は開幕から `SAMPLE_SEASON_DAYS` 日以内（`sample_year` がこの日数を使う）。"""
        for index in range(30):
            for year in (2024, 2025, 2026, 2027):
                fixtures = sample_fixtures(TEAM_IDS, key=f"key-{index}", year=year)
                with self.subTest(key=index, year=year):
                    self.assertLessEqual(
                        max(f.date for f in fixtures), opening_day(year) + timedelta(days=SAMPLE_SEASON_DAYS)
                    )


class SampleYearTest(unittest.TestCase):
    def test_boundary_at_the_end_of_the_season_span(self) -> None:
        last_possible = opening_day(2026) + timedelta(days=SAMPLE_SEASON_DAYS)
        self.assertEqual(sample_year(last_possible), 2025)
        self.assertEqual(sample_year(last_possible + timedelta(days=1)), 2026)
        self.assertEqual(sample_year(date(2026, 1, 1)), 2025)
        self.assertEqual(sample_year(date(2026, 12, 31)), 2026)

    def test_every_game_is_before_today(self) -> None:
        """どの日に作っても、見本の試合はすべて今日より前の日付。"""
        start = date(2026, 1, 1)
        for offset in range(0, 366, 3):
            today = start + timedelta(days=offset)
            year = sample_year(today)
            fixtures = sample_fixtures(TEAM_IDS, key=KEY, year=year)
            with self.subTest(today=today):
                self.assertLess(max(f.date for f in fixtures), today)


class SampleGamesTest(unittest.TestCase):
    league: SampleLeague
    fixtures: list[Fixture]
    games: list[Game]

    @classmethod
    def setUpClass(cls) -> None:
        cls.league = draw_sample_league(KEY, YEAR)
        cls.fixtures = sample_fixtures(TEAM_IDS, key=KEY, year=YEAR)
        cls.games = play(cls.league, fixtures=cls.fixtures)

    def test_plays_every_fixture_in_date_order(self) -> None:
        self.assertEqual(len(self.games), len(self.fixtures))
        self.assertEqual(
            sorted((g.played_on, g.home_team_id, g.away_team_id) for g in self.games),
            sorted((f.date, f.home_team_id, f.visitor_team_id) for f in self.fixtures),
        )
        dates = [g.played_on for g in self.games]
        self.assertEqual(dates, sorted(dates))

    def test_every_game_passes_the_line_check(self) -> None:
        """保存の前の照合（打席から数え直した明細と一致する。`DjangoGameRepository.add_all` が通すもの）。"""
        for game in self.games:
            with self.subTest(game=box(game)):
                ensure_lines_match_plate_appearances(game)
                self.assertTrue(game.plate_appearances)

    def test_games_have_times_and_attendance_within_capacity(self) -> None:
        capacities = {
            team_id: team.stadium.profile.capacity for team_id, team in zip(TEAM_IDS, self.league.teams, strict=True)
        }
        for game in self.games:
            with self.subTest(game=box(game)):
                self.assertIsNotNone(game.times)
                assert game.attendance is not None
                self.assertLessEqual(game.attendance, capacities[game.home_team_id] or 0)
                self.assertGreater(game.attendance, 0)

    def test_some_batters_and_pitchers_qualify(self) -> None:
        """規定打席・規定投球回に届く選手が1人以上いる（打率・防御率の部門を空にしない）。"""
        for key in (KEY, "another-key", "third-key"):
            league = draw_sample_league(key, YEAR)
            games = self.games if key == KEY else play(league, key=key)
            plate_appearances: Counter[int] = Counter()
            outs: Counter[int] = Counter()
            for game in games:
                for line in game.batting:
                    plate_appearances[line.player_id] += line.line.plate_appearances
                for line in game.pitching:
                    outs[line.player_id] += line.line.innings.outs
            with self.subTest(key=key):
                self.assertTrue(
                    any(v >= required_plate_appearances(SAMPLE_GAMES_PER_TEAM) for v in plate_appearances.values())
                )
                self.assertTrue(any(v >= required_outs(SAMPLE_GAMES_PER_TEAM) for v in outs.values()))

    def test_the_same_key_gives_the_same_games_whatever_the_team_ids(self) -> None:
        """試合の乱数は key・年・試合の識別（日付と球団の並びの位置）から作る。保存後の id が違っても同じ試合。"""
        other_ids = (101, 205, 302, 47, 58, 69)
        mapping = dict(zip(TEAM_IDS, other_ids, strict=True))
        fixtures = [
            type(f)(date=f.date, home_team_id=mapping[f.home_team_id], visitor_team_id=mapping[f.visitor_team_id])
            for f in self.fixtures
        ]
        games = play(self.league, team_ids=other_ids, fixtures=fixtures)
        back = {v: k for k, v in mapping.items()}
        self.assertEqual(
            [box(g) for g in self.games],
            [
                (
                    g.played_on,
                    back[g.home_team_id],
                    back[g.away_team_id],
                    g.home_score,
                    g.away_score,
                    len(g.plate_appearances),
                )
                for g in games
            ],
        )

    def test_each_game_has_its_own_random_source(self) -> None:
        """開幕日の試合は、その1試合だけを回しても全体で回したときと同じ（試合ごとに乱数を作る。共有しない）。"""
        first_day = [f for f in self.fixtures if f.date == self.fixtures[0].date]
        in_full = {(g.home_team_id, g.away_team_id): box(g) for g in self.games if g.played_on == first_day[0].date}
        for fixture in first_day:
            alone = play(self.league, fixtures=[fixture])
            with self.subTest(fixture=fixture):
                self.assertEqual(box(alone[0]), in_full[(fixture.home_team_id, fixture.visitor_team_id)])

    def test_a_different_key_gives_different_games(self) -> None:
        games = play(self.league, key="another-key")
        self.assertNotEqual([box(g) for g in self.games], [box(g) for g in games])


if __name__ == "__main__":
    unittest.main()
