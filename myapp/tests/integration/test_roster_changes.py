"""ロスターの変更。移籍・外国人枠・主将の指名。"""

from django.contrib.auth.models import User
from django.urls import reverse

from myapp.domain.exceptions import (
    DuplicateJerseyNumber,
    ForeignPlayerQuotaExceeded,
)
from myapp.domain.value_objects import (
    BattingLine,
)
from myapp.infrastructure import orm_models
from myapp.infrastructure.repositories import (
    DjangoTeamRepository,
)

from ..helpers import (
    give_batting,
    login_as_manager,
    play_game,
)
from .base import BaseCase


class TransferTest(BaseCase):
    """移籍と経歴。"""

    def setUp(self):
        super().setUp()
        self.player = self.service.register_player(self.team.id, "山田", 10, "内野手")

    def test_transfer_closes_the_old_stint_and_opens_a_new_one(self):
        self.service.transfer_player(
            self.player.id,
            from_team_id=self.team.id,
            to_team_id=self.rival.id,
            number=7,
            year=2026,
        )

        stints = list(orm_models.PlayerStint.objects.filter(player_id=self.player.id).order_by("from_year"))
        self.assertEqual(len(stints), 2)
        self.assertEqual(stints[0].team_id, self.team.id)
        self.assertEqual(stints[0].to_year, 2026)
        self.assertEqual(stints[1].team_id, self.rival.id)
        self.assertEqual(stints[1].number, 7)
        self.assertIsNone(stints[1].to_year)

    def test_player_appears_on_the_new_roster_only(self):
        self.service.transfer_player(
            self.player.id,
            from_team_id=self.team.id,
            to_team_id=self.rival.id,
            number=7,
            year=2026,
        )

        self.assertEqual(self.service.list_batters(self.team.id).rows, [])
        self.assertEqual([r.name for r in self.service.list_batters(self.rival.id).rows], ["山田"])

    def test_stats_follow_the_player_not_the_team(self):
        """成績は選手に紐づくので、移籍しても失われない。"""
        give_batting(self.team, self.rival, self.player.id, BattingLine(at_bats=10, singles=3))

        self.service.transfer_player(
            self.player.id,
            from_team_id=self.team.id,
            to_team_id=self.rival.id,
            number=7,
            year=2026,
        )

        detail = self.service.get_player_detail(self.rival.id, self.player.id)
        self.assertEqual(detail.at_bats, 10)

    def test_the_old_number_becomes_available(self):
        self.service.transfer_player(
            self.player.id,
            from_team_id=self.team.id,
            to_team_id=self.rival.id,
            number=7,
            year=2026,
        )

        # 空いた10番を別の選手が使える
        self.service.register_player(self.team.id, "田中", 10, "外野手")
        self.assertEqual([r.name for r in self.service.list_batters(self.team.id).rows], ["田中"])

    def test_number_in_use_at_the_destination_is_rejected(self):
        self.service.register_player(self.rival.id, "先客", 7, "外野手")

        with self.assertRaises(DuplicateJerseyNumber):
            self.service.transfer_player(
                self.player.id,
                from_team_id=self.team.id,
                to_team_id=self.rival.id,
                number=7,
                year=2026,
            )

    def test_career_is_visible_on_the_team_aggregate(self):
        self.service.transfer_player(
            self.player.id,
            from_team_id=self.team.id,
            to_team_id=self.rival.id,
            number=7,
            year=2026,
        )

        team = DjangoTeamRepository().find_by_id(self.rival.id)
        career = team.find_player(self.player.id).career

        self.assertEqual([s.team_name for s in career], ["相手チーム", "テストチーム"])

    def test_admin_shows_the_current_team(self):
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))
        response = self.client.get("/admin/myapp/player/")

        self.assertContains(response, "テストチーム")
        self.assertContains(response, "field-current_number")

    def test_failed_transfer_leaves_the_source_team_untouched(self):
        """検査に失敗したら、元チームの退団も取り消されること（部分的な保存を残さない）。"""
        self.service.register_player(self.rival.id, "先客", 7, "外野手")

        with self.assertRaises(DuplicateJerseyNumber):
            self.service.transfer_player(
                self.player.id,
                from_team_id=self.team.id,
                to_team_id=self.rival.id,
                number=7,
                year=2026,
            )

        stint = orm_models.PlayerStint.objects.get(player_id=self.player.id)
        self.assertIsNone(stint.to_year)
        self.assertEqual(stint.team_id, self.team.id)


class ForeignPlayerQuotaTest(BaseCase):
    """外国人枠（登録上限・試合出場上限）。"""

    def setUp(self):
        super().setUp()
        self.foreign = self.service.register_player(self.team.id, "助っ人", 50, "外野手")
        orm_models.Player.objects.filter(id=self.foreign.id).update(is_foreign_player=True)

    def test_transfer_rejects_when_destination_quota_exceeded(self):
        orm_models.League.objects.filter(id=self.league.id).update(foreign_player_roster_limit=0)

        with self.assertRaises(ForeignPlayerQuotaExceeded):
            self.service.transfer_player(
                self.foreign.id,
                from_team_id=self.team.id,
                to_team_id=self.rival.id,
                number=99,
                year=2026,
            )

        # ロールバックされ、元チームの在籍は閉じられていない
        stint = orm_models.PlayerStint.objects.get(player_id=self.foreign.id)
        self.assertIsNone(stint.to_year)
        self.assertEqual(stint.team_id, self.team.id)

    def test_update_game_rejects_when_home_team_quota_exceeded(self):
        orm_models.League.objects.filter(id=self.league.id).update(foreign_player_game_limit=0)
        game = play_game(self.team, self.rival)

        with self.assertRaises(ForeignPlayerQuotaExceeded):
            self.service.update_game(
                game.id,
                year=2026,
                played_on=game.played_on,
                home_team_id=self.team.id,
                away_team_id=self.rival.id,
                home_score=1,
                away_score=0,
                batting={self.foreign.id: BattingLine(at_bats=1, singles=1)},
            )
        self.assertEqual(orm_models.GameBattingLine.objects.count(), 0)

    def test_update_game_rejects_when_away_team_quota_exceeded(self):
        """ホーム・ビジターは独立に判定する。ホームに助っ人がいなくても、
        ビジター側の上限超過は検出される。"""
        rival_foreign = self.service.register_player(self.rival.id, "助っ人2", 51, "外野手")
        orm_models.Player.objects.filter(id=rival_foreign.id).update(is_foreign_player=True)
        orm_models.League.objects.filter(id=self.league.id).update(foreign_player_game_limit=0)
        game = play_game(self.team, self.rival)

        with self.assertRaises(ForeignPlayerQuotaExceeded):
            self.service.update_game(
                game.id,
                year=2026,
                played_on=game.played_on,
                home_team_id=self.team.id,
                away_team_id=self.rival.id,
                home_score=1,
                away_score=0,
                batting={rival_foreign.id: BattingLine(at_bats=1, singles=1)},
            )

    def test_update_game_allows_exactly_at_the_game_limit(self):
        orm_models.League.objects.filter(id=self.league.id).update(foreign_player_game_limit=1)
        game = play_game(self.team, self.rival)

        self.service.update_game(
            game.id,
            year=2026,
            played_on=game.played_on,
            home_team_id=self.team.id,
            away_team_id=self.rival.id,
            home_score=1,
            away_score=0,
            batting={self.foreign.id: BattingLine(at_bats=1, singles=1)},
        )  # 例外にならない

        self.assertEqual(orm_models.GameBattingLine.objects.count(), 1)

    def test_update_game_with_no_limit_set_never_rejects(self):
        """空欄（無制限）はリーグの既定値（3人）とは別に、明示的に検証しておく。"""
        orm_models.League.objects.filter(id=self.league.id).update(foreign_player_game_limit=None)
        game = play_game(self.team, self.rival)

        self.service.update_game(
            game.id,
            year=2026,
            played_on=game.played_on,
            home_team_id=self.team.id,
            away_team_id=self.rival.id,
            home_score=1,
            away_score=0,
            batting={self.foreign.id: BattingLine(at_bats=1, singles=1)},
        )  # 例外にならない

        self.assertEqual(orm_models.GameBattingLine.objects.count(), 1)


class CaptaincyApplicationTest(BaseCase):
    """主将の指名・解任。"""

    def setUp(self):
        super().setUp()
        self.player = self.service.register_player(self.team.id, "山田", 10, "内野手")

    def test_appoint_and_remove_round_trip_through_the_database(self):
        self.service.appoint_captain(self.team.id, self.player.id, 2026)

        team = DjangoTeamRepository().find_by_id(self.team.id)
        self.assertEqual(team.current_captain.id, self.player.id)

        self.service.remove_captain(self.team.id, self.player.id, 2027)

        team = DjangoTeamRepository().find_by_id(self.team.id)
        self.assertIsNone(team.current_captain)

    def test_player_edit_appoints_a_captain(self):
        login_as_manager(self.client, self.team, username="u")

        response = self.client.post(
            reverse("player_edit", args=[self.team.id, self.player.id]),
            {"appoint_captain": "1"},
        )

        self.assertRedirects(response, reverse("player_edit", args=[self.team.id, self.player.id]))
        team = DjangoTeamRepository().find_by_id(self.team.id)
        self.assertEqual(team.current_captain.id, self.player.id)

    def test_player_edit_shows_duplicate_captain_error(self):
        other = self.service.register_player(self.team.id, "田中", 11, "外野手")
        self.service.appoint_captain(self.team.id, other.id)
        login_as_manager(self.client, self.team, username="u")

        response = self.client.post(
            reverse("player_edit", args=[self.team.id, self.player.id]),
            {"appoint_captain": "1"},
            follow=True,
        )

        self.assertContains(response, "には既に主将")
        team = DjangoTeamRepository().find_by_id(self.team.id)
        self.assertEqual(team.current_captain.id, other.id)
