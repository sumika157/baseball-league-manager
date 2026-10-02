"""世界の作成時に推定して保存する初期能力（`PennantPlayerRatings`）。

推定の式そのものは `tests/domain/test_simulation_estimate.py`、束ね方は `test_initial_ratings.py`。
ここでは、保存と読み込みの往復・世界の範囲・一括の書き込み・世界の削除・世界の作成との結びつきを見る。
"""

from dataclasses import fields
from statistics import pstdev

from django.db import IntegrityError, connection, transaction
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext

from myapp.application.pennant_world import PennantWorldService, WorldRepositories
from myapp.domain.exceptions import InvalidRatings, InvalidWorld, PlayerNotFound
from myapp.domain.pennant.ratings import PlayerRatings
from myapp.domain.pennant.world import WorldScope
from myapp.domain.simulation.ratings import BatterRatings, GrowthType, PitcherRatings
from myapp.domain.simulation.samples import BATTER_SD
from myapp.domain.simulation.spread import MIN_POPULATION
from myapp.domain.value_objects import BattingLine, InningsPitched, PitchingLine
from myapp.infrastructure import orm_models
from myapp.infrastructure.queries import DjangoFieldingTotalsQuery
from myapp.infrastructure.repositories import (
    DjangoLeagueRepository,
    DjangoRatingsRepository,
    DjangoTeamRepository,
    DjangoWorldRepository,
)
from myapp.management.estimate_check import estimate_from_source
from myapp.presentation.views import build_pennant_world_service

from ..helpers import give_batting, give_pitching
from .base import BaseCase
from .world_case import YEAR, WorldCase

START = 2030
REAL = WorldScope.real()


class ColumnsMatchTheDomainTest(SimpleTestCase):
    """列は domain の項目が出典。項目を足したのに列を足し忘れると、その項目だけ保存されない。"""

    def test_every_rating_has_a_column_with_the_domain_label(self):
        columns = {field.name: field for field in orm_models.PennantPlayerRatings._meta.get_fields()}

        for cls in (BatterRatings, PitcherRatings):
            for name, label in cls.LABELS.items():
                with self.subTest(item=name):
                    self.assertIn(name, columns)
                    self.assertEqual(columns[name].verbose_name, label)

    def test_every_dataclass_field_is_a_label_or_the_growth_type(self):
        """能力の項目は LABELS に挙がっているものだけ（挙げ忘れた項目は、保存も検査もされない）。"""
        for cls in (BatterRatings, PitcherRatings):
            with self.subTest(cls=cls.__name__):
                self.assertEqual({f.name for f in fields(cls)}, set(cls.LABELS) | {"growth"})

    def test_growth_choices_come_from_the_domain(self):
        self.assertEqual(
            [value for value, _ in orm_models.PennantPlayerRatings.GROWTH_CHOICES],
            [growth.value for growth in GrowthType],
        )


class RatingsRepositoryTest(WorldCase):
    """保存と読み込みの往復、世界の範囲。"""

    def setUp(self):
        super().setUp()
        self.repository = DjangoRatingsRepository(self.scope)
        players = self.pennant_players(self.pennant_team)  # 背番号 1〜9 が打者、18 が投手
        self.batter_id, self.pitcher_id = players[0], players[9]

    def test_the_world_creation_already_saved_the_start_year_ratings(self):
        stored = self.repository.find_by_year(YEAR)

        in_world = orm_models.Player.objects.filter(stints__team__league__world=self.world_id).distinct().count()
        self.assertEqual(len(stored), in_world)

    def test_batters_and_pitchers_round_trip(self):
        batter = PlayerRatings(self.batter_id, 2031, BatterRatings(61, 52, 47, 38, 70, GrowthType.LATE))
        pitcher = PlayerRatings(self.pitcher_id, 2031, PitcherRatings(66, 58, 44, 72, GrowthType.EARLY))

        self.repository.add_all([batter, pitcher])
        loaded = {item.player_id: item for item in self.repository.find_by_year(2031)}

        self.assertEqual(loaded, {self.batter_id: batter, self.pitcher_id: pitcher})

    def test_the_ratings_of_one_player_are_listed_by_year(self):
        self.repository.add_all([PlayerRatings(self.batter_id, 2032, BatterRatings(40, 40, 40, 40, 40))])
        self.repository.add_all([PlayerRatings(self.batter_id, 2031, BatterRatings(45, 45, 45, 45, 45))])

        years = [item.year for item in self.repository.find_by_player(self.batter_id)]

        self.assertEqual(years, [YEAR, 2031, 2032], "年の順に並ぶ。翌年の能力は行を足して残す")

    def test_a_player_outside_the_world_cannot_be_given_ratings(self):
        real_player = self.real_batters[0]

        with self.assertRaises(PlayerNotFound):
            self.repository.add_all([PlayerRatings(real_player, 2031, BatterRatings())])

        self.assertFalse(orm_models.PennantPlayerRatings.objects.filter(player_id=real_player).exists())

    def test_nothing_is_written_when_one_of_the_players_is_outside_the_world(self):
        with self.assertRaises(PlayerNotFound):
            self.repository.add_all(
                [
                    PlayerRatings(self.batter_id, 2031, BatterRatings()),
                    PlayerRatings(self.real_batters[0], 2031, BatterRatings()),
                ]
            )

        self.assertEqual(self.repository.find_by_year(2031), [])

    def test_the_real_scope_cannot_hold_ratings(self):
        with self.assertRaises(InvalidWorld):
            DjangoRatingsRepository(REAL).add_all([PlayerRatings(self.real_batters[0], 2031, BatterRatings())])

    def test_another_world_cannot_see_the_ratings(self):
        other = build_pennant_world_service().create_world(
            name="別", owner_id=None, source_league_ids=[self.league.id], start_year=START, seed=9
        )
        other_repository = DjangoRatingsRepository(WorldScope.pennant(other.world.id))

        self.assertEqual(other_repository.find_by_player(self.batter_id), [])
        self.assertTrue(all(item.player_id != self.batter_id for item in other_repository.find_by_year(YEAR)))
        self.assertEqual(DjangoRatingsRepository(REAL).find_by_year(YEAR), [])

    def test_the_same_player_and_year_cannot_be_saved_twice(self):
        self.repository.add_all([PlayerRatings(self.batter_id, 2031, BatterRatings())])

        with self.assertRaises(InvalidRatings):
            self.repository.add_all([PlayerRatings(self.batter_id, 2031, BatterRatings(60, 60, 60, 60, 60))])

        self.assertEqual(self.repository.find_by_player(self.batter_id)[-1].ratings, BatterRatings())

    def test_the_same_player_and_year_cannot_appear_twice_in_one_call(self):
        with self.assertRaises(InvalidRatings):
            self.repository.add_all(
                [
                    PlayerRatings(self.pitcher_id, 2031, PitcherRatings()),
                    PlayerRatings(self.batter_id, 2031, BatterRatings()),
                    PlayerRatings(self.batter_id, 2031, BatterRatings(60, 60, 60, 60, 60)),
                ]
            )

        self.assertEqual(self.repository.find_by_year(2031), [], "何も書かない")

    def test_another_year_of_the_same_player_can_be_added(self):
        self.repository.add_all([PlayerRatings(self.batter_id, 2031, BatterRatings())])
        self.repository.add_all([PlayerRatings(self.batter_id, 2032, BatterRatings())])

        self.assertEqual([item.year for item in self.repository.find_by_player(self.batter_id)], [YEAR, 2031, 2032])

    def test_the_database_still_rejects_a_duplicate_that_bypasses_the_repository(self):
        """リポジトリの検査をすり抜けた直書きでも、一意制約が最後に止める。"""
        row = orm_models.PennantPlayerRatings(
            player_id=self.batter_id, year=YEAR, growth="普通", contact=50, power=50, eye=50, speed=50, fielding=50
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            row.save(force_insert=True)

    def test_a_pitcher_cannot_be_given_batter_ratings(self):
        with self.assertRaises(InvalidRatings):
            self.repository.add_all([PlayerRatings(self.pitcher_id, 2031, BatterRatings())])

        self.assertEqual(self.repository.find_by_year(2031), [])

    def test_a_batter_cannot_be_given_pitcher_ratings(self):
        with self.assertRaises(InvalidRatings):
            self.repository.add_all(
                [
                    PlayerRatings(self.batter_id, 2031, BatterRatings()),
                    PlayerRatings(self.batter_id, 2032, PitcherRatings()),
                ]
            )

        self.assertEqual(self.repository.find_by_year(2031), [], "同じ呼び出しの正しい行も書かない")

    def test_a_row_cannot_mix_batter_and_pitcher_items(self):
        row = orm_models.PennantPlayerRatings(
            player_id=self.batter_id, year=2031, growth="普通", contact=50, power=50, eye=50, speed=50, fielding=50
        )
        row.stuff = 50

        with self.assertRaises(IntegrityError), transaction.atomic():
            row.save()

    def test_a_row_must_have_one_of_the_two_sets(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            orm_models.PennantPlayerRatings.objects.create(player_id=self.batter_id, year=2031, growth="普通")

    def test_saving_is_one_bulk_write(self):
        """約1,600人ぶんでも、選手ごとに書かない（書き込みのクエリ数が人数に比例しない）。"""
        batters = self.pennant_players(self.pennant_team)[:9] + self.pennant_players(self.pennant_rival)[:9]
        ratings = [
            PlayerRatings(player_id, 2040 + year, BatterRatings()) for year in range(3) for player_id in batters
        ]

        with CaptureQueriesContext(connection) as queries:
            self.repository.add_all(ratings)

        inserts = [q for q in queries if q["sql"].lstrip().upper().startswith("INSERT")]
        self.assertEqual(len(inserts), 1, f"{len(ratings)}人ぶんが1回の INSERT にまとまる")
        self.assertLessEqual(len(queries), 5)


class WorldDeletionRemovesRatingsTest(WorldCase):
    def test_deleting_the_world_removes_its_ratings(self):
        self.assertGreater(len(DjangoRatingsRepository(self.scope).find_by_year(YEAR)), 0)

        build_pennant_world_service().delete_world(self.world_id)

        self.assertEqual(orm_models.PennantPlayerRatings.objects.count(), 0)

    def test_deleting_one_world_keeps_the_ratings_of_another(self):
        other = build_pennant_world_service().create_world(
            name="別", owner_id=None, source_league_ids=[self.league.id], start_year=START, seed=9
        )
        kept = orm_models.PennantPlayerRatings.objects.filter(player__stints__team__league__world=other.world.id)
        before = kept.count()

        build_pennant_world_service().delete_world(self.world_id)

        self.assertGreater(before, 0)
        self.assertEqual(kept.count(), before)


def _innings(innings: int) -> InningsPitched:
    return InningsPitched(outs=innings * InningsPitched.OUTS_PER_INNING)


class ForkedRatingsTest(BaseCase):
    """世界の作成が、分岐元の成績から能力を推定して保存する。"""

    def setUp(self):
        super().setUp()
        service = self.service
        self.slugger = service.register_player(self.team.id, "強打者", 3, "外野手")
        self.rookie = service.register_player(self.team.id, "新人", 4, "外野手")
        self.ace = service.register_player(self.team.id, "エース", 18, "投手")
        self.novice_pitcher = service.register_player(self.team.id, "控え投手", 19, "投手")
        self.rival_player = service.register_player(self.rival.id, "相手の選手", 3, "内野手")
        # 強打者: 本塁打が多く、四球も多い。エース: 奪三振が多く、四球が少なく、先発で長く投げる
        give_batting(
            self.team,
            self.rival,
            self.slugger.id,
            BattingLine(at_bats=480, singles=90, doubles=25, home_runs=50, walks=70, strikeouts=110),
        )
        give_pitching(
            self.team,
            self.rival,
            self.ace.id,
            PitchingLine(
                innings=_innings(180),
                strikeouts=220,
                hits_allowed=130,
                walks_allowed=30,
                home_runs_allowed=8,
                starts=28,
            ),
            day=2,
        )
        self.created = self._create(seed=21)
        self.ratings = self._ratings_by_name(self.created)

    def _create(self, *, seed, name="能力", start_year=START):
        return build_pennant_world_service().create_world(
            name=name, owner_id=None, source_league_ids=[self.league.id], start_year=start_year, seed=seed
        )

    @staticmethod
    def _ratings_by_name(created):
        repository = DjangoRatingsRepository(WorldScope.pennant(created.world.id))
        return {
            orm_models.Player.objects.get(id=item.player_id).name: item.ratings
            for item in repository.find_by_year(created.world.start_year)
        }

    def test_every_forked_player_gets_one_set_of_ratings(self):
        self.assertEqual(self.created.rating_count, self.created.player_count)
        self.assertEqual(len(self.ratings), 5)
        self.assertEqual(orm_models.PennantPlayerRatings.objects.count(), 5, "実データの選手の分は作らない")

    def test_the_kind_follows_the_registered_position(self):
        self.assertIsInstance(self.ratings["強打者"], BatterRatings)
        self.assertIsInstance(self.ratings["エース"], PitcherRatings)
        self.assertIsInstance(self.ratings["控え投手"], PitcherRatings, "成績が無くても投手は投手の能力")

    def test_the_record_of_the_source_player_shapes_the_ratings(self):
        slugger, rookie = self.ratings["強打者"], self.ratings["新人"]
        ace, novice = self.ratings["エース"], self.ratings["控え投手"]

        self.assertGreater(slugger.power, rookie.power)
        self.assertGreater(slugger.eye, rookie.eye)
        self.assertGreater(ace.stuff, novice.stuff)
        self.assertGreater(ace.control, novice.control)
        self.assertGreater(ace.stamina, novice.stamina, "先発で投げた投手はスタミナが高い")

    def test_a_player_without_a_record_is_below_the_first_team_average(self):
        rookie = self.ratings["新人"]

        self.assertLess(rookie.power, 50)
        self.assertLess(rookie.contact, 50)

    def test_the_same_seed_gives_the_same_ratings_in_another_world(self):
        again = self._create(seed=21, name="もう一つ")

        self.assertEqual(self._ratings_by_name(again), self.ratings)

    def test_a_different_seed_changes_only_the_hidden_growth_type(self):
        """成績から決まる項目は同じで、隠し値（成長型）だけがシードで変わる。"""
        growths = set()
        for seed in range(30):
            rated = self._ratings_by_name(self._create(seed=seed, name=f"シード{seed}"))
            self.assertEqual(rated["強打者"].power, self.ratings["強打者"].power)
            growths.add(rated["強打者"].growth)

        self.assertGreater(len(growths), 1)

    def test_a_failure_while_saving_the_ratings_leaves_no_world(self):
        class FailingRatings:
            def add_all(self, ratings):
                raise RuntimeError("能力の保存で失敗")

        def factory(scope):
            return WorldRepositories(
                leagues=DjangoLeagueRepository(scope),
                teams=DjangoTeamRepository(scope),
                ratings=FailingRatings(),  # type: ignore[arg-type]
            )

        service = PennantWorldService(
            real_leagues=DjangoLeagueRepository(REAL),
            real_teams=DjangoTeamRepository(REAL),
            real_fielding=DjangoFieldingTotalsQuery(REAL),
            worlds=DjangoWorldRepository(),
            repositories_for=factory,
        )
        worlds_before = orm_models.PennantWorld.objects.count()

        with self.assertRaises(RuntimeError):
            service.create_world(
                name="失敗", owner_id=None, source_league_ids=[self.league.id], start_year=2031, seed=1
            )

        self.assertEqual(orm_models.PennantWorld.objects.count(), worlds_before, "半端な世界を残さない")


class SameMaterialTest(BaseCase):
    """世界の作成と確認用のコマンド（`simulate_sample --from-real-leagues`）が、同じ材料から同じ能力を作る。

    材料（成績・年齢・守備）の組み立てと、推定に渡す母集団（現在在籍の選手全員）が同じでないと、
    リーグ全体の実測（盗塁・失策の目印）がずれて、確認で合った水準が世界の作成で合わなくなる。
    """

    def setUp(self):
        super().setUp()
        service = self.service
        self.runner = service.register_player(self.team.id, "走者", 5, "外野手")
        self.slugger = service.register_player(self.team.id, "強打者", 3, "外野手")
        self.ace = service.register_player(self.team.id, "エース", 18, "投手")
        # 退団した選手が大量に盗塁していた。世界には写されないので、目印（リーグの盗塁の水準）にも入らない
        self.retired = service.register_player(self.rival.id, "退団した走者", 9, "外野手")
        give_batting(
            self.team,
            self.rival,
            self.runner.id,
            BattingLine(at_bats=450, singles=100, walks=40, stolen_bases=30, caught_stealing=8),
        )
        give_batting(
            self.team,
            self.rival,
            self.slugger.id,
            BattingLine(at_bats=480, singles=90, doubles=25, home_runs=40, walks=70, strikeouts=110),
            day=2,
        )
        give_batting(
            self.rival,
            self.team,
            self.retired.id,
            BattingLine(at_bats=450, singles=100, walks=40, stolen_bases=60, caught_stealing=5),
            day=3,
        )
        give_pitching(
            self.team,
            self.rival,
            self.ace.id,
            PitchingLine(innings=_innings(150), strikeouts=170, hits_allowed=120, walks_allowed=35, starts=25),
            day=4,
        )
        service.retire_player(self.rival.id, self.retired.id)

    def test_the_world_and_the_check_give_the_same_ratings(self):
        world = build_pennant_world_service().create_world(
            name="同じ材料", owner_id=None, source_league_ids=[self.league.id], start_year=START, seed=21
        )
        in_world = {
            orm_models.Player.objects.get(id=item.player_id).name: item.ratings
            for item in DjangoRatingsRepository(WorldScope.pennant(world.world.id)).find_by_year(START)
        }
        service = build_pennant_world_service()
        source = service.load_source([self.league.id])

        checked = estimate_from_source(service, source, seed=21, year=START)

        in_check = {orm_models.Player.objects.get(id=pid).name: item.ratings for pid, item in checked.items()}
        self.assertEqual(in_check, in_world)
        self.assertNotIn("退団した走者", in_check, "確認でも、世界に写される現在在籍の選手だけを使う")

    def test_a_retired_player_does_not_move_the_league_norms(self):
        """退団した選手の盗塁は、目印に入らない。入ると、現役の走者の走力が低く見積もられる。"""
        service = build_pennant_world_service()
        source = service.load_source([self.league.id])
        with_retired = [player for teams in source.rosters for team in teams for player in team.players]

        only_active = service.estimate_ratings(source.active_players, seed=1, year=START)
        everyone = service.estimate_ratings(with_retired, seed=1, year=START)

        runner_active = next(i for i in only_active if i.player_id == self.runner.id).ratings
        runner_all = next(i for i in everyone if i.player_id == self.runner.id).ratings
        self.assertGreater(runner_active.speed, runner_all.speed)


class ForkedSpreadTest(BaseCase):
    """能力の散らばりの調整が、世界の作成を通る（母集団は世界に写す選手全員）。"""

    def setUp(self):
        super().setUp()
        self.names = []
        for index in range(MIN_POPULATION + 5):
            player = self.service.register_player(self.team.id, f"野手{index}", 20 + index, "外野手")
            self.names.append(player.name)
            if index < 10:
                give_batting(
                    self.team,
                    self.rival,
                    player.id,
                    BattingLine(
                        at_bats=450,
                        singles=90 + index,
                        home_runs=4 + 3 * index,
                        walks=30 + 4 * index,
                        strikeouts=140 - 8 * index,
                    ),
                    day=index + 1,
                )

    def test_the_standard_deviation_of_the_world_matches_the_target(self):
        created = build_pennant_world_service().create_world(
            name="散らばり", owner_id=None, source_league_ids=[self.league.id], start_year=START, seed=3
        )
        ratings = [
            item.ratings
            for item in DjangoRatingsRepository(WorldScope.pennant(created.world.id)).find_by_year(START)
            if isinstance(item.ratings, BatterRatings)
        ]

        for name in ("contact", "power", "eye"):
            with self.subTest(item=name):
                self.assertGreaterEqual(len(ratings), MIN_POPULATION)
                self.assertAlmostEqual(pstdev(getattr(r, name) for r in ratings), BATTER_SD, delta=0.8)


class FieldingTotalsQueryTest(BaseCase):
    def setUp(self):
        super().setUp()
        self.shortstop = self.service.register_player(self.team.id, "遊撃手", 6, "内野手")
        self.bench = self.service.register_player(self.team.id, "控え", 7, "内野手")
        first = give_batting(self.team, self.rival, self.shortstop.id, BattingLine(at_bats=4), day=1)
        second = give_batting(self.team, self.rival, self.shortstop.id, BattingLine(at_bats=4), day=2)
        for game, (putouts, assists, errors) in ((first, (2, 4, 1)), (second, (1, 3, 0))):
            orm_models.GameFieldingLine.objects.create(
                game_id=game.id, player_id=self.shortstop.id, putouts=putouts, assists=assists, errors=errors
            )

    def test_totals_are_summed_over_games(self):
        totals = DjangoFieldingTotalsQuery(REAL).totals_for([self.shortstop.id, self.bench.id])

        line = totals[self.shortstop.id]
        self.assertEqual((line.putouts, line.assists, line.errors), (3, 7, 1))
        self.assertEqual(line.total_chances, 11)

    def test_players_who_never_fielded_are_left_out(self):
        totals = DjangoFieldingTotalsQuery(REAL).totals_for([self.shortstop.id, self.bench.id])

        self.assertEqual(set(totals), {self.shortstop.id})

    def test_no_players_means_no_query_result(self):
        self.assertEqual(DjangoFieldingTotalsQuery(REAL).totals_for([]), {})

    def test_another_scope_does_not_see_the_real_games(self):
        created = build_pennant_world_service().create_world(
            name="守備", owner_id=None, source_league_ids=[self.league.id], start_year=START, seed=1
        )

        totals = DjangoFieldingTotalsQuery(WorldScope.pennant(created.world.id)).totals_for([self.shortstop.id])

        self.assertEqual(totals, {})
