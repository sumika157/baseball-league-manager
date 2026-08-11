"""選手の画面。一覧・個人ページ・編集・検索。"""

from django.contrib.auth.models import User
from django.urls import reverse

from myapp.domain.value_objects import (
    BattingLine,
    InningsPitched,
    PitchingLine,
)
from myapp.infrastructure import orm_models
from myapp.infrastructure.repositories import (
    DjangoTeamRepository,
)

from ..helpers import (
    give_batting,
    give_pitching,
    login_as_manager,
    play_game,
)
from .base import BaseCase


class PlayerListViewTest(BaseCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("player_list", args=[self.team.id])
        # 登録は書き込みなので担当者であることが要る（閲覧は誰でもできる）
        login_as_manager(self.client, self.team, username="editor")

    def test_register_via_form(self):
        self.client.post(self.url, {"name": "山田", "number": "10", "position": "内野手"})
        self.assertEqual(orm_models.PlayerStint.objects.filter(number=10).count(), 1)

    def test_duplicate_number_is_rejected_via_form(self):
        self.client.post(self.url, {"name": "山田", "number": "10", "position": "内野手"})
        self.client.post(self.url, {"name": "田中", "number": "10", "position": "外野手"})
        self.assertEqual(orm_models.PlayerStint.objects.filter(number=10).count(), 1)

    def test_non_numeric_number_is_rejected_without_crashing(self):
        response = self.client.post(self.url, {"name": "山田", "number": "あいう", "position": "内野手"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(orm_models.Player.objects.count(), 0)

    def test_both_modes_render(self):
        self.assertEqual(self.client.get(f"{self.url}?pos=batter").status_code, 200)
        self.assertEqual(self.client.get(f"{self.url}?pos=pitcher").status_code, 200)

    def test_missing_team_returns_404(self):
        self.assertEqual(self.client.get(reverse("player_list", args=[9999])).status_code, 404)


class PlayerEditViewTest(BaseCase):
    def setUp(self):
        super().setUp()
        login_as_manager(self.client, self.team, username="editor")

    def _url(self, player_id):
        return reverse("player_edit", args=[self.team.id, player_id])

    def test_designated_hitter_keeps_position(self):
        """指名打者を編集しても投手に化けないこと（旧バグの再発防止）。"""
        player = self.service.register_player(self.team.id, "大谷", 17, "指名打者")

        response = self.client.get(self._url(player.id))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<option value="指名打者" selected>', html=False)

    def test_update_basic_information(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")

        self.client.post(
            self._url(player.id),
            {
                "name": "山田太郎",
                "number": "11",
                "position": "外野手",
            },
        )

        detail = self.service.get_player_detail(self.team.id, player.id)
        self.assertEqual(detail.name, "山田太郎")
        self.assertEqual(detail.number, 11)
        self.assertEqual(detail.position, "外野手")

    def test_stats_are_shown_but_not_editable(self):
        """成績は試合の集計結果なので、この画面からは変更できない。"""
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        give_batting(self.team, self.rival, player.id, BattingLine(at_bats=10, singles=3))

        # 打数を送っても無視される
        self.client.post(
            self._url(player.id),
            {
                "name": "山田",
                "number": "10",
                "position": "内野手",
                "at_bats": "999",
            },
        )

        self.assertEqual(self.service.get_player_detail(self.team.id, player.id).at_bats, 10)

    def test_totals_reflect_games(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        give_batting(self.team, self.rival, player.id, BattingLine(at_bats=4, singles=2), day=1)
        give_batting(self.team, self.rival, player.id, BattingLine(at_bats=6, home_runs=1), day=2)

        detail = self.service.get_player_detail(self.team.id, player.id)

        self.assertEqual(detail.at_bats, 10)
        self.assertAlmostEqual(detail.batting_average, 0.3)

    def test_retire_from_the_screen(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")

        self.client.post(self._url(player.id), {"retire": "1"})

        # 退団は在籍期間を閉じることで表す
        stint = orm_models.PlayerStint.objects.get(player_id=player.id)
        self.assertIsNotNone(stint.to_year)

    def test_duplicate_number_on_update_is_rejected(self):
        self.service.register_player(self.team.id, "山田", 10, "内野手")
        tanaka = self.service.register_player(self.team.id, "田中", 11, "外野手")

        self.client.post(
            self._url(tanaka.id),
            {
                "name": "田中",
                "number": "10",
                "position": "外野手",
            },
        )

        self.assertEqual(self.service.get_player_detail(self.team.id, tanaka.id).number, 11)

    def test_missing_player_returns_404(self):
        self.assertEqual(self.client.get(self._url(9999)).status_code, 404)


class PlayerDetailViewTest(BaseCase):
    """選手個人ページ（フェーズ1）。"""

    def setUp(self):
        super().setUp()
        self.player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        play_game(
            self.team,
            self.rival,
            home_score=5,
            away_score=3,
            day=1,
            batting={self.player.id: BattingLine(at_bats=4, singles=2)},
        )
        play_game(
            self.team,
            self.rival,
            home_score=1,
            away_score=4,
            day=2,
            batting={self.player.id: BattingLine(at_bats=6, home_runs=1)},
        )
        self.url = reverse("player_detail", args=[self.team.id, self.player.id])

    def test_page_renders(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "山田")

    def test_shows_career_totals(self):
        profile = self.service.get_player_profile(self.team.id, self.player.id)

        self.assertEqual(profile.detail.at_bats, 10)
        self.assertAlmostEqual(profile.detail.batting_average, 0.3)

    def test_lists_each_game_newest_first(self):
        profile = self.service.get_player_profile(self.team.id, self.player.id)

        self.assertEqual(profile.appearances, 2)
        self.assertEqual([r.played_on.day for r in profile.games], [2, 1])

    def test_batter_rows_have_no_decision(self):
        """個人ページはその選手の働きを見る場所。野手にチームの勝敗は出さない。"""
        profile = self.service.get_player_profile(self.team.id, self.player.id)

        self.assertEqual([r.decision for r in profile.games], ["", ""])

    def test_pitcher_rows_show_the_pitchers_own_decision(self):
        """投手には本人に付いた記録（勝・敗・Ｓ・Ｈ）を、ボックススコアと同じ印で出す。"""
        ace = self.service.register_player(self.team.id, "エース", 18, "投手")
        give_pitching(
            self.team,
            self.rival,
            ace.id,
            PitchingLine(innings=InningsPitched.from_notation("9.0"), wins=1),
            day=3,
        )
        closer = self.service.register_player(self.team.id, "守護神", 22, "投手")
        give_pitching(
            self.team,
            self.rival,
            closer.id,
            PitchingLine(innings=InningsPitched.from_notation("1.0"), saves=1),
            day=4,
        )

        ace_games = self.service.get_player_profile(self.team.id, ace.id).games
        closer_games = self.service.get_player_profile(self.team.id, closer.id).games

        self.assertEqual(ace_games[0].decision, "勝")
        self.assertEqual(closer_games[0].decision, "Ｓ")

    def test_opponent_is_shown_from_the_player_side(self):
        profile = self.service.get_player_profile(self.team.id, self.player.id)
        self.assertEqual(profile.games[0].opponent_name, "相手チーム")

    def test_games_without_the_player_are_excluded(self):
        play_game(self.team, self.rival, day=3)
        profile = self.service.get_player_profile(self.team.id, self.player.id)

        self.assertEqual(profile.appearances, 2)

    def test_player_without_games(self):
        other = self.service.register_player(self.team.id, "控え", 99, "内野手")
        response = self.client.get(reverse("player_detail", args=[self.team.id, other.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "出場した試合がまだありません")

    def test_player_list_links_to_the_profile(self):
        body = self.client.get(reverse("player_list", args=[self.team.id])).content.decode()
        self.assertIn(self.url, body)

    def test_missing_player_returns_404(self):
        self.assertEqual(self.client.get(reverse("player_detail", args=[self.team.id, 9999])).status_code, 404)


class PlayerProfileTest(BaseCase):
    """選手のプロフィール項目。"""

    def test_profile_survives_the_round_trip(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        orm_models.Player.objects.filter(id=player.id).update(
            birth_date="1998-03-15",
            throws="右",
            bats="左",
            height_cm=180,
            weight_kg=78,
            birthplace="大阪府",
            debut_year=2021,
        )

        saved = DjangoTeamRepository().find_by_id(self.team.id).find_player(player.id)

        self.assertEqual(saved.profile.height_cm, 180)
        self.assertEqual(saved.profile.throws_bats, "右投左打")
        self.assertEqual(saved.profile.birthplace, "大阪府")

    def test_profile_is_optional(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        saved = DjangoTeamRepository().find_by_id(self.team.id).find_player(player.id)

        self.assertTrue(saved.profile.is_empty)

    def test_amateur_career_survives_the_round_trip(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        orm_models.Player.objects.filter(id=player.id).update(
            high_school="甲子園高校",
            university="六大学",
            corporate_team="○○重工",
        )

        saved = DjangoTeamRepository().find_by_id(self.team.id).find_player(player.id)

        self.assertEqual(saved.profile.amateur_path, "甲子園高校 → 六大学 → ○○重工")

    def test_name_kana_and_back_name_reach_the_player_page(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        orm_models.Player.objects.filter(id=player.id).update(name_kana="ヤマダタロウ", back_name="T.YAMADA")

        profile = self.service.get_player_profile(self.team.id, player.id)

        self.assertEqual(profile.name_kana, "ヤマダタロウ")
        self.assertEqual(profile.back_name, "T.YAMADA")

    def test_name_kana_and_back_name_appear_on_the_page(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        orm_models.Player.objects.filter(id=player.id).update(name_kana="ヤマダタロウ", back_name="T.YAMADA")

        body = self.client.get(reverse("player_detail", args=[self.team.id, player.id])).content.decode()

        self.assertIn("<rt>ヤマダタロウ</rt>", body)
        self.assertIn("T.YAMADA", body)

    def test_page_without_kana_or_back_name_stays_plain(self):
        """未入力の選手に空のルビや区切り記号を出さない。"""
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")

        body = self.client.get(reverse("player_detail", args=[self.team.id, player.id])).content.decode()

        self.assertNotIn("<ruby>", body)
        self.assertNotIn("back-name", body)

    def test_kana_identical_to_the_name_is_not_shown_as_ruby(self):
        """カタカナ名の選手（外国人など）は名前と読みが同じになるため、ルビを出さない。"""
        player = self.service.register_player(self.team.id, "デイミアン・ベル", 42, "外野手")
        orm_models.Player.objects.filter(id=player.id).update(name_kana="デイミアン・ベル")

        body = self.client.get(reverse("player_detail", args=[self.team.id, player.id])).content.decode()

        self.assertNotIn("<ruby>", body)

    def test_name_kana_and_back_name_survive_an_aggregate_save(self):
        """集約経由の保存で、ドメインが知らない項目として消えないこと。"""
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        orm_models.Player.objects.filter(id=player.id).update(name_kana="ヤマダタロウ", back_name="T.YAMADA")

        self.service.update_player(
            team_id=self.team.id, player_id=player.id, name="山田", number=11, position_label="内野手"
        )

        row = orm_models.Player.objects.get(id=player.id)
        self.assertEqual(row.name_kana, "ヤマダタロウ")
        self.assertEqual(row.back_name, "T.YAMADA")

    def test_nationality_reaches_the_player_page(self):
        """国籍と外国人枠は選手一覧にしか出ていなかった（本人のページに無かった）。"""
        player = self.service.register_player(self.team.id, "デイミアン・ベル", 42, "外野手")
        orm_models.Player.objects.filter(id=player.id).update(nationality="アメリカ合衆国", is_foreign_player=True)

        response = self.client.get(reverse("player_detail", args=[self.team.id, player.id]))

        self.assertContains(response, "国籍")
        self.assertContains(response, "アメリカ合衆国")
        self.assertContains(response, "外国人")

    def test_domestic_player_has_no_nationality_row_or_badge(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")

        response = self.client.get(reverse("player_detail", args=[self.team.id, player.id]))

        self.assertNotContains(response, "国籍")
        self.assertNotContains(response, "外国人")

    def test_captain_badge_appears_on_the_player_page(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        self.service.appoint_captain(self.team.id, player.id)

        response = self.client.get(reverse("player_detail", args=[self.team.id, player.id]))

        self.assertContains(response, "主将")

    def test_player_without_captaincy_has_no_badge(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")

        self.assertNotContains(self.client.get(reverse("player_detail", args=[self.team.id, player.id])), "主将")

    def test_amateur_career_appears_on_the_player_page(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        orm_models.Player.objects.filter(id=player.id).update(high_school="甲子園高校")

        response = self.client.get(reverse("player_detail", args=[self.team.id, player.id]))

        self.assertContains(response, "プロ入り前")
        self.assertContains(response, "甲子園高校")

    def test_player_without_amateur_career(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")

        profile = self.service.get_player_profile(self.team.id, player.id)

        self.assertEqual(profile.amateur_career, [])

    def test_admin_has_the_amateur_career_section(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        self.client.force_login(User.objects.create_superuser(username="am", password="x"))

        response = self.client.get(f"/admin/myapp/player/{player.id}/change/")

        self.assertContains(response, "プロ入り前の経歴")
        for field in ("high_school", "university", "corporate_team"):
            with self.subTest(field=field):
                self.assertContains(response, field)

    def test_admin_edit_page_has_the_profile_section(self):
        player = self.service.register_player(self.team.id, "山田", 10, "内野手")
        self.client.force_login(User.objects.create_superuser(username="root", password="x"))

        response = self.client.get(f"/admin/myapp/player/{player.id}/change/")

        self.assertContains(response, "プロフィール")
        self.assertContains(response, "birth_date")
        self.assertContains(response, "birthplace")


class PlayerSearchTest(BaseCase):
    """選手を名前で探す。所属を知らなくてもたどり着けるようにする。"""

    def setUp(self):
        super().setUp()
        self.yamada = self.service.register_player(self.team.id, "山田太郎", 10, "内野手")
        self.yamamoto = self.service.register_player(self.team.id, "山本次郎", 11, "投手")
        self.tanaka = self.service.register_player(self.rival.id, "田中三郎", 7, "外野手")
        self.url = reverse("player_search")

    def _search(self, keyword):
        return self.client.get(f"{self.url}?q={keyword}").context["results"]

    def test_partial_match(self):
        names = [r.name for r in self._search("山")]
        self.assertEqual(sorted(names), ["山本次郎", "山田太郎"])

    def test_exact_name(self):
        self.assertEqual([r.name for r in self._search("田中三郎")], ["田中三郎"])

    def test_shows_the_current_team_and_league(self):
        row = self._search("田中三郎")[0]

        self.assertEqual(row.team_name, "相手チーム")
        self.assertEqual(row.league_name, "テストリーグ")
        self.assertEqual(row.number, 7)
        self.assertTrue(row.is_active)

    def test_retired_players_are_found(self):
        """退団した選手も探せる。経歴を確認したい場面があるため。"""
        self.service.retire_player(self.team.id, self.yamada.id)

        row = next(r for r in self._search("山田") if r.name == "山田太郎")

        self.assertFalse(row.is_active)
        self.assertEqual(row.team_name, "テストチーム")

    def test_transferred_player_shows_the_current_team(self):
        self.service.transfer_player(
            self.yamada.id,
            from_team_id=self.team.id,
            to_team_id=self.rival.id,
            number=99,
            year=2026,
        )

        row = self._search("山田太郎")[0]

        self.assertEqual(row.team_name, "相手チーム")
        self.assertEqual(row.number, 99)

    def test_no_match(self):
        response = self.client.get(f"{self.url}?q=存在しない名前")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "見つかりませんでした")

    def test_empty_keyword_shows_the_form_only(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["searched"])
        self.assertEqual(response.context["results"], [])

    def test_search_box_is_on_every_page(self):
        for name in ("dashboard", "team_list", "game_list"):
            with self.subTest(page=name):
                self.assertContains(self.client.get(reverse(name)), "app-search")

    def test_results_link_to_the_player_page(self):
        body = self.client.get(f"{self.url}?q=田中").content.decode()
        self.assertIn(reverse("player_detail", args=[self.rival.id, self.tanaka.id]), body)
