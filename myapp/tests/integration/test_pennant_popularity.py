"""ペナントの球団の人気と観客数（#208 E1）。

決定（2026-11-14。#208 のコメント）と計画から:
- 人気は球団 × 年の値として保存する。書くのは世界の作成（開幕年。全球団50）とシーズンを締める処理（翌年）だけ
- 締めた年の人気は書き換えない（`AlreadyClosed`）。実データの範囲には書けない
- 進める処理は、人気と本拠地の収容人数（分からなければペナントの標準の収容人数）から観客数を作る。
  全球団50の世界の観客数は、人気を足す前の式と同じ値。得点と打席は人気で変わらない
- この機能より前の世界（行が無い）は初期値で補って進め、締めるときに締める年の補った値も書く
- GM ホームのカード・球団史の列・オフの結果の節に出す（書き込みの導線は無い）

世界は2リーグ×3球団（公式戦を1週間だけ進め、残りの対戦を消して「公式戦を終えた」年にする。ポストシーズンは「なし」）。
"""

from __future__ import annotations

from django.db import transaction
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from myapp.application.pennant_popularity import popularity_basis
from myapp.application.pennant_simulation import game_key
from myapp.domain.exceptions import AlreadyClosed, InvalidPopularity, InvalidWorld
from myapp.domain.pennant.offseason import OffseasonClub
from myapp.domain.pennant.popularity import PENNANT_DEFAULT_CAPACITY, ClubPopularity, Popularity
from myapp.domain.pennant.postseason import FinalStanding, LeagueFinish
from myapp.domain.pennant.schedule import AdvanceTarget
from myapp.domain.pennant.world import WorldScope
from myapp.domain.simulation.game_day import simulated_attendance
from myapp.domain.simulation.randomness import game_seed, make_random
from myapp.domain.value_objects import ForeignPlayerRules, GameStage, TeamRecord
from myapp.infrastructure import orm_models
from myapp.infrastructure.repositories import (
    DjangoFixtureRepository,
    DjangoPopularityRepository,
    DjangoWorldRepository,
)
from myapp.presentation.views import (
    build_offseason_view_service,
    build_pennant_home_service,
    build_pennant_offseason_service,
    build_pennant_season_service,
    build_pennant_world_service,
    build_pennant_world_view,
)

from .world_case import YEAR, _register_club

SEED = 11
CAPACITY = 30000


class PopularityCase(TestCase):
    """2リーグ×3球団の世界。`teams` は人気リーグの3球団（0 は収容人数のある球場、1 は収容人数の無い球場、
    2 は本拠地なし。0 が受け持つ球団）、`all_teams` は世界の全球団。
    """

    world_id: int
    teams: list[int]
    all_teams: list[int]
    other_league_id: int
    league_id: int
    managed_source_id: int

    @classmethod
    def setUpTestData(cls):
        league = orm_models.League.objects.create(name="人気リーグ")
        other = orm_models.League.objects.create(name="相手リーグ")
        cls.league_id = league.id
        cls.other_league_id = other.id
        stadiums = [
            orm_models.Stadium.objects.create(name="人気球場", city="東京", capacity=CAPACITY),
            orm_models.Stadium.objects.create(name="収容人数の無い球場", city="大阪", capacity=None),
            None,
        ]
        for index, stadium in enumerate(stadiums):
            team = orm_models.Team.objects.create(league=league, name=f"人気{index}球団", home_stadium=stadium)
            _register_club(team, f"人{index}")
            if index == 0:
                cls.managed_source_id = team.id
        # 日程は2リーグ×3球団で組む（1リーグ×3球団では年間の試合数を配れない）
        other_stadium = orm_models.Stadium.objects.create(name="相手球場", city="福岡", capacity=CAPACITY)
        for index in range(3):
            team = orm_models.Team.objects.create(league=other, name=f"相手{index}球団", home_stadium=other_stadium)
            _register_club(team, f"相{index}")
        cls.world_id = cls.create_world()
        cls.teams = list(
            orm_models.Team.objects.filter(league__world_id=cls.world_id, name__startswith="人気")
            .order_by("id")
            .values_list("id", flat=True)
        )
        cls.all_teams = list(
            orm_models.Team.objects.filter(league__world_id=cls.world_id).order_by("id").values_list("id", flat=True)
        )

    @classmethod
    def create_world(cls) -> int:
        return (
            build_pennant_world_service()
            .create_world(
                name="人気の世界",
                owner_id=None,
                source_league_ids=[cls.league_id, cls.other_league_id],
                start_year=YEAR,
                seed=SEED,
                managed_source_team_id=cls.managed_source_id,
            )
            .world.id
        )

    # --- 道具 ---

    def rows(self, world_id=None) -> list[tuple[int, int, int]]:
        world_id = self.world_id if world_id is None else world_id
        return list(
            orm_models.PennantClubPopularity.objects.filter(team__league__world_id=world_id)
            .order_by("year", "team_id")
            .values_list("year", "team_id", "value")
        )

    def finish_regular_season(self, world_id=None) -> None:
        """公式戦を1週間だけ進め、残りの対戦を消して「公式戦を終えた」年にする。"""
        world_id = self.world_id if world_id is None else world_id
        build_pennant_season_service(world_id).advance(AdvanceTarget.WEEK)
        fixtures = DjangoFixtureRepository(WorldScope.pennant(world_id))
        fixtures.remove(fixtures.find_all())

    def close(self, world_id=None) -> None:
        build_pennant_offseason_service(self.world_id if world_id is None else world_id).close_season()

    def games(self, world_id=None):
        world_id = self.world_id if world_id is None else world_id
        return list(
            orm_models.Game.objects.filter(home_team__league__world_id=world_id)
            .order_by("played_on", "home_team_id")
            .values_list("played_on", "home_team_id", "away_team_id", "home_score", "away_score", "attendance")
        )

    def plate_appearances(self):
        return list(
            orm_models.GamePlateAppearance.objects.filter(game__home_team__league__world_id=self.world_id)
            .order_by("game__played_on", "game__home_team_id", "sequence")
            .values_list("game__played_on", "sequence", "batter_id", "pitcher_id", "result")
        )

    def set_popularity(self, team_id: int, value: int, year: int = YEAR) -> None:
        orm_models.PennantClubPopularity.objects.filter(team_id=team_id, year=year).update(value=value)

    def context(self):
        return build_pennant_world_view().get_context(self.world_id)


class WorldCreationTest(PopularityCase):
    def test_creating_a_world_writes_fifty_for_every_club_in_the_opening_year(self):
        """2026-11-14 ユーザー決定: 初期値は全球団50（収容人数・勝率は使わない）。"""
        self.assertEqual(self.rows(), [(YEAR, team_id, 50) for team_id in self.all_teams])

    def test_deleting_the_world_deletes_its_popularity(self):
        other = self.create_world()
        build_pennant_world_service().delete_world(other)

        self.assertEqual(self.rows(other), [])
        self.assertEqual(len(self.rows()), len(self.all_teams), "他の世界の人気は残る")


class AttendanceTest(PopularityCase):
    def test_a_world_where_every_club_is_at_fifty_keeps_the_attendance_of_the_old_formula(self):
        """全球団50なら、観客数は人気を足す前の式（収容人数 × 入場率）と同じ値（乱数源も同じ）。"""
        self.finish_regular_season()
        world = DjangoWorldRepository().find_by_id(self.world_id)

        games = [game for game in self.games() if game[1] == self.teams[0]]
        self.assertTrue(games, "収容人数のある球場のホーム試合がある")
        for played_on, home, away, _, _, attendance in games:
            key = game_key(GameStage.REGULAR, played_on, home, away)
            rng = make_random(game_seed(world.seed, played_on.year, f"{key}:game-day"))
            # 試合の時刻が同じ乱数源から先に引く（`simulated_times`）。正規分布1回ぶん（2回）を読み飛ばす
            rng.random()
            rng.random()
            self.assertEqual(attendance, simulated_attendance(CAPACITY, played_on, rng))

    def test_clubs_without_a_known_capacity_use_the_standard_capacity_in_a_world(self):
        """収容人数の無い球場・本拠地の無い球団も、ペナントの世界では標準の収容人数で観客数を作る。"""
        season = build_pennant_season_service(self.world_id)
        # どちらの球団もホーム試合をするまで進める（1週間ではホーム試合の無い球団がある）
        for _ in range(6):
            if all(any(game[1] == team_id for game in self.games()) for team_id in self.teams[1:]):
                break
            season.advance(AdvanceTarget.WEEK)

        for team_id in self.teams[1:]:
            attendances = [game[5] for game in self.games() if game[1] == team_id]
            with self.subTest(team_id=team_id):
                self.assertTrue(attendances)
                self.assertTrue(all(value is not None for value in attendances), attendances)
                self.assertTrue(all(0 < value <= PENNANT_DEFAULT_CAPACITY for value in attendances), attendances)

    def test_popularity_raises_attendance_without_changing_the_games(self):
        """人気の高い球団のホーム試合ほど観客が入る。得点と打席は人気で変わらない（別の乱数源）。"""
        savepoint = transaction.savepoint()
        self.finish_regular_season()
        plain_games, plain_plays = self.games(), self.plate_appearances()
        transaction.savepoint_rollback(savepoint)

        for team_id in self.all_teams:
            self.set_popularity(team_id, 90)
        self.finish_regular_season()
        popular_games, popular_plays = self.games(), self.plate_appearances()

        self.assertEqual([game[:5] for game in popular_games], [game[:5] for game in plain_games])
        self.assertEqual(popular_plays, plain_plays)
        self.assertGreater(sum(game[5] for game in popular_games), sum(game[5] for game in plain_games))
        for plain, popular in zip(plain_games, popular_games, strict=True):
            self.assertGreaterEqual(popular[5], plain[5])


class CloseTest(PopularityCase):
    def test_closing_writes_next_years_popularity_for_every_club(self):
        self.finish_regular_season()
        self.close()

        rows = self.rows()
        self.assertEqual([row for row in rows if row[0] == YEAR], [(YEAR, team_id, 50) for team_id in self.all_teams])
        following = [row for row in rows if row[0] == YEAR + 1]
        self.assertEqual([row[1] for row in following], self.all_teams)
        self.assertTrue(all(1 <= row[2] <= 100 for row in following), following)

    def test_a_closed_year_cannot_be_written_again(self):
        """締めた年の人気は書き換えない（年ごとの状態。締めた年の試合と同じく `AlreadyClosed`）。"""
        self.finish_regular_season()
        self.close()
        orm_models.PennantClubPopularity.objects.filter(team_id=self.teams[0], year=YEAR).delete()

        with self.assertRaises(AlreadyClosed):
            DjangoPopularityRepository(WorldScope.pennant(self.world_id)).add_all(
                [ClubPopularity(team_id=self.teams[0], year=YEAR, popularity=Popularity(60))]
            )

    def test_a_world_from_before_this_feature_is_filled_with_the_initial_value(self):
        """行の無い世界（この機能より前）も進めて締められる。締めるときに締める年の補った値（50）も書く。"""
        orm_models.PennantClubPopularity.objects.filter(team__league__world_id=self.world_id).delete()
        self.finish_regular_season()
        self.assertTrue(all(game[5] is not None for game in self.games()))

        self.close()

        rows = self.rows()
        self.assertEqual([row for row in rows if row[0] == YEAR], [(YEAR, team_id, 50) for team_id in self.all_teams])
        self.assertEqual([row[1] for row in rows if row[0] == YEAR + 1], self.all_teams)


class RepositoryTest(PopularityCase):
    def repository(self, scope=None):
        return DjangoPopularityRepository(scope or WorldScope.pennant(self.world_id))

    def test_the_real_data_cannot_hold_popularity(self):
        real_team = orm_models.Team.objects.filter(league_id=self.league_id).values_list("id", flat=True).first()
        with self.assertRaises(InvalidWorld):
            self.repository(WorldScope.real()).add_all(
                [ClubPopularity(team_id=real_team, year=YEAR, popularity=Popularity(50))]
            )

    def test_clubs_of_other_worlds_and_duplicates_are_rejected(self):
        real_team = orm_models.Team.objects.filter(league_id=self.league_id).values_list("id", flat=True).first()
        with self.assertRaises(InvalidPopularity):
            self.repository().add_all([ClubPopularity(team_id=real_team, year=YEAR + 1, popularity=Popularity(50))])
        with self.assertRaises(InvalidPopularity):
            self.repository().add_all([ClubPopularity(team_id=self.teams[0], year=YEAR, popularity=Popularity(50))])
        twice = ClubPopularity(team_id=self.teams[0], year=YEAR + 1, popularity=Popularity(50))
        with self.assertRaises(InvalidPopularity):
            self.repository().add_all([twice, twice])
        self.assertEqual(len(self.rows()), len(self.all_teams), "どれかが書けなければ何も書かない")

    def test_reads_are_limited_to_the_world(self):
        other = self.create_world()
        repository = self.repository()
        self.assertEqual([row.team_id for row in repository.find_by_year(YEAR)], self.all_teams)
        other_team = orm_models.Team.objects.filter(league__world_id=other).values_list("id", flat=True).first()
        self.assertEqual(repository.find_by_team(other_team), [])


class ScreenTest(PopularityCase):
    def test_the_home_shows_the_popularity_card(self):
        self.finish_regular_season()

        home = build_pennant_home_service(self.world_id).get_home(self.context(), include_advance=False)
        card = home.popularity
        self.assertIsNotNone(card)
        assert card is not None
        self.assertEqual((card.year, card.popularity, card.previous), (YEAR, 50, None))
        self.assertEqual((card.league_rank, card.league_team_count), (1, 3))
        own_games = [game for game in self.games() if game[1] == self.teams[0]]
        self.assertEqual(card.home_games, len(own_games))
        self.assertEqual(card.attendance_total, sum(game[5] for game in own_games))

        content = self.client.get(reverse("pennant_world", args=[self.world_id])).content.decode()
        self.assertIn("人気と観客動員", content)

    def test_the_offseason_and_the_club_history_show_popularity_after_closing(self):
        self.finish_regular_season()
        self.close()
        # オフの結果の節は選んだリーグ（受け持つ球団のリーグ）の球団だけ
        following = {
            team_id: value for year, team_id, value in self.rows() if year == YEAR + 1 and team_id in self.teams
        }

        summary = build_offseason_view_service(self.world_id).get_summary(self.context(), YEAR)
        assert summary is not None
        self.assertEqual(
            {row.team_id: (row.before, row.after) for row in summary.popularity},
            {team_id: (50, value) for team_id, value in following.items()},
        )
        self.assertEqual([row.after for row in summary.popularity], sorted(following.values(), reverse=True))
        content = self.client.get(reverse("pennant_offseason", args=[self.world_id, YEAR])).content.decode()
        self.assertIn(f"人気（{YEAR}年 → {YEAR + 1}年）", content)

        history = self.client.get(reverse("pennant_team_history", args=[self.world_id, self.teams[0]]))
        self.assertEqual(history.status_code, 200)
        (season,) = history.context["history"].seasons
        own_total = sum(game[5] for game in self.games() if game[1] == self.teams[0])
        self.assertEqual((season.popularity, season.attendance), (50, own_total))
        self.assertIn("観客動員", history.content.decode())

    def test_years_without_saved_popularity_show_a_dash_in_the_club_history(self):
        """人気を足す前の年（行が無い）は、球団史に作っていない値を出さない。"""
        orm_models.PennantClubPopularity.objects.filter(team__league__world_id=self.world_id).delete()
        self.finish_regular_season()

        history = self.client.get(reverse("pennant_team_history", args=[self.world_id, self.teams[0]]))
        (season,) = history.context["history"].seasons
        self.assertIsNone(season.popularity)


class PopularityBasisTest(SimpleTestCase):
    """締める処理の人気の材料（`pennant_popularity.popularity_basis`）。勝率とリーグ優勝は最終順位の全順序の先頭、
    日本一は勝ち上がりの優勝球団（`following` に渡す）、スター選手は在籍する選手の実績から。"""

    def basis(self, recorded):
        finish = LeagueFinish(
            league_id=1,
            standings=(
                FinalStanding(team_id=1, record=TeamRecord(wins=80, losses=60)),
                FinalStanding(team_id=2, record=TeamRecord(wins=60, losses=80)),
            ),
        )
        club = OffseasonClub(
            team_id=2,
            foreign_rules=ForeignPlayerRules.UNLIMITED,
            registered_player_limit=None,
            players=(),
        )
        return popularity_basis(year=YEAR, team_ids=[2, 1, 3], recorded=recorded, finishes=[finish], clubs=[club])

    def test_the_league_leader_is_the_champion_and_clubs_without_a_finish_have_no_factors(self):
        basis = self.basis({1: Popularity(60)})

        self.assertTrue(basis.factors[1].league_champion)
        self.assertFalse(basis.factors[2].league_champion)
        self.assertAlmostEqual(basis.factors[1].winning_percentage, 80 / 140)
        self.assertNotIn(3, basis.factors)

    def test_clubs_without_rows_are_filled_and_written_for_the_closing_year(self):
        basis = self.basis({1: Popularity(60)})

        self.assertEqual(basis.current, {1: Popularity(60), 2: Popularity(50), 3: Popularity(50)})
        self.assertEqual(
            basis.unrecorded(),
            [ClubPopularity(2, YEAR, Popularity(50)), ClubPopularity(3, YEAR, Popularity(50))],
        )

    def test_the_japan_champion_is_added_when_the_next_year_is_derived(self):
        basis = self.basis({1: Popularity(50), 2: Popularity(50), 3: Popularity(50)})

        with_title = {row.team_id: row.popularity for row in basis.following(japan_champion=2, world_seed=SEED)}
        without = {row.team_id: row.popularity for row in basis.following(japan_champion=None, world_seed=SEED)}

        self.assertGreater(with_title[2], without[2])
        self.assertEqual({row.year for row in basis.following(japan_champion=None, world_seed=SEED)}, {YEAR + 1})
