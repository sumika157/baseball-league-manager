"""シーズンを締める処理の検査（引退・ドラフト・翌年の能力・日程・編成の後始末・二重実行・原子性）。

世界は `test_pennant_advance.py` と同じ球団2つ（1リーグ）。1シーズン143試合を最後まで進めた世界を、
クラスで1度だけ作り、各テストが締める（テストごとに巻き戻る）。引退は乱数で決まるので、
特定の選手が引退する前提のテストは `decide_retirements` を差し替えて決める。
"""

from collections import Counter
from datetime import date
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import transaction
from django.db.models import Count

from myapp.application.dto import SeasonClosed
from myapp.domain.exceptions import (
    AlreadyClosed,
    InvalidRatings,
    InvalidSchedule,
    SeasonLimitReached,
    SeasonNotFinished,
    WorldNotFound,
)
from myapp.domain.pennant.club_plan import ClubPlan, LineupChoice, PlanSection
from myapp.domain.pennant.schedule import AdvanceTarget
from myapp.domain.pennant.season import SeasonPhase, season_phase, season_year
from myapp.domain.pennant.world import WorldScope
from myapp.domain.value_objects import FieldingPosition
from myapp.infrastructure import orm_models
from myapp.infrastructure.queries import DjangoSimulationContextQuery
from myapp.infrastructure.repositories import DjangoClubPlanRepository, DjangoFixtureRepository, DjangoWorldRepository
from myapp.presentation.views import (
    build_pennant_offseason_service,
    build_pennant_season_service,
    build_pennant_world_service,
)

from .test_pennant_advance import SEASON_GAMES, YEAR, SeasonCase

DECIDE_RETIREMENTS = "myapp.domain.pennant.offseason.decide_retirements"


def active_stints(world_id: int, *, to_year=None):
    return orm_models.PlayerStint.objects.filter(team__league__world_id=world_id, to_year=to_year)


def ratings_of(world_id: int, year: int):
    return orm_models.PennantPlayerRatings.objects.filter(
        player__stints__team__league__world_id=world_id, year=year
    ).distinct()


class ClosedSeasonCase(SeasonCase):
    """世界Aを、シーズン終了まで進めたところから始める。世界Cは1試合もしていない。"""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        for world_id in (cls.world_a,):
            build_pennant_season_service(world_id).advance(AdvanceTarget.SEASON_END)

    def close(self, world_id=None, **kwargs) -> SeasonClosed:
        return build_pennant_offseason_service(self.world_a if world_id is None else world_id).close_season(**kwargs)

    def counts(self, world_id=None) -> dict[str, int]:
        world_id = self.world_a if world_id is None else world_id
        return {
            "players": orm_models.Player.objects.filter(stints__team__league__world_id=world_id).distinct().count(),
            "stints": orm_models.PlayerStint.objects.filter(team__league__world_id=world_id).count(),
            "closed": active_stints(world_id, to_year=YEAR).count(),
            "ratings": orm_models.PennantPlayerRatings.objects.filter(
                player__stints__team__league__world_id=world_id
            ).count(),
            "fixtures": orm_models.PennantFixture.objects.filter(home_team__league__world_id=world_id).count(),
        }

    def managed_team_id(self, world_id=None) -> int:
        team_id = DjangoWorldRepository().find_by_id(self.world_a if world_id is None else world_id).managed_team_id
        assert team_id is not None
        return team_id

    def first_players(self, team_id: int, count: int) -> list[int]:
        return list(
            orm_models.PlayerStint.objects.filter(team_id=team_id, to_year=None)
            .order_by("number")
            .values_list("player_id", flat=True)[:count]
        )


class CloseSeasonTest(ClosedSeasonCase):
    def test_retired_players_leave_in_the_closed_year_and_rookies_join_the_next_year(self):
        result = self.close()

        self.assertEqual((result.year, result.next_year), (YEAR, YEAR + 1))
        self.assertEqual(active_stints(self.world_a, to_year=YEAR).count(), result.retired_count)
        rookies = active_stints(self.world_a).filter(from_year=YEAR + 1)
        self.assertEqual(rookies.count(), result.draftee_count)
        self.assertGreaterEqual(result.draftee_count, 2 * 2, "各球団2人以上")
        self.assertFalse(active_stints(self.world_a).filter(from_year__gt=YEAR + 1).exists())
        for player in orm_models.Player.objects.filter(stints__in=rookies):
            self.assertTrue(player.birth_date, f"{player.name} のプロフィールが入っている")

    def test_current_jersey_numbers_do_not_overlap_in_a_team(self):
        self.close()

        numbers = active_stints(self.world_a).values("team_id", "number").annotate(n=Count("id"))
        self.assertTrue(numbers)
        self.assertEqual([row for row in numbers if row["n"] > 1], [])

    def test_next_year_ratings_exist_for_everyone_current_and_nobody_retired(self):
        with mock.patch(DECIDE_RETIREMENTS, return_value=tuple(self.first_players(self.managed_team_id(), 3))):
            self.close()

        current = set(active_stints(self.world_a).values_list("player_id", flat=True))
        next_year = set(ratings_of(self.world_a, YEAR + 1).values_list("player_id", flat=True))
        self.assertEqual(next_year, current)
        retired = set(active_stints(self.world_a, to_year=YEAR).values_list("player_id", flat=True))
        self.assertEqual(len(retired), 3)
        self.assertFalse(next_year & retired)
        # 年ごとの推移が残る: 締めた年の能力は消えない
        self.assertTrue(ratings_of(self.world_a, YEAR).filter(player_id__in=retired).count() == 3)

    def test_the_next_schedule_has_143_games_per_team_and_the_phase_is_before_the_opening(self):
        result = self.close()

        fixtures = DjangoFixtureRepository(WorldScope.pennant(self.world_a)).find_all()
        self.assertEqual(len(fixtures), result.fixture_count)
        self.assertEqual({f.date.year for f in fixtures}, {YEAR + 1})
        per_team = Counter(team for f in fixtures for team in (f.home_team_id, f.visitor_team_id))
        self.assertEqual(set(per_team.values()), {SEASON_GAMES})
        self.assertEqual(len(per_team), 2)
        last_played = DjangoSimulationContextQuery(WorldScope.pennant(self.world_a)).last_played_on()
        first = fixtures[0].date
        self.assertIs(season_phase(last_played_on=last_played, next_fixture_on=first), SeasonPhase.BEFORE_OPENING)
        self.assertEqual(season_year(next_fixture_on=first, last_played_on=last_played, start_year=YEAR), YEAR + 1)

    def test_a_second_close_raises_and_adds_nothing(self):
        self.close()
        before = self.counts()

        with self.assertRaisesMessage(AlreadyClosed, f"{YEAR}年のシーズンは既に締めています。"):
            self.close()

        self.assertEqual(self.counts(), before)

    def test_a_season_with_games_left_cannot_be_closed(self):
        build_pennant_season_service(self.world_other_seed).advance(AdvanceTarget.WEEK)
        before = self.counts(self.world_other_seed)

        with self.assertRaises(SeasonNotFinished):
            self.close(self.world_other_seed)

        self.assertEqual(self.counts(self.world_other_seed), before)

    def test_a_world_that_has_not_played_cannot_be_closed(self):
        with self.assertRaises(SeasonNotFinished):
            self.close(self.world_other_seed)

    def test_an_unexpected_year_is_refused_without_writing(self):
        before = self.counts()

        with self.assertRaisesRegex(AlreadyClosed, "開き直してください"):
            # 開幕年より前の年度は、締めたことのある年ではない（「既に締めています」と言わない）
            self.close(expected_year=YEAR - 1)

        self.assertEqual(self.counts(), before)
        self.close(expected_year=YEAR)

    def test_the_final_season_cannot_be_closed(self):
        before = self.counts()

        with mock.patch("myapp.domain.pennant.season.MAX_SEASONS_PER_WORLD", 1):
            with self.assertRaises(SeasonLimitReached):
                self.close()
            option = build_pennant_offseason_service(self.world_a).close_option()

        self.assertEqual(self.counts(), before)
        self.assertFalse(option.can_close)
        self.assertTrue(option.reason)

    def test_unknown_world_is_not_found(self):
        with self.assertRaises(WorldNotFound):
            self.close(self.world_a + 1000)

    def test_the_option_follows_the_state_of_the_season(self):
        service = build_pennant_offseason_service(self.world_a)

        before = service.close_option()
        self.close()
        after = service.close_option()

        self.assertTrue(before.can_close)
        self.assertEqual((before.year, before.next_year), (YEAR, YEAR + 1))
        self.assertFalse(after.can_close)
        self.assertIn("既に締めています", after.reason)
        unplayed = build_pennant_offseason_service(self.world_other_seed).close_option()
        self.assertFalse(unplayed.can_close)
        self.assertIsNone(unplayed.year)

    def test_closing_the_same_world_twice_from_the_same_state_gives_the_same_result(self):
        """乱数は世界のシード・年・選手 / 球団の id から作る。巻き戻してもう一度締めても、同じ結果になる。

        （別の世界は球団と選手の id が違うので、同じシードでも結果は同じにならない。）
        """

        def snapshot():
            people = sorted(
                orm_models.PlayerStint.objects.filter(team__league__world_id=self.world_a).values_list(
                    "team__name", "player__name", "number", "from_year", "to_year", "player__position"
                )
            )
            ratings = sorted(
                (row.player.name, row.year, row.contact, row.power, row.eye, row.speed, row.fielding)
                for row in ratings_of(self.world_a, YEAR + 1).select_related("player")
            )
            fixtures = sorted(
                orm_models.PennantFixture.objects.filter(home_team__league__world_id=self.world_a).values_list(
                    "date", "home_team__name", "visitor_team__name"
                )
            )
            return people, ratings, fixtures

        with transaction.atomic():
            first = self.close()
            first_snapshot = snapshot()
            transaction.set_rollback(True)
        second = self.close()

        self.assertEqual(first, second)
        self.assertEqual(first_snapshot, snapshot())

    def test_deleting_a_closed_world_deletes_retired_players_and_rookies(self):
        with mock.patch(DECIDE_RETIREMENTS, return_value=tuple(self.first_players(self.managed_team_id(), 2))):
            result = self.close()
        self.assertGreater(result.draftee_count, 0)
        player_ids = list(
            orm_models.Player.objects.filter(stints__team__league__world_id=self.world_a).values_list("id", flat=True)
        )

        build_pennant_world_service().delete_world(self.world_a)

        self.assertFalse(orm_models.Player.objects.filter(id__in=player_ids).exists())
        self.assertFalse(orm_models.PennantPlayerRatings.objects.filter(player_id__in=player_ids).exists())
        self.assertEqual(self.counts(self.world_b)["players"] > 0, True, "別の世界は残る")


class ClubPlanReleaseTest(ClosedSeasonCase):
    def plan_repository(self) -> DjangoClubPlanRepository:
        return DjangoClubPlanRepository(WorldScope.pennant(self.world_a))

    def test_manual_sections_with_a_retired_player_return_to_automatic(self):
        team_id = self.managed_team_id()
        players = self.first_players(team_id, 12)
        retiring, others = players[0], players[1:]
        self.plan_repository().save(
            ClubPlan(
                team_id,
                active_ids=tuple(players),
                lineup=tuple(LineupChoice(player_id, FieldingPosition.DESIGNATED_HITTER) for player_id in others[:9]),
                rotation=tuple(others[:2]),
                closer_id=others[2],
            )
        )

        with mock.patch(DECIDE_RETIREMENTS, return_value=(retiring,)):
            result = self.close()

        # 1軍登録を戻すと、依存するオーダー・ローテーション・抑えも戻る
        self.assertEqual(set(result.released_sections), set(PlanSection))
        self.assertEqual(result.released_club_count, 1)
        self.assertEqual(self.plan_repository().find_all(), [])

    def test_only_the_section_with_a_retired_player_returns(self):
        team_id = self.managed_team_id()
        players = self.first_players(team_id, 12)
        retiring, others = players[0], players[1:]
        self.plan_repository().save(ClubPlan(team_id, active_ids=tuple(others), rotation=(retiring, others[0])))

        with mock.patch(DECIDE_RETIREMENTS, return_value=(retiring,)):
            result = self.close()

        self.assertEqual(result.released_sections, (PlanSection.ROTATION,))
        (saved,) = self.plan_repository().find_all()
        self.assertEqual(saved.active_ids, tuple(others), "退団者を含まない区画は手動のまま")
        self.assertIsNone(saved.rotation)

    def test_a_plan_without_retired_players_is_kept(self):
        team_id = self.managed_team_id()
        players = self.first_players(team_id, 12)
        plan = ClubPlan(team_id, active_ids=tuple(players[1:]))
        self.plan_repository().save(plan)

        with mock.patch(DECIDE_RETIREMENTS, return_value=(players[0],)):
            result = self.close()

        self.assertEqual(result.released_sections, ())
        self.assertEqual(self.plan_repository().find_all(), [plan])


class ForeignQuotaTest(ClosedSeasonCase):
    def test_a_team_at_its_foreign_quota_gets_no_foreign_rookie(self):
        league = orm_models.League.objects.get(world_id=self.world_a)
        league.foreign_player_roster_limit = 1
        league.save()
        team_id = self.managed_team_id()
        orm_models.Player.objects.filter(id=self.first_players(team_id, 1)[0]).update(is_foreign_player=True)

        with mock.patch(DECIDE_RETIREMENTS, return_value=()):
            self.close()

        added = orm_models.Player.objects.filter(
            stints__team_id=team_id, stints__from_year=YEAR + 1, is_foreign_player=True
        )
        self.assertFalse(added.exists())
        for team in orm_models.Team.objects.filter(league=league):
            foreign = orm_models.Player.objects.filter(
                stints__team=team, stints__to_year=None, is_foreign_player=True
            ).count()
            self.assertLessEqual(foreign, 1, team.name)


class AtomicityTest(ClosedSeasonCase):
    def test_a_failure_in_the_middle_leaves_nothing(self):
        team_id = self.managed_team_id()
        players = self.first_players(team_id, 12)
        plan = ClubPlan(team_id, active_ids=tuple(players))
        DjangoClubPlanRepository(WorldScope.pennant(self.world_a)).save(plan)
        before = self.counts()

        # 最後の書き込み（日程）で失敗させる。在籍・新人・能力・編成の後始末がすべて巻き戻る
        with (
            mock.patch(DECIDE_RETIREMENTS, return_value=(players[0],)),
            mock.patch.object(DjangoFixtureRepository, "add_all", side_effect=InvalidSchedule("失敗")),
            self.assertRaises(InvalidSchedule),
        ):
            self.close()

        self.assertEqual(self.counts(), before)
        self.assertEqual(DjangoClubPlanRepository(WorldScope.pennant(self.world_a)).find_all(), [plan])
        self.assertFalse(orm_models.Player.objects.filter(stints__from_year=YEAR + 1).exists())


class AdvanceAfterCloseTest(ClosedSeasonCase):
    def test_the_next_season_is_played_with_the_next_year_ratings(self):
        result = self.close()

        report = build_pennant_season_service(self.world_a).advance(AdvanceTarget.WEEK)

        self.assertFalse(report.schedule_created, "翌年の日程は締めるときに作ってある")
        self.assertGreater(report.games, 0)
        years = set(
            orm_models.Game.objects.filter(home_team__league__world_id=self.world_a).values_list("year", flat=True)
        )
        self.assertEqual(years, {YEAR, YEAR + 1})
        first = min(report.played_dates)
        self.assertGreater(first, date(YEAR, 12, 31))
        # 新人は翌年の能力を持ち、日を進める材料（球団の現在の選手）に含まれる
        context = DjangoSimulationContextQuery(WorldScope.pennant(self.world_a)).teams()
        in_context = {player.player_id for team in context for player in team.players}
        rookies = set(active_stints(self.world_a).filter(from_year=YEAR + 1).values_list("player_id", flat=True))
        self.assertEqual(len(rookies), result.draftee_count)
        self.assertTrue(rookies <= in_context)
        self.assertEqual(
            set(ratings_of(self.world_a, YEAR + 1).values_list("player_id", flat=True)) & rookies, rookies
        )


class DoubleCloseTest(ClosedSeasonCase):
    def test_a_future_expected_year_is_refused_with_a_message_that_fits(self):
        before = self.counts()

        with self.assertRaisesMessage(AlreadyClosed, "開き直してください"):
            self.close(expected_year=YEAR + 1)

        self.assertEqual(self.counts(), before)

    def test_closing_again_after_the_next_schedule_is_gone_is_stopped_by_the_ratings_check(self):
        """翌年の日程が無くなっても、翌年の能力が「締めた」事実として残る。"""
        self.close()
        orm_models.PennantFixture.objects.filter(home_team__league__world_id=self.world_a).delete()
        before = self.counts()

        with self.assertRaises(AlreadyClosed):
            self.close()

        self.assertEqual(self.counts(), before)

    def test_a_second_close_that_slips_past_the_checks_leaves_nothing(self):
        """検査と翌年の能力の確認をすり抜けた2回目も、能力の保存（`add_all` の検査）で落ちて、
        在籍・新人・能力・編成が何も残らない。

        `add_all` の検査もすり抜けたときの最後の砦は DB の一意制約（`IntegrityError`）で、
        同じトランザクションごと巻き戻る。
        """
        self.close()
        team_id = self.managed_team_id()
        players = self.first_players(team_id, 12)
        plan = ClubPlan(team_id, active_ids=tuple(players))
        DjangoClubPlanRepository(WorldScope.pennant(self.world_a)).save(plan)
        before = self.counts()
        service = build_pennant_offseason_service(self.world_a)
        find_by_year = service._ratings.find_by_year

        def blind_to_next_year(year):
            return [] if year == YEAR + 1 else find_by_year(year)

        with (
            mock.patch.object(service, "_check", return_value=YEAR),
            mock.patch.object(service._ratings, "find_by_year", side_effect=blind_to_next_year),
            mock.patch(DECIDE_RETIREMENTS, return_value=(players[0],)),
            self.assertRaises(InvalidRatings),
        ):
            service.close_season()

        self.assertEqual(self.counts(), before)
        self.assertEqual(DjangoClubPlanRepository(WorldScope.pennant(self.world_a)).find_all(), [plan])
        self.assertEqual(active_stints(self.world_a, to_year=YEAR + 1).count(), 0)


class CloseSeasonCommandTest(ClosedSeasonCase):
    def test_the_command_closes_the_season_and_reports_it(self):
        out = StringIO()

        call_command("pennant_close_season", "--world", str(self.world_a), stdout=out)

        self.assertIn(f"{YEAR}年のシーズンを締めました", out.getvalue())
        self.assertIn("所要", out.getvalue())
        self.assertTrue(ratings_of(self.world_a, YEAR + 1).exists())

    def test_the_command_reports_a_domain_error_as_a_command_error(self):
        with self.assertRaises(CommandError) as raised:
            call_command("pennant_close_season", "--world", str(self.world_other_seed), stdout=StringIO())

        self.assertIn("まだ試合をしていない", str(raised.exception))
