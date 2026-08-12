"""試合の画面。一覧・詳細・記録の入力。"""

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from myapp.domain.value_objects import (
    BattingLine,
    InningsPitched,
    PitchingLine,
)
from myapp.infrastructure import orm_models

from ..helpers import (
    api_inning_rows,
    login_as_manager,
    play_game,
    post_game_update,
)
from .base import BaseCase


class GameViewTest(BaseCase):
    """試合一覧・試合詳細（フェーズ1）。"""

    def setUp(self):
        super().setUp()
        self.batter = self.service.register_player(self.team.id, "山田", 10, "内野手")
        self.pitcher = self.service.register_player(self.team.id, "佐藤", 18, "投手")
        self.game = play_game(
            self.team,
            self.rival,
            home_score=5,
            away_score=3,
            day=1,
            batting={self.batter.id: BattingLine(at_bats=4, singles=2, runs_batted_in=1)},
            pitching={
                self.pitcher.id: PitchingLine(innings=InningsPitched.from_notation("7.0"), earned_runs=2, strikeouts=8)
            },
        )

    def test_list_renders(self):
        response = self.client.get(reverse("game_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "テストチーム")
        self.assertContains(response, "テストチーム の勝ち")

    def test_list_is_newest_first(self):
        play_game(self.team, self.rival, day=5)
        rows = self.client.get(reverse("game_list")).context["games"]

        self.assertEqual(rows[0].played_on.day, 5)

    def test_list_can_be_filtered_by_team(self):
        other = orm_models.Team.objects.create(league=self.league, name="第三チーム")
        play_game(other, self.rival, day=9)

        rows = self.client.get(f"{reverse('game_list')}?team={other.id}").context["games"]

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].home_team_name, "第三チーム")

    def test_list_can_be_filtered_by_year(self):
        play_game(self.team, self.rival, year=2025, day=1)
        rows = self.client.get(f"{reverse('game_list')}?year=2025").context["games"]

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].year, 2025)

    def test_invalid_filter_is_ignored(self):
        response = self.client.get(f"{reverse('game_list')}?year=abc&team=xyz")
        self.assertEqual(response.status_code, 200)

    def test_list_can_be_filtered_by_month(self):
        play_game(self.team, self.rival, month=7, day=20)

        rows = self.client.get(f"{reverse('game_list')}?year=2026&month=7").context["games"]

        self.assertEqual([r.played_on.month for r in rows], [7])

    def test_month_choices_come_from_the_games(self):
        play_game(self.team, self.rival, month=7, day=20)

        response = self.client.get(reverse("game_list"))

        self.assertEqual(response.context["months"], [4, 7])

    def test_default_is_the_latest_month_of_the_latest_season(self):
        """全件を一度に描くと重いため、開いた直後は直近の月だけを見せる。"""
        play_game(self.team, self.rival, year=2025, month=9, day=1)
        play_game(self.team, self.rival, month=7, day=20)

        response = self.client.get(reverse("game_list"))

        self.assertEqual(response.context["selected_year"], 2026)
        self.assertEqual(response.context["selected_month"], 7)
        self.assertEqual([r.played_on.month for r in response.context["games"]], [7])

    def test_month_without_games_falls_back_instead_of_erroring(self):
        """年やチームを変えると選んでいた月に試合が無いことがある。エラーにしない。"""
        response = self.client.get(f"{reverse('game_list')}?year=2026&month=12")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_month"], 4)

    def test_list_can_be_filtered_by_league(self):
        other_league = orm_models.League.objects.create(name="別リーグ")
        x = orm_models.Team.objects.create(league=other_league, name="Xチーム")
        y = orm_models.Team.objects.create(league=other_league, name="Yチーム")
        play_game(x, y, day=3)

        rows = self.client.get(f"{reverse('game_list')}?league={other_league.id}").context["games"]

        self.assertEqual([r.home_team_name for r in rows], ["Xチーム"])

    def test_interleague_games_appear_in_both_leagues(self):
        """リーグをまたぐ対戦は、どちらのリーグの日程にも現れる。"""
        other_league = orm_models.League.objects.create(name="別リーグ")
        x = orm_models.Team.objects.create(league=other_league, name="Xチーム")
        play_game(self.team, x, day=6)

        for league in (self.league, other_league):
            with self.subTest(league=league.name):
                rows = self.client.get(f"{reverse('game_list')}?league={league.id}").context["games"]
                self.assertIn(6, [r.played_on.day for r in rows])

    def test_dashboard_game_link_carries_the_league(self):
        """ダッシュボードのリーグタブから移ると、そのリーグの日程が開く。"""
        play_game(self.team, self.rival, day=2)

        body = self.client.get(reverse("dashboard")).content.decode()

        self.assertIn(f"{reverse('game_list')}?league={self.league.id}", body)

    def test_league_tabs_are_shown_with_an_all_option(self):
        """リーグはダッシュボードと同じくタブで切り替える。"""
        other_league = orm_models.League.objects.create(name="別リーグ")

        body = self.client.get(reverse("game_list")).content.decode()

        for league in (self.league, other_league):
            with self.subTest(league=league.name):
                self.assertIn(f"league={league.id}", body)
        self.assertIn("すべて", body)

    def test_league_tab_keeps_the_selected_month(self):
        """リーグを切り替えても、見ている月は保つ。"""
        play_game(self.team, self.rival, month=7, day=20)

        body = self.client.get(f"{reverse('game_list')}?year=2026&month=7").content.decode()

        self.assertIn(f"league={self.league.id}&amp;month=7", body)

    def test_team_choices_follow_the_selected_league(self):
        """チームの選択肢は、選んでいるリーグの所属チームだけにする。"""
        other_league = orm_models.League.objects.create(name="別リーグ")
        orm_models.Team.objects.create(league=other_league, name="Xチーム")

        response = self.client.get(f"{reverse('game_list')}?league={self.league.id}")

        self.assertEqual(
            {t.name for t in response.context["teams"]},
            {"テストチーム", "相手チーム"},
        )

    def test_team_outside_the_league_is_dropped(self):
        """リーグを切り替えると、選んでいたチームがそのリーグにいないことがある。"""
        other_league = orm_models.League.objects.create(name="別リーグ")
        x = orm_models.Team.objects.create(league=other_league, name="Xチーム")

        response = self.client.get(f"{reverse('game_list')}?league={self.league.id}&team={x.id}")

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["selected_team"])

    def test_create_link_ignores_the_league_filter(self):
        """登録の導線は、リーグを絞っていても担当チームがあれば見せる。"""
        other_league = orm_models.League.objects.create(name="別リーグ")
        managed = orm_models.Team.objects.create(league=other_league, name="担当チーム")
        login_as_manager(self.client, managed)

        response = self.client.get(f"{reverse('game_list')}?league={self.league.id}")

        self.assertTrue(response.context["can_create_game"])

    def test_month_choices_follow_the_selected_league(self):
        """リーグを絞ると、そのリーグで試合がある月だけが選択肢になる。"""
        other_league = orm_models.League.objects.create(name="別リーグ")
        x = orm_models.Team.objects.create(league=other_league, name="Xチーム")
        y = orm_models.Team.objects.create(league=other_league, name="Yチーム")
        play_game(x, y, month=8, day=1)

        response = self.client.get(f"{reverse('game_list')}?league={other_league.id}")

        self.assertEqual(response.context["months"], [8])
        self.assertEqual(response.context["selected_month"], 8)

    def test_result_label_is_the_same_from_both_paths(self):
        """結果の文言は GameRow が持つ。参照クエリと集約の経路で食い違わないこと。"""
        from_query = self.client.get(reverse("game_list")).context["games"][0]
        from_aggregate = self.service.get_game_detail(self.game.id).game

        self.assertEqual(from_query.result, "テストチーム の勝ち")
        self.assertEqual(from_query.result, from_aggregate.result)

    def test_tie_is_labelled_the_same_from_both_paths(self):
        tie = play_game(self.team, self.rival, home_score=3, away_score=3, day=8)

        from_query = next(r for r in self.service.list_games().rows if r.id == tie.id)

        self.assertEqual(from_query.result, "引分")
        self.assertEqual(from_query.result, self.service.get_game_detail(tie.id).game.result)

    def test_list_does_not_read_game_details(self):
        """一覧は日付・チーム・スコアしか使わない。明細まで読むと件数ぶん重くなる。

        試合を増やしてもクエリ数が変わらないことで、明細を読んでいないと分かる。
        """
        with CaptureQueriesContext(connection) as first:
            self.client.get(reverse("game_list"))

        for day in range(2, 12):
            play_game(self.team, self.rival, day=day)

        with CaptureQueriesContext(connection) as grown:
            response = self.client.get(reverse("game_list"))

        self.assertEqual(len(response.context["games"]), 11)
        self.assertEqual(len(grown.captured_queries), len(first.captured_queries))

    def test_detail_shows_both_stat_kinds(self):
        response = self.client.get(reverse("game_detail", args=[self.game.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "山田")
        self.assertContains(response, "佐藤")
        self.assertContains(response, "7.0")

    def test_detail_computes_rates_from_the_domain(self):
        detail = self.service.get_game_detail(self.game.id)

        self.assertAlmostEqual(detail.batting[0].batting_average, 0.5)
        # 7回で自責点2 → 2*27/21
        self.assertAlmostEqual(detail.pitching[0].earned_run_average, 2 * 27 / 21)

    def test_missing_game_returns_404(self):
        self.assertEqual(self.client.get(reverse("game_detail", args=[9999])).status_code, 404)


class GameEntryTest(BaseCase):
    """サイトからの試合登録と成績の一括入力（フェーズ3）。"""

    def setUp(self):
        super().setUp()
        self.user = login_as_manager(self.client, self.team, username="scorer")
        self.batter = self.service.register_player(self.team.id, "山田", 10, "内野手")
        self.pitcher = self.service.register_player(self.team.id, "佐藤", 18, "投手")

    def _create_game(self):
        return self.client.post(
            reverse("game_create"),
            {
                "year": "2026",
                "played_on": "2026-04-01",
                "home_team": self.team.id,
                "away_team": self.rival.id,
                "home_score": "5",
                "away_score": "3",
            },
        )

    def test_login_is_required(self):
        self.client.logout()
        response = self.client.get(reverse("game_create"))

        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

    def test_create_game_then_go_to_stats(self):
        response = self._create_game()

        game = orm_models.Game.objects.get()
        self.assertEqual(game.home_score, 5)
        self.assertRedirects(response, reverse("game_edit", args=[game.id]))

    def test_same_team_is_rejected_without_crashing(self):
        response = self.client.post(
            reverse("game_create"),
            {
                "year": "2026",
                "played_on": "2026-04-01",
                "home_team": self.team.id,
                "away_team": self.team.id,
                "home_score": "0",
                "away_score": "0",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(orm_models.Game.objects.count(), 0)

    def test_edit_page_lists_the_roster(self):
        self._create_game()
        game = orm_models.Game.objects.get()

        response = self.client.get(reverse("game_edit", args=[game.id]))

        self.assertEqual(response.status_code, 200)
        payload = response.context["payload"]
        players = [p for team in payload["teams"] for p in team["players"]]
        names = {p["name"] for p in players}
        self.assertIn("山田", names)
        self.assertIn("佐藤", names)
        # 打順・投手の選択肢に使うので、投手かどうかが分かる
        self.assertEqual(len([p for p in players if p["is_pitcher"]]), 1)

    def _stats_payload(self, game, **overrides):
        payload = {
            "year": 2026,
            "played_on": "2026-04-01",
            "home_team": self.team.id,
            "away_team": self.rival.id,
            "home_score": 5,
            "away_score": 3,
            "batting": [{"player_id": self.batter.id}],
            "pitching": [{"player_id": self.pitcher.id}],
            "innings": api_inning_rows(),
        }
        payload.update(overrides)
        return payload

    def test_save_stats_for_the_roster(self):
        self._create_game()
        game = orm_models.Game.objects.get()

        post_game_update(
            self.client,
            game.id,
            self._stats_payload(
                game,
                batting=[{"player_id": self.batter.id, "at_bats": 4, "singles": 2}],
                pitching=[{"player_id": self.pitcher.id, "innings_pitched": "7.0", "strikeouts": 8}],
            ),
        )

        detail = self.service.get_player_detail(self.team.id, self.batter.id)
        self.assertEqual(detail.at_bats, 4)
        pitcher = self.service.get_player_detail(self.team.id, self.pitcher.id)
        self.assertEqual(pitcher.strikeouts, 8)
        self.assertEqual(pitcher.innings_pitched, "7.0")

    def test_blank_rows_are_not_recorded(self):
        """出場しなかった選手の行を残さない。"""
        self._create_game()
        game = orm_models.Game.objects.get()

        post_game_update(self.client, game.id, self._stats_payload(game))

        self.assertEqual(orm_models.GameBattingLine.objects.count(), 0)
        self.assertEqual(orm_models.GamePitchingLine.objects.count(), 0)

    def test_clearing_a_row_removes_the_record(self):
        """一度入力した選手を「出場していない」に戻せること。"""
        self._create_game()
        game = orm_models.Game.objects.get()

        post_game_update(
            self.client,
            game.id,
            self._stats_payload(game, batting=[{"player_id": self.batter.id, "at_bats": 4}]),
        )
        self.assertEqual(orm_models.GameBattingLine.objects.count(), 1)

        post_game_update(self.client, game.id, self._stats_payload(game))
        self.assertEqual(orm_models.GameBattingLine.objects.count(), 0)

    def test_the_lineup_is_prefilled(self):
        """入力済みの打順は開き直したときに残っていること。

        成績そのものは payload に載せない（打席から導く値なので、編集画面が
        持つのは打順と打席だけ）。
        """
        self._create_game()
        game = orm_models.Game.objects.get()

        post_game_update(
            self.client,
            game.id,
            self._stats_payload(game, batting=[{"player_id": self.batter.id, "at_bats": 4, "batting_order": 3}]),
        )

        payload = self.client.get(reverse("game_edit", args=[game.id])).context["payload"]
        slots = [slot for team in payload["teams"] for slot in team["lineup"]]
        slot = next(each for each in slots if each["player_id"] == self.batter.id)
        self.assertEqual(slot["batting_order"], 3)

    def test_score_can_be_corrected(self):
        self._create_game()
        game = orm_models.Game.objects.get()

        post_game_update(self.client, game.id, self._stats_payload(game, home_score=9))

        self.assertEqual(orm_models.Game.objects.get().home_score, 9)

    def test_missing_game_returns_404(self):
        self.assertEqual(self.client.get(reverse("game_edit", args=[9999])).status_code, 404)


class BoxScoreEntryTest(BaseCase):
    """手動入力でもボックススコアとNPBの記録が揃うこと。

    イニングスコアと継投を入れれば、勝敗・セーブ・ホールドは規則で決まる。
    投手の欄に勝敗を入力させないのは、規則から一意に決まるものを人が入れると
    記録どうしが食い違うため。
    """

    def setUp(self):
        super().setUp()
        login_as_manager(self.client, self.team, self.rival)
        self.starter = self.service.register_player(self.team.id, "先発", 11, "投手")
        self.middle = self.service.register_player(self.team.id, "中継ぎ", 12, "投手")
        self.closer = self.service.register_player(self.team.id, "抑え", 13, "投手")
        self.batter = self.service.register_player(self.team.id, "4番", 3, "内野手")
        self.loser = self.service.register_player(self.rival.id, "相手先発", 21, "投手")
        self.game = play_game(self.team, self.rival, home_score=0, away_score=0)
        self.url = reverse("game_edit", args=[self.game.id])

    def _payload(self, **overrides):
        payload = {
            "year": 2026,
            "played_on": "2026-04-01",
            "home_team": self.team.id,
            "away_team": self.rival.id,
            # ホーム2点・ビジター0点。抑えは1点差以内ではないが3点差以内で締める
            "home_score": 2,
            "away_score": 0,
            "batting": [
                {
                    "player_id": self.batter.id,
                    "at_bats": 4,
                    "singles": 2,
                    "runs_batted_in": 2,
                    "batting_order": 4,
                    "slot_sequence": 0,
                    "fielding_position": "一",
                }
            ],
            # ホームは 先発6回 → 中継ぎ2回 → 抑え1回、ビジターは先発が完投
            "pitching": [
                {"player_id": self.starter.id, "entered_inning": 1, "innings_pitched": "6.0"},
                {"player_id": self.middle.id, "entered_inning": 7, "innings_pitched": "2.0"},
                {"player_id": self.closer.id, "entered_inning": 9, "innings_pitched": "1.0"},
                {"player_id": self.loser.id, "entered_inning": 1, "innings_pitched": "9.0"},
            ],
            "innings": api_inning_rows(away=[0] * 9, home=[2] + [0] * 8),
        }
        payload.update(overrides)
        return payload

    def _line_of(self, player):
        return orm_models.GamePitchingLine.objects.get(game_id=self.game.id, player_id=player.id)

    def test_decisions_are_derived_from_the_line_score(self):
        post_game_update(self.client, self.game.id, self._payload())

        self.assertEqual(self._line_of(self.starter).wins, 1)
        self.assertEqual(self._line_of(self.loser).losses, 1)
        self.assertEqual(self._line_of(self.closer).saves, 1)
        self.assertEqual(self._line_of(self.middle).holds, 1)

    def test_the_winner_does_not_also_get_a_save(self):
        post_game_update(self.client, self.game.id, self._payload())

        self.assertEqual(self._line_of(self.starter).saves, 0)
        self.assertEqual(self._line_of(self.closer).wins, 0)

    def test_a_large_lead_yields_no_save(self):
        """5点差で登板した抑えにはセーブが付かない（1回だけの登板では）。"""
        post_game_update(
            self.client,
            self.game.id,
            self._payload(
                home_score=5,
                innings=api_inning_rows(away=[0] * 9, home=[5] + [0] * 8),
            ),
        )

        self.assertEqual(self._line_of(self.closer).saves, 0)
        self.assertEqual(self._line_of(self.starter).wins, 1)

    def test_the_line_score_must_match_the_final_score(self):
        response = post_game_update(self.client, self.game.id, self._payload(home_score=7))

        self.assertEqual(response.status_code, 400)
        self.assertIn("イニングスコアの合計が得点と一致しません", response.json()["error"])

    def test_lineup_is_saved_and_shown_in_the_box_score(self):
        post_game_update(self.client, self.game.id, self._payload())

        line = orm_models.GameBattingLine.objects.get(game_id=self.game.id, player_id=self.batter.id)
        self.assertEqual(line.batting_order, 4)
        self.assertEqual(line.fielding_position, "一")

        response = self.client.get(reverse("game_detail", args=[self.game.id]))
        self.assertContains(response, "打順")
        self.assertContains(response, "一")

    def test_appearance_order_follows_the_entered_inning(self):
        post_game_update(self.client, self.game.id, self._payload())

        orders = {
            self._line_of(p).player_id: self._line_of(p).appearance_order
            for p in (self.starter, self.middle, self.closer)
        }
        self.assertEqual(orders[self.starter.id], 1)
        self.assertEqual(orders[self.middle.id], 2)
        self.assertEqual(orders[self.closer.id], 3)

    def test_hold_points_accumulate_for_the_reliever(self):
        post_game_update(self.client, self.game.id, self._payload())

        detail = self.service.get_player_detail(self.team.id, self.middle.id)
        self.assertEqual(detail.holds, 1)
        self.assertEqual(detail.hold_points, 1)

    def test_line_score_is_shown_on_the_detail_page(self):
        post_game_update(self.client, self.game.id, self._payload())

        response = self.client.get(reverse("game_detail", args=[self.game.id]))

        self.assertContains(response, "linescore-table")
        self.assertEqual(len(response.context["detail"].line_score.columns), 9)

    def test_the_edit_form_has_no_stat_inputs_at_all(self):
        """成績は打席から導くので、入力欄の材料を payload に載せない。

        勝敗・セーブだけでなく、打数も投球回も登板順も送らない。載せると
        「打席と食い違う成績」を入力できる余地が残る。
        """
        response = self.client.get(self.url)

        payload = response.context["payload"]
        players = [p for team in payload["teams"] for p in team["players"]]
        self.assertTrue(players)
        for player in players:
            self.assertEqual(set(player), {"id", "name", "number", "position", "is_pitcher"})
        # 代わりに打席の語彙が載る（画面側に同じ表を持たせないため）
        self.assertIn("results", payload["vocabulary"])
