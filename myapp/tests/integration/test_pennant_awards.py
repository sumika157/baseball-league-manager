"""ペナントの表彰（締めたシーズンのタイトル受賞者を保存する。#248）。

表彰は CLAUDE.md の「集計を保存しない」の例外なので、ここで次の前提を固定する。

- 締める処理が保存する受賞者は、締める直前にタイトルの画面と同じ集計で導いた受賞者と同じ
- 締めたシーズンの試合は締めた後に変わらない（だから保存した受賞者と、後から導いた受賞者はずれない）
- 締めた年の受賞者を読む画面（歴代・球団史・選手ページ）は表彰を読み、締めていない年だけを導く
  （同じ年を両方から読まない）
- 世界の範囲の外の表彰は読めも書けもせず、世界の削除で消える
"""

from collections import Counter
from io import StringIO
from unittest import mock

from django.core.management import CommandError, call_command
from django.urls import reverse

from myapp.application.pennant_awards import derive_award_rows
from myapp.application.services import TeamApplicationService
from myapp.domain.exceptions import AlreadyClosed, InvalidAward, InvalidWorld
from myapp.domain.pennant.awards import Award
from myapp.domain.pennant.offseason import plan_offseason
from myapp.domain.pennant.schedule import AdvanceTarget
from myapp.domain.pennant.season import is_season_closed, unclosed_completed_year
from myapp.infrastructure import orm_models
from myapp.infrastructure.queries import DjangoAwardQuery, DjangoOffseasonQuery
from myapp.infrastructure.repositories import DjangoAwardRepository, DjangoGameRepository
from myapp.presentation.views import (
    build_pennant_history_service,
    build_pennant_offseason_service,
    build_pennant_season_service,
    build_pennant_world_service,
    build_pennant_world_view,
)

from ..helpers import build_service, real_scope, world_scope
from .test_pennant_advance import YEAR
from .test_pennant_home import HomeCase


def year_games(world_id: int, year: int) -> list[tuple]:
    """その年の試合の得点と明細の数（締めた後に書き換わっていないことを突き合わせる）。"""
    return list(
        orm_models.Game.objects.filter(home_team__league__world_id=world_id, played_on__year=year)
        .order_by("id")
        .values_list("id", "played_on", "home_score", "away_score")
    ) + [
        (
            orm_models.GameBattingLine.objects.filter(
                game__home_team__league__world_id=world_id, game__played_on__year=year
            ).count(),
            orm_models.GamePitchingLine.objects.filter(
                game__home_team__league__world_id=world_id, game__played_on__year=year
            ).count(),
        )
    ]


class ClosedYearCase(HomeCase):
    """世界Aの1年目（YEAR）を締め、2年目（YEAR + 1）をシーズン終了まで進めたところ（締めていない）。"""

    league_id: int
    derived_before: list
    games_before: list

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.league_id = orm_models.League.objects.get(world_id=cls.world_a).id
        build_pennant_season_service(world_scope(cls.world_a)).advance(AdvanceTarget.SEASON_END)
        cls.derived_before = derive_award_rows(build_service(world_scope(cls.world_a)), YEAR, cls.league_id)
        cls.games_before = year_games(cls.world_a, YEAR)
        build_pennant_offseason_service(world_scope(cls.world_a)).close_season()
        build_pennant_season_service(world_scope(cls.world_a)).advance(AdvanceTarget.SEASON_END)

    def saved(self, world_id=None) -> list:
        return DjangoAwardQuery(world_scope(world_id or self.world_a)).list_awards()

    def context(self):
        return build_pennant_world_view(real_scope().workspace_id).get_context(self.world_a)

    def player_url(self, row, world_id=None) -> str:
        return reverse("pennant_player_detail", args=[world_id or self.world_a, row.team_id, row.player_id])


class SavedAwardsTest(ClosedYearCase):
    def test_the_saved_awards_are_the_titles_derived_when_closing(self):
        self.assertTrue(self.derived_before, "1年ぶんの試合があるので、受賞者はいる")
        self.assertEqual(self.saved(), self.derived_before)

    def test_tied_leaders_are_all_saved(self):
        """同率の1位は全員が受賞者（タイトルの画面の1位と同じ。上位で切らない）。"""
        titles = build_service(world_scope(self.world_a)).get_league_titles(self.league_id, YEAR, leaders=None)
        expected = {(d.key, e.player_id) for d in titles.departments for e in d.entries or [] if e.rank == 1}
        self.assertEqual({(row.title.value, row.player_id) for row in self.saved()}, expected)
        ties = Counter((row.title, row.year) for row in self.saved())
        self.assertTrue(any(count > 1 for count in ties.values()), "この世界には同率の1位がある（全員が表彰になる）")

    def test_the_closed_season_is_not_rewritten(self):
        """締めた年の試合は、締めた後も次のシーズンを回した後も変わらない。だから導き直しても表彰と同じ。"""
        self.assertEqual(year_games(self.world_a, YEAR), self.games_before)
        self.assertEqual(
            derive_award_rows(build_service(world_scope(self.world_a)), YEAR, self.league_id), self.saved()
        )

    def test_the_unclosed_year_is_not_saved(self):
        self.assertEqual({row.year for row in self.saved()}, {YEAR})

    def test_the_unclosed_year_agrees_with_is_season_closed(self):
        """画面が「導く年」とする年（`unclosed_completed_year`）は、締めたかの出典（`is_season_closed`）と食い違わない。"""

        def closed(year: int) -> bool:
            return is_season_closed(
                year=year, start_year=YEAR, next_year_ratings_exist=offseason.has_ratings(year + 1)
            )

        offseason = DjangoOffseasonQuery(world_scope(self.world_a))
        context = self.context()
        self.assertEqual(unclosed_completed_year(phase=context.phase, current_year=context.season_year), YEAR + 1)
        self.assertEqual((closed(YEAR), closed(YEAR + 1)), (True, False))

        build_pennant_offseason_service(world_scope(self.world_a)).close_season()

        context = self.context()
        self.assertIsNone(unclosed_completed_year(phase=context.phase, current_year=context.season_year))
        self.assertEqual((closed(YEAR), closed(YEAR + 1)), (True, True))

    def test_a_second_close_of_the_closed_year_adds_nothing(self):
        before = orm_models.PennantAward.objects.count()

        with self.assertRaises(AlreadyClosed):
            build_pennant_offseason_service(world_scope(self.world_a)).close_season(expected_year=YEAR)

        self.assertEqual(orm_models.PennantAward.objects.count(), before)

    def test_a_failure_while_writing_the_awards_leaves_nothing(self):
        """表彰は締める処理と同じトランザクションで書く。表彰で失敗したら、在籍も能力も日程も残らない。"""
        stints = orm_models.PlayerStint.objects.filter(team__league__world_id=self.world_a).count()

        with (
            mock.patch.object(DjangoAwardRepository, "add_all", side_effect=InvalidAward("失敗")),
            self.assertRaises(InvalidAward),
        ):
            build_pennant_offseason_service(world_scope(self.world_a)).close_season()

        self.assertEqual(orm_models.PlayerStint.objects.filter(team__league__world_id=self.world_a).count(), stints)
        self.assertFalse(orm_models.PennantPlayerRatings.objects.filter(year=YEAR + 2).exists())
        self.assertEqual({row.year for row in self.saved()}, {YEAR})

    def test_the_title_counts_reach_the_track_record(self):
        """実績の加点の材料は、締めた年の表彰と、いま締める年に導いた受賞の数（過去の年を導き直さない）。"""
        expected = Counter(row.player_id for row in self.saved())
        expected.update(
            row.player_id
            for row in derive_award_rows(build_service(world_scope(self.world_a)), YEAR + 1, self.league_id)
        )
        with (
            mock.patch("myapp.application.pennant_offseason.plan_offseason", wraps=plan_offseason) as planned,
            mock.patch.object(
                TeamApplicationService,
                "get_league_titles",
                autospec=True,
                side_effect=TeamApplicationService.get_league_titles,
            ) as titles,
        ):
            build_pennant_offseason_service(world_scope(self.world_a)).close_season()

        clubs = planned.call_args.args[0]
        shown = {p.player_id: p.track_record.titles for club in clubs for p in club.players}
        self.assertEqual({k: v for k, v in shown.items() if v}, {k: v for k, v in expected.items() if k in shown})
        self.assertTrue(any(count >= 1 for count in shown.values()))
        self.assertEqual({call.args[2] for call in titles.call_args_list}, {YEAR + 1}, "締めた年は導き直さない")
        self.assertEqual({row.year for row in self.saved()}, {YEAR, YEAR + 1})


class ScreensReadTheAwardsTest(ClosedYearCase):
    def test_the_history_reads_the_closed_year_from_the_awards(self):
        """締めた年は表彰を読み、締めていない年（シーズン終了のいまの年度）だけを導く。"""
        with mock.patch.object(
            TeamApplicationService,
            "get_league_titles",
            autospec=True,
            side_effect=TeamApplicationService.get_league_titles,
        ) as titles:
            history = build_pennant_history_service(world_scope(self.world_a)).get_world_history(
                self.context(), league_id=None
            )

        self.assertEqual({call.args[2] for call in titles.call_args_list}, {YEAR + 1})
        first, second = history.years
        shown = {(c.department_key, h.player_id) for c in (*first.batting, *first.pitching) for h in c.holders}
        self.assertEqual(shown, {(row.title.value, row.player_id) for row in self.saved()})
        derived = derive_award_rows(build_service(world_scope(self.world_a)), YEAR + 1, self.league_id)
        shown = {(c.department_key, h.player_id) for c in (*second.batting, *second.pitching) for h in c.holders}
        self.assertEqual(shown, {(row.title.value, row.player_id) for row in derived})

    def test_the_shown_record_is_the_saved_one(self):
        """出典が表彰であることを、保存した記録の表記を書き換えて確かめる（試合から導いた値なら変わらない）。"""
        award = orm_models.PennantAward.objects.filter(league_id=self.league_id, year=YEAR).first()
        orm_models.PennantAward.objects.filter(id=award.id).update(value="9.999")
        row = next(r for r in self.saved() if r.value == "9.999")

        pages = (
            reverse("pennant_history", args=[self.world_a]),
            reverse("pennant_team_history", args=[self.world_a, row.team_id]),
            self.player_url(row),
        )
        for url in pages:
            with self.subTest(url=url):
                self.assertContains(self.client.get(url), "9.999")

    def test_the_club_history_lists_the_awards_of_the_club(self):
        for team_id in {row.team_id for row in self.saved()}:
            with self.subTest(team_id=team_id):
                club = build_pennant_history_service(world_scope(self.world_a)).get_club_history(
                    self.context(), team_id
                )
                first = next(s for s in club.seasons if s.year == YEAR)
                self.assertEqual(
                    [(t.label, t.player_id) for t in first.titles],
                    [(r.label, r.player_id) for r in self.saved() if r.team_id == team_id],
                )


class PlayerPageTest(ClosedYearCase):
    def test_the_player_page_lists_the_titles(self):
        row = self.saved()[0]

        response = self.client.get(self.player_url(row))

        self.assertContains(response, "獲得タイトル")
        self.assertContains(response, row.label)
        awards = response.context["awards"]
        self.assertEqual(
            [a for a in awards if a.year == YEAR], [r for r in self.saved() if r.player_id == row.player_id]
        )

    def test_the_unclosed_year_is_derived_on_the_player_page(self):
        derived = derive_award_rows(build_service(world_scope(self.world_a)), YEAR + 1, self.league_id)
        row = derived[0]

        awards = self.client.get(self.player_url(row)).context["awards"]

        self.assertEqual(
            [a for a in awards if a.year == YEAR + 1], [r for r in derived if r.player_id == row.player_id]
        )

    def test_a_player_who_did_not_play_the_unclosed_year_is_not_derived(self):
        """締めていない年に出場していない選手は、その年のタイトルを導かない（リーグ1つぶんの集計を省く）。"""
        played = {
            *orm_models.GameBattingLine.objects.filter(game__year=YEAR + 1).values_list("player_id", flat=True),
            *orm_models.GamePitchingLine.objects.filter(game__year=YEAR + 1).values_list("player_id", flat=True),
        }
        stint = (
            orm_models.PlayerStint.objects.filter(team__league__world_id=self.world_a)
            .exclude(player_id__in=played)
            .first()
        )
        assert stint is not None, "2年目に出場していない選手（控え・新人）がいる"

        with mock.patch.object(
            TeamApplicationService,
            "get_league_titles",
            autospec=True,
            side_effect=TeamApplicationService.get_league_titles,
        ) as titles:
            response = self.client.get(
                reverse("pennant_player_detail", args=[self.world_a, stint.team_id, stint.player_id])
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(titles.call_count, 0)

    def test_a_player_without_titles_has_no_card(self):
        holders = {row.player_id for row in self.saved()} | {
            row.player_id
            for row in derive_award_rows(build_service(world_scope(self.world_a)), YEAR + 1, self.league_id)
        }
        stint = (
            orm_models.PlayerStint.objects.filter(team__league__world_id=self.world_a)
            .exclude(player_id__in=holders)
            .first()
        )

        response = self.client.get(
            reverse("pennant_player_detail", args=[self.world_a, stint.team_id, stint.player_id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "獲得タイトル")

    def test_the_real_data_page_has_no_titles(self):
        stint = orm_models.PlayerStint.objects.filter(team__league__world__isnull=True).first()

        response = self.client.get(reverse("player_detail", args=[stint.team_id, stint.player_id]))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "獲得タイトル")
        self.assertEqual(response.context["awards"], [])


class ClosedYearGamesTest(ClosedYearCase):
    """締めた年の試合は書けない（表彰が試合と食い違わない前提を、書き込みの入口で守る）。締めていない年は書ける。"""

    def game_of(self, year: int):
        game_id = (
            orm_models.Game.objects.filter(home_team__league__world_id=self.world_a, year=year)
            .order_by("id")
            .values_list("id", flat=True)[0]
        )
        return DjangoGameRepository(world_scope(self.world_a)).find_by_id(game_id)

    def test_a_game_of_the_closed_year_cannot_be_saved(self):
        game = self.game_of(YEAR)

        with self.assertRaises(AlreadyClosed) as raised:
            DjangoGameRepository(world_scope(self.world_a)).save(game)

        self.assertEqual(raised.exception.year, YEAR)

    def test_a_new_game_of_the_closed_year_cannot_be_added(self):
        game = self.game_of(YEAR)
        game.id = None
        before = orm_models.Game.objects.count()

        with self.assertRaises(AlreadyClosed):
            DjangoGameRepository(world_scope(self.world_a)).add_all([game])

        self.assertEqual(orm_models.Game.objects.count(), before)

    def test_a_game_of_the_unclosed_year_can_still_be_saved(self):
        """締める前の年（シーズン終了の後。ポストシーズンの試合など）は書ける。"""
        game = self.game_of(YEAR + 1)

        DjangoGameRepository(world_scope(self.world_a)).save(game)


class AwardScopeTest(ClosedYearCase):
    def award(self, **changes) -> Award:
        row = self.saved()[0]
        values = {
            "year": YEAR + 5,
            "league_id": row.league_id,
            "title": row.title,
            "player_id": row.player_id,
            "team_id": row.team_id,
            "value": row.value,
        }
        return Award(**{**values, **changes})

    def test_a_year_with_awards_cannot_be_written_again(self):
        with self.assertRaises(InvalidAward):
            DjangoAwardRepository(world_scope(self.world_a)).add_all([self.award(year=YEAR)])

    def test_the_same_award_twice_is_refused(self):
        with self.assertRaises(InvalidAward):
            DjangoAwardRepository(world_scope(self.world_a)).add_all([self.award(), self.award()])

    def test_the_real_data_cannot_hold_awards(self):
        with self.assertRaises(InvalidWorld):
            DjangoAwardRepository(real_scope()).add_all([self.award()])

    def test_another_world_neither_reads_nor_writes_them(self):
        self.assertEqual(self.saved(self.world_b), [])
        self.assertEqual(DjangoAwardRepository(world_scope(self.world_b)).count_by_player(), {})
        with self.assertRaises(InvalidAward):
            DjangoAwardRepository(world_scope(self.world_b)).add_all([self.award()])

    def test_a_player_or_team_outside_the_world_is_refused(self):
        other_team = orm_models.Team.objects.filter(league__world_id=self.world_b).first()
        other_player = orm_models.PlayerStint.objects.filter(team=other_team).first().player_id
        real_player = orm_models.PlayerStint.objects.filter(team__league__world__isnull=True).first().player_id
        repository = DjangoAwardRepository(world_scope(self.world_a))
        for award in (
            self.award(team_id=other_team.id),
            self.award(player_id=other_player),
            self.award(player_id=real_player),
        ):
            with self.subTest(award=award), self.assertRaises(InvalidAward):
                repository.add_all([award])

    def test_deleting_the_world_deletes_its_awards(self):
        self.assertTrue(orm_models.PennantAward.objects.filter(league__world_id=self.world_a).exists())

        build_pennant_world_service(real_scope()).delete_world(self.world_a)

        self.assertFalse(orm_models.PennantAward.objects.filter(league_id=self.league_id).exists())


class RecordMissingAwardsTest(ClosedYearCase):
    """表彰を記録する前に締めた世界は、管理コマンドで締めた年の表彰を補う（締めていない年は書かない）。"""

    def test_the_command_fills_the_closed_year_only(self):
        orm_models.PennantAward.objects.filter(league__world_id=self.world_a).delete()
        out = StringIO()

        call_command("pennant_record_awards", world=self.world_a, stdout=out)

        self.assertIn(f"{YEAR}年の表彰を記録しました", out.getvalue())
        self.assertEqual(self.saved(), self.derived_before)

    def test_running_it_again_writes_nothing(self):
        out = StringIO()

        call_command("pennant_record_awards", world=self.world_a, stdout=out)

        self.assertIn("表彰の無い締めた年はありません", out.getvalue())
        self.assertEqual({row.year for row in self.saved()}, {YEAR})

    def test_check_lists_the_closed_years_without_awards_and_writes_nothing(self):
        orm_models.PennantAward.objects.filter(league__world_id=self.world_a).delete()

        with self.assertRaises(CommandError) as raised:
            call_command("pennant_record_awards", world=self.world_a, check=True, stdout=StringIO())

        self.assertIn(f"{YEAR}年", str(raised.exception))
        self.assertNotIn(f"{YEAR + 1}年", str(raised.exception), "締めていない年は数えない")
        self.assertEqual(self.saved(), [])

    def test_check_passes_when_every_closed_year_has_awards(self):
        out = StringIO()

        call_command("pennant_record_awards", world=self.world_a, check=True, stdout=out)

        self.assertIn("表彰の無い締めた年はありません", out.getvalue())
