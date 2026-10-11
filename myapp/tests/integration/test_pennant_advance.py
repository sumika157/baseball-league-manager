"""ペナントのシーズン進行の検査（日程の保存・日を進める・試合の一括保存）。

世界は球団2つ（1リーグ）にする。NPB 形式の既定の規則でも1球団143試合で組め、1シーズンが
143試合なので速い。1日ずつ進めた結果と1週間まとめて進めた結果が一致すること（試合ごとの乱数と、
疲労を直近の登板から導くこと）が、この段階の核。
"""

import copy
from collections import defaultdict
from dataclasses import replace
from datetime import date, timedelta
from unittest import mock

from django.db import transaction
from django.test import TestCase

from myapp.application.dto import AdvanceReport
from myapp.domain.entities import Game
from myapp.domain.exceptions import (
    InvalidGame,
    InvalidPlateAppearance,
    InvalidSchedule,
    InvalidWorld,
    TeamNotFound,
    WorldNotFound,
)
from myapp.domain.pennant.schedule import AdvanceTarget, Fixture
from myapp.domain.pennant.world import WorldScope
from myapp.infrastructure import orm_models
from myapp.infrastructure.queries import DjangoGameListQuery, DjangoSimulationContextQuery
from myapp.infrastructure.repositories import DjangoFixtureRepository, DjangoGameRepository
from myapp.presentation.views import build_pennant_season_service, build_pennant_world_service, build_service

YEAR = 2026
SEASON_GAMES = 143
BATTER_POSITIONS = ["捕手"] * 2 + ["内野手"] * 5 + ["外野手"] * 4 + ["指名打者"]
PITCHERS = 11


def register_club(service, team, prefix):
    """野手12人と投手11人。名前は世界の中で一意にする（世界どうしを名前で突き合わせる）。"""
    for number, position in enumerate(BATTER_POSITIONS, start=1):
        service.register_player(team.id, f"{prefix}野手{number}", number, position)
    for number in range(1, PITCHERS + 1):
        service.register_player(team.id, f"{prefix}投手{number}", 20 + number, "投手")


class SeasonCase(TestCase):
    """実データのリーグ（球団2つ）を分岐した世界を2つ。同じシードで作るので、同じ進め方なら同じ結果になる。"""

    league_id: int
    home: orm_models.Team
    rival: orm_models.Team
    world_a: int
    world_b: int
    world_other_seed: int

    @classmethod
    def setUpTestData(cls):
        league = orm_models.League.objects.create(name="進行リーグ")
        cls.league_id = league.id
        stadium = orm_models.Stadium.objects.create(name="進行球場", city="東京")
        cls.home = orm_models.Team.objects.create(league=league, name="ホーム球団", home_stadium=stadium)
        cls.rival = orm_models.Team.objects.create(league=league, name="ライバル球団")
        service = build_service()
        register_club(service, cls.home, "ホ")
        register_club(service, cls.rival, "ラ")
        cls.world_a = cls._create_world("世界A", seed=7)
        cls.world_b = cls._create_world("世界B", seed=7)
        cls.world_other_seed = cls._create_world("世界C", seed=8)

    @classmethod
    def _create_world(cls, name, *, seed) -> int:
        created = build_pennant_world_service().create_world(
            name=name,
            owner_id=None,
            source_league_ids=[cls.league_id],
            start_year=YEAR,
            seed=seed,
            managed_source_team_id=cls.home.id,
        )
        return created.world.id

    # --- 読み取りの道具 ---

    @staticmethod
    def fixtures_of(world_id) -> list[Fixture]:
        return DjangoFixtureRepository(WorldScope.pennant(world_id)).find_all()

    @staticmethod
    def games_of(world_id) -> list[Game]:
        return DjangoGameRepository(WorldScope.pennant(world_id)).find_all()

    @staticmethod
    def snapshot(world_id, *, until=None):
        """世界の試合を、世界をまたいで突き合わせられる形にする（id ではなく名前と日付で）。"""
        in_world = {"game__home_team__league__world_id": world_id}
        if until is not None:
            in_world["game__played_on__lte"] = until
        plate_appearances = (
            orm_models.GamePlateAppearance.objects.filter(**in_world)
            .order_by("game__played_on", "game__home_team__name", "sequence")
            .values_list(
                "game__played_on",
                "game__home_team__name",
                "game__away_team__name",
                "game__home_score",
                "game__away_score",
                "sequence",
                "inning",
                "is_bottom",
                "batter__name",
                "pitcher__name",
                "result",
                "fielded_by",
            )
        )
        pitching = (
            orm_models.GamePitchingLine.objects.filter(**in_world)
            .order_by("game__played_on", "game__home_team__name", "appearance_order", "player__name")
            .values_list(
                "game__played_on",
                "game__home_team__name",
                "player__name",
                "appearance_order",
                "innings_pitched",
                "wins",
                "losses",
                "saves",
                "holds",
                "earned_runs",
            )
        )
        return list(plate_appearances), list(pitching)

    @staticmethod
    def advance(world_id, target) -> AdvanceReport:
        return build_pennant_season_service(world_id).advance(target)


class AdvanceTest(SeasonCase):
    def test_a_world_has_no_schedule_until_it_is_advanced(self):
        self.assertEqual(self.fixtures_of(self.world_a), [])
        self.assertEqual(self.games_of(self.world_a), [])

    def test_the_first_advance_creates_the_schedule_of_the_opening_year(self):
        report = self.advance(self.world_a, AdvanceTarget.DAY)

        self.assertTrue(report.schedule_created)
        fixtures = self.fixtures_of(self.world_a)
        played = self.games_of(self.world_a)
        self.assertEqual(len(fixtures) + len(played), SEASON_GAMES)
        self.assertTrue(all(f.date.year == YEAR for f in fixtures))

    def test_a_day_turns_the_fixtures_of_that_day_into_games(self):
        service = build_pennant_season_service(self.world_a)
        service.ensure_schedule()
        before = self.fixtures_of(self.world_a)
        first_day = before[0].date

        report = service.advance(AdvanceTarget.DAY)

        self.assertFalse(report.schedule_created, "日程はもう作ってある")
        self.assertEqual(report.played_dates, (first_day,))
        self.assertEqual(report.games, 1)
        self.assertEqual(report.today, first_day)
        self.assertEqual(report.remaining_fixtures, SEASON_GAMES - 1)
        after = self.fixtures_of(self.world_a)
        self.assertEqual(after, before[1:], "消化した対戦だけが消える")
        (game,) = self.games_of(self.world_a)
        self.assertEqual(
            (game.played_on, game.home_team_id, game.away_team_id),
            (first_day, before[0].home_team_id, before[0].visitor_team_id),
        )
        self.assertEqual(game.season.year, YEAR)

    def test_the_schedule_is_made_only_once(self):
        service = build_pennant_season_service(self.world_a)

        self.assertTrue(service.ensure_schedule())
        self.assertFalse(service.ensure_schedule())
        self.assertEqual(len(self.fixtures_of(self.world_a)), SEASON_GAMES)

    def test_the_same_world_always_gets_the_same_schedule(self):
        """日程は世界のシードから作る。同じシードの別の世界は、同じ日付の並びになる。"""
        for world_id in (self.world_a, self.world_b, self.world_other_seed):
            build_pennant_season_service(world_id).ensure_schedule()

        dates = {w: [f.date for f in self.fixtures_of(w)] for w in (self.world_a, self.world_b, self.world_other_seed)}

        self.assertEqual(dates[self.world_a], dates[self.world_b])
        self.assertEqual(len(dates[self.world_other_seed]), SEASON_GAMES)

    def test_a_game_made_by_advancing_is_a_complete_scorebook(self):
        """打席・明細・イニングスコア・守備成績まで揃った試合ができる（スコアブックの保存と同じ組み立て）。"""
        self.advance(self.world_a, AdvanceTarget.WEEK)

        repository = DjangoGameRepository(WorldScope.pennant(self.world_a))
        for game in self.games_of(self.world_a):
            with self.subTest(day=game.played_on):
                loaded = repository.find_by_id(game.id)
                self.assertTrue(loaded.plate_appearances)
                self.assertTrue(loaded.fielding)
                self.assertGreaterEqual(len(loaded.batting), 18, "両チームの打順9人ぶん")
                self.assertEqual(loaded.line_score.home_total, loaded.home_score)
                self.assertEqual(loaded.line_score.away_total, loaded.away_score)
                wins = sum(p.line.wins for p in loaded.pitching)
                self.assertEqual(wins, 0 if loaded.home_score == loaded.away_score else 1, "引分以外は勝ち投手が1人")

    def test_advancing_a_finished_season_does_nothing(self):
        self.advance(self.world_a, AdvanceTarget.SEASON_END)

        report = self.advance(self.world_a, AdvanceTarget.DAY)

        self.assertEqual((report.games, report.played_dates), (0, ()))
        self.assertTrue(report.season_finished)
        self.assertEqual(len(self.games_of(self.world_a)), SEASON_GAMES, "試合を作り足さない")
        self.assertEqual(self.fixtures_of(self.world_a), [], "日程を作り足さない")

    def test_an_unknown_world_is_not_found(self):
        with self.assertRaises(WorldNotFound):
            self.advance(self.world_a + 1000, AdvanceTarget.DAY)

    def test_next_game_stops_on_the_managed_teams_next_game_day(self):
        service = build_pennant_season_service(self.world_a)

        report = service.advance(AdvanceTarget.NEXT_MANAGED_GAME)

        # 球団2つのリーグでは、毎日の試合が自軍の試合
        self.assertEqual(len(report.played_dates), 1)

    def test_month_end_plays_to_the_end_of_the_month(self):
        report = self.advance(self.world_a, AdvanceTarget.MONTH_END)

        self.assertEqual({(d.year, d.month) for d in report.played_dates}, {(YEAR, 3)})
        self.assertEqual(report.today, report.played_dates[-1])
        self.assertTrue(all(f.date.month != 3 for f in self.fixtures_of(self.world_a)))


class SeasonEndTest(SeasonCase):
    def test_playing_to_the_end_empties_the_fixtures_and_every_team_plays_143(self):
        report = self.advance(self.world_a, AdvanceTarget.SEASON_END)

        self.assertEqual(self.fixtures_of(self.world_a), [])
        self.assertTrue(report.season_finished)
        self.assertEqual(report.games, SEASON_GAMES)
        self.assertEqual(report.remaining_fixtures, 0)
        counts = DjangoGameListQuery(WorldScope.pennant(self.world_a)).count_by_team(year=YEAR)
        self.assertEqual(set(counts.values()), {SEASON_GAMES})
        self.assertEqual(len(counts), 2)

    def test_the_world_today_is_derived_from_the_games(self):
        report = self.advance(self.world_a, AdvanceTarget.WEEK)

        query = DjangoSimulationContextQuery(WorldScope.pennant(self.world_a))

        self.assertEqual(query.last_played_on(), report.today)
        self.assertEqual(query.last_played_on(), max(g.played_on for g in self.games_of(self.world_a)))

    def test_starters_rest_five_days_even_across_separate_advances(self):
        """疲労は保存せず直近の登板から導く。1日ずつの「進める」をまたいでも、先発の間隔が空く。"""
        for _ in range(45):
            self.advance(self.world_a, AdvanceTarget.DAY)

        starts = defaultdict(list)
        for line in orm_models.GamePitchingLine.objects.filter(
            game__home_team__league__world_id=self.world_a, appearance_order=1
        ).select_related("game"):
            starts[line.player_id].append(line.game.played_on)
        self.assertGreater(len(starts), 6)
        for pitcher_id, days in starts.items():
            days.sort()
            gaps = [(later - earlier).days for earlier, later in zip(days, days[1:], strict=False)]
            self.assertTrue(all(gap >= 6 for gap in gaps), (pitcher_id, days))

    def test_no_reliever_pitches_three_days_in_a_row(self):
        self.advance(self.world_a, AdvanceTarget.SEASON_END)

        days_by_pitcher = defaultdict(set)
        for line in orm_models.GamePitchingLine.objects.filter(
            game__home_team__league__world_id=self.world_a
        ).select_related("game"):
            days_by_pitcher[line.player_id].add(line.game.played_on)
        for pitcher_id, days in days_by_pitcher.items():
            for day in days:
                run = {day + timedelta(days=offset) for offset in range(3)}
                self.assertFalse(run <= days, (pitcher_id, day))


class ReproducibilityTest(SeasonCase):
    """1日ずつ進めても1週間まとめて進めても、同じシーズンになる。

    試合ごとの乱数は世界のシード・年・日付と対戦の球団 id から作る。球団の id は世界ごとに違うので、
    **同じ世界**を巻き戻して（セーブポイント）別の進め方で進め直し、結果を突き合わせる。
    """

    def replay(self, world_id, run, *, until=None):
        """`run` で世界を進めた結果を取り、世界を元に戻す。"""
        savepoint = transaction.savepoint()
        try:
            run()
            return self.snapshot(world_id, until=until)
        finally:
            transaction.savepoint_rollback(savepoint)

    def days(self, count):
        return lambda: [self.advance(self.world_a, AdvanceTarget.DAY) for _ in range(count)]

    def test_days_one_by_one_equal_a_week_at_once(self):
        week = self.replay(self.world_a, lambda: self.advance(self.world_a, AdvanceTarget.WEEK))
        played = len({row[0] for row in week[0]})
        self.assertGreater(played, 3, "1週間で数日ぶんの試合ができている")

        one_by_one = self.replay(self.world_a, self.days(played))

        self.assertEqual(week, one_by_one)

    def test_a_month_equals_weeks(self):
        last_day = None

        def month():
            nonlocal last_day
            last_day = self.advance(self.world_a, AdvanceTarget.MONTH_END).today

        month_result = self.replay(self.world_a, month)

        def weeks():
            while self.advance(self.world_a, AdvanceTarget.WEEK).today < last_day:
                pass

        # 週は月末をまたぎうるので、月末までの分を突き合わせる
        self.assertEqual(month_result, self.replay(self.world_a, weeks, until=last_day))

    def test_the_whole_season_does_not_depend_on_how_it_was_advanced(self):
        def weeks():
            for _ in range(100):
                if self.advance(self.world_a, AdvanceTarget.WEEK).season_finished:
                    return

        whole = self.replay(self.world_a, lambda: self.advance(self.world_a, AdvanceTarget.SEASON_END))
        by_weeks = self.replay(self.world_a, weeks)

        self.assertEqual(len({row[0] for row in whole[0]}), 143, "1日1試合なので143日")
        self.assertEqual(whole, by_weeks)

    def test_flushing_in_small_chunks_changes_nothing(self):
        """メモリに溜める試合数の上限で書き出す区切りが変わっても、結果は同じ（日の区切りで書き出す）。"""

        def small_chunks():
            with mock.patch("myapp.application.pennant_season.FLUSH_EVERY_GAMES", 10):
                self.advance(self.world_a, AdvanceTarget.SEASON_END)

        whole = self.replay(self.world_a, lambda: self.advance(self.world_a, AdvanceTarget.SEASON_END))

        self.assertEqual(whole, self.replay(self.world_a, small_chunks))

    def test_a_different_seed_gives_a_different_season(self):
        def with_seed(seed):
            def run():
                orm_models.PennantWorld.objects.filter(id=self.world_a).update(seed=seed)
                self.advance(self.world_a, AdvanceTarget.MONTH_END)

            return run

        same = self.replay(self.world_a, with_seed(7))

        self.assertEqual(same, self.replay(self.world_a, with_seed(7)), "同じシードなら同じ")
        self.assertNotEqual(same, self.replay(self.world_a, with_seed(8)))


class IsolationTest(SeasonCase):
    """ある世界を進めても、別の世界・実データには触れない。"""

    def test_advancing_one_world_leaves_the_others_alone(self):
        build_pennant_season_service(self.world_b).ensure_schedule()
        real_games = orm_models.Game.objects.filter(home_team__league__world__isnull=True).count()

        self.advance(self.world_a, AdvanceTarget.WEEK)

        self.assertEqual(len(self.fixtures_of(self.world_b)), SEASON_GAMES, "別の世界の日程は消化されない")
        self.assertEqual(self.games_of(self.world_b), [])
        self.assertEqual(self.fixtures_of(self.world_other_seed), [], "別の世界の日程を勝手に作らない")
        self.assertEqual(orm_models.Game.objects.filter(home_team__league__world__isnull=True).count(), real_games)
        self.assertEqual(orm_models.PennantFixture.objects.filter(home_team__league__world__isnull=True).count(), 0)

    def test_the_teams_of_a_world_play_only_inside_it(self):
        self.advance(self.world_a, AdvanceTarget.WEEK)

        teams = set(orm_models.Team.objects.filter(league__world_id=self.world_a).values_list("id", flat=True))
        for game in orm_models.Game.objects.filter(home_team__league__world_id=self.world_a):
            self.assertIn(game.home_team_id, teams)
            self.assertIn(game.away_team_id, teams)
        for fixture in self.fixtures_of(self.world_a):
            self.assertIn(fixture.home_team_id, teams)

    def test_a_game_that_was_already_taken_cannot_be_played_twice(self):
        """同じ世界を同時に進めたとき、同じ対戦の試合が2つできない（消化済みを消そうとして止まる）。"""
        service = build_pennant_season_service(self.world_a)
        service.ensure_schedule()
        stale_fixtures = self.fixtures_of(self.world_a)
        service.advance(AdvanceTarget.DAY)
        before = len(self.games_of(self.world_a))
        stale = build_pennant_season_service(self.world_a)

        # もう1つのサービスは、日を進める前の日程と「今日」を見ている（同時に進めようとした状況）
        with (
            mock.patch.object(stale._fixtures, "find_all", return_value=stale_fixtures),
            mock.patch.object(stale._context_query, "last_played_on", return_value=None),
            self.assertRaises(InvalidSchedule),
        ):
            stale.advance(AdvanceTarget.DAY)

        self.assertEqual(len(self.games_of(self.world_a)), before)

    def test_deleting_the_world_removes_its_fixtures(self):
        self.advance(self.world_a, AdvanceTarget.DAY)
        build_pennant_season_service(self.world_b).ensure_schedule()

        build_pennant_world_service().delete_world(self.world_a)

        self.assertEqual(orm_models.PennantFixture.objects.filter(home_team__league__world_id=self.world_a).count(), 0)
        self.assertEqual(len(self.fixtures_of(self.world_b)), SEASON_GAMES)


class FixtureRepositoryTest(SeasonCase):
    def setUp(self):
        self.scope = WorldScope.pennant(self.world_a)
        self.repository = DjangoFixtureRepository(self.scope)
        teams = orm_models.Team.objects.filter(league__world_id=self.world_a).order_by("id")
        self.first, self.second = (t.id for t in teams)
        self.day = timedelta(days=1)

    def _fixture(self, offset, *, home=None, visitor=None) -> Fixture:
        return Fixture(date(YEAR, 4, 1) + offset * self.day, home or self.first, visitor or self.second)

    def test_round_trip_in_date_order(self):
        later, earlier = self._fixture(3), self._fixture(1, home=self.second, visitor=self.first)

        self.repository.add_all([later, earlier])

        self.assertEqual(self.repository.find_all(), [earlier, later])

    def test_remove_deletes_only_the_given_fixtures(self):
        a, b, c = self._fixture(1), self._fixture(2), self._fixture(3)
        self.repository.add_all([a, b, c])

        self.repository.remove([a, c])

        self.assertEqual(self.repository.find_all(), [b])

    def test_removing_a_fixture_twice_is_refused_and_removes_nothing(self):
        a, b = self._fixture(1), self._fixture(2)
        self.repository.add_all([a, b])
        self.repository.remove([a])

        with self.assertRaises(InvalidSchedule):
            self.repository.remove([a, b])

        self.assertEqual(self.repository.find_all(), [b], "b も消えていない")

    def test_a_duplicate_is_refused(self):
        a = self._fixture(1)
        with self.assertRaises(InvalidSchedule):
            self.repository.add_all([a, a])
        self.repository.add_all([a])
        with self.assertRaises(InvalidSchedule):
            self.repository.add_all([a])

        self.assertEqual(self.repository.find_all(), [a])

    def test_a_team_outside_the_scope_cannot_be_scheduled(self):
        other_team = orm_models.Team.objects.filter(league__world_id=self.world_b).first()

        with self.assertRaises(TeamNotFound):
            self.repository.add_all([self._fixture(1, visitor=other_team.id)])
        self.assertEqual(self.repository.find_all(), [])

    def test_each_world_sees_only_its_own_fixtures(self):
        self.repository.add_all([self._fixture(1)])

        self.assertEqual(DjangoFixtureRepository(WorldScope.pennant(self.world_b)).find_all(), [])

    def test_the_real_data_has_no_schedule(self):
        real = DjangoFixtureRepository(WorldScope.real())

        self.assertEqual(real.find_all(), [])
        with self.assertRaises(InvalidWorld):
            real.add_all([self._fixture(1)])


class AddAllTest(SeasonCase):
    """`GameRepository.add_all` は `save()` と同じ検査を通し、同じ行を作る。"""

    def setUp(self):
        self.advance(self.world_a, AdvanceTarget.WEEK)
        self.repository = DjangoGameRepository(WorldScope.pennant(self.world_a))
        self.source = self.repository.find_by_id(self.games_of(self.world_a)[0].id)

    def _new_game(self, **changes) -> Game:
        """保存済みの試合の写し（まだ保存していない新しい試合）。"""
        game = copy.deepcopy(self.source)
        game.id = None
        for name, value in changes.items():
            setattr(game, name, value)
        return game

    def _count(self) -> int:
        return orm_models.Game.objects.filter(home_team__league__world_id=self.world_a).count()

    @staticmethod
    def _rows_of(game_id) -> dict[str, list]:
        """1試合ぶんの行を、id を除いて種類ごとに並べる（保存の仕方によらず同じになるはずの中身）。"""
        plain = {"game_id": game_id}
        by = "plate_appearance__game_id"
        return {
            "game": list(
                orm_models.Game.objects.filter(id=game_id).values("year", "played_on", "home_score", "away_score")
            ),
            "batting": list(
                orm_models.GameBattingLine.objects.filter(**plain)
                .order_by("player_id")
                .values(
                    *[f.name for f in orm_models.GameBattingLine._meta.concrete_fields if f.name not in ("id", "game")]
                )
            ),
            "pitching": list(
                orm_models.GamePitchingLine.objects.filter(**plain)
                .order_by("player_id")
                .values(
                    *[
                        f.name
                        for f in orm_models.GamePitchingLine._meta.concrete_fields
                        if f.name not in ("id", "game")
                    ]
                )
            ),
            "fielding": list(
                orm_models.GameFieldingLine.objects.filter(**plain)
                .order_by("player_id")
                .values(
                    *[
                        f.name
                        for f in orm_models.GameFieldingLine._meta.concrete_fields
                        if f.name not in ("id", "game")
                    ]
                )
            ),
            "innings": list(
                orm_models.GameInningScore.objects.filter(**plain)
                .order_by("inning", "is_home")
                .values("inning", "is_home", "runs")
            ),
            "plate_appearances": list(
                orm_models.GamePlateAppearance.objects.filter(**plain)
                .order_by("sequence")
                .values(
                    "sequence",
                    "inning",
                    "is_bottom",
                    "batter_id",
                    "pitcher_id",
                    "batting_order",
                    "slot_sequence",
                    "result",
                    "fielded_by",
                )
            ),
            "advances": list(
                orm_models.GameRunnerAdvance.objects.filter(**{by: game_id})
                .order_by("plate_appearance__sequence", "id")
                .values("plate_appearance__sequence", "runner_id", "from_base", "to_base", "reason", "error_index")
            ),
            "errors": list(
                orm_models.GameFieldingError.objects.filter(**{by: game_id})
                .order_by("plate_appearance__sequence", "id")
                .values("plate_appearance__sequence", "player_id", "position", "kind")
            ),
            "substitutions": list(
                orm_models.GameRunnerSubstitution.objects.filter(**{by: game_id})
                .order_by("plate_appearance__sequence", "id")
                .values("plate_appearance__sequence", "base", "leaving_runner_id", "entering_runner_id")
            ),
        }

    def test_stores_new_games_and_returns_their_ids(self):
        before = self._count()
        games = [self._new_game(), self._new_game()]

        self.repository.add_all(games)

        self.assertEqual(self._count(), before + 2)
        self.assertEqual(len({game.id for game in games}), 2)
        self.assertTrue(all(game.id is not None for game in games))

    def test_the_stored_rows_equal_the_ones_save_makes(self):
        """一括の書き込みと1試合ずつの `save()` で、読み戻した結果が同じ（写し方の食い違いを防ぐ）。"""
        bulk, single = self._new_game(), self._new_game()

        self.repository.add_all([bulk])
        self.repository.save(single)

        self.assertEqual(self._rows_of(bulk.id), self._rows_of(single.id))
        self.assertEqual(self._rows_of(bulk.id), self._rows_of(self.source.id), "元の試合と同じ中身")

    def test_the_ids_are_set_on_the_aggregate(self):
        game = self._new_game()

        self.repository.add_all([game])

        loaded = self.repository.find_by_id(game.id)
        self.assertEqual(
            [(p.sequence, p.result) for p in loaded.plate_appearances_in_order()],
            [(p.sequence, p.result) for p in game.plate_appearances_in_order()],
        )
        for entry in game.plate_appearances_in_order():
            self.assertEqual(orm_models.GamePlateAppearance.objects.get(id=entry.id).sequence, entry.sequence)

    def test_a_line_that_disagrees_with_the_plate_appearances_is_refused(self):
        bad = self._new_game()
        bad.batting[0].line = replace(bad.batting[0].line, singles=bad.batting[0].line.singles + 1)
        before = self._count()

        # 先頭は問題の無い試合。照合は書き込みの前に全試合ぶん済ませるので、先頭の試合も書かれない
        with self.assertRaises(InvalidPlateAppearance):
            self.repository.add_all([self._new_game(), bad])

        self.assertEqual(self._count(), before)

    def test_a_game_with_an_id_is_refused(self):
        with self.assertRaises(InvalidGame):
            self.repository.add_all([self.source])

    def test_a_team_outside_the_scope_is_refused(self):
        other = DjangoGameRepository(WorldScope.pennant(self.world_b))
        before = self._count()

        with self.assertRaises(TeamNotFound):
            other.add_all([self._new_game()])

        self.assertEqual(self._count(), before)
        self.assertEqual(
            orm_models.Game.objects.filter(home_team__league__world_id=self.world_b).count(), 0, "別の世界にも書かない"
        )

    def test_adding_nothing_is_fine(self):
        before = self._count()

        self.repository.add_all([])

        self.assertEqual(self._count(), before)
