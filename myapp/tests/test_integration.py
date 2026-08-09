"""結合テスト。リポジトリの永続化と画面の動作を確認する。

成績は試合の記録から集計されるため、成績を持たせたい場合は
helpers の play_game / give_batting / give_pitching で試合を作る。
"""

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from myapp.domain.exceptions import DuplicateJerseyNumber, InvalidGame
from myapp.domain.value_objects import (
    BattingLine,
    InningsPitched,
    JerseyNumber,
    PitchingLine,
    Position,
)
from myapp.infrastructure import orm_models
from myapp.infrastructure.repositories import DjangoGameRepository, DjangoTeamRepository

from .helpers import build_service, give_batting, give_pitching, play_game


class BaseCase(TestCase):
    def setUp(self):
        self.league = orm_models.League.objects.create(name='テストリーグ')
        self.team = orm_models.Team.objects.create(
            league=self.league, name='テストチーム', city='東京'
        )
        self.rival = orm_models.Team.objects.create(league=self.league, name='相手チーム')
        self.service = build_service()


class RepositoryRoundTripTest(BaseCase):
    """ORM ⇄ ドメインの往復でデータが失われないこと。"""

    def setUp(self):
        super().setUp()
        self.repo = DjangoTeamRepository()

    def test_save_and_reload_a_player(self):
        team = self.repo.find_by_id(self.team.id)
        player = team.add_player('山田', JerseyNumber(10), Position.INFIELDER)
        self.repo.save(team)

        saved = self.repo.find_by_id(self.team.id).find_player(player.id)

        self.assertEqual(saved.name, '山田')
        self.assertEqual(saved.number.value, 10)
        self.assertEqual(saved.position, Position.INFIELDER)

    def test_batting_totals_come_from_games(self):
        player = self.service.register_player(self.team.id, '山田', 10, '内野手')
        give_batting(self.team, self.rival, player.id, BattingLine(at_bats=4, singles=2), day=1)
        give_batting(self.team, self.rival, player.id, BattingLine(at_bats=3, home_runs=1), day=2)

        saved = self.repo.find_by_id(self.team.id).find_player(player.id)

        self.assertEqual(saved.batting.at_bats, 7)
        self.assertEqual(saved.batting.hits, 3)

    def test_innings_are_added_as_outs_not_decimals(self):
        """5.2 + 5.2 は 10.4 ではなく 11.1。"""
        player = self.service.register_player(self.team.id, '佐藤', 18, '投手')
        line = PitchingLine(innings=InningsPitched.from_notation('5.2'), earned_runs=1)
        give_pitching(self.team, self.rival, player.id, line, day=1)
        give_pitching(self.team, self.rival, player.id, line, day=2)

        saved = self.repo.find_by_id(self.team.id).find_player(player.id)

        self.assertEqual(saved.pitching.innings.outs, 34)
        self.assertEqual(str(saved.pitching.innings), '11.1')
        self.assertEqual(saved.pitching.earned_runs, 2)

    def test_player_without_games_has_empty_stats(self):
        player = self.service.register_player(self.team.id, '山田', 10, '内野手')
        saved = self.repo.find_by_id(self.team.id).find_player(player.id)

        self.assertEqual(saved.batting.at_bats, 0)
        self.assertEqual(saved.pitching.innings.outs, 0)

    def test_duplicate_number_is_rejected_on_the_aggregate(self):
        team = self.repo.find_by_id(self.team.id)
        team.add_player('山田', JerseyNumber(10), Position.INFIELDER)
        self.repo.save(team)

        reloaded = self.repo.find_by_id(self.team.id)
        with self.assertRaises(DuplicateJerseyNumber):
            reloaded.add_player('田中', JerseyNumber(10), Position.OUTFIELDER)


class GameRepositoryTest(BaseCase):
    def test_round_trip(self):
        player = self.service.register_player(self.team.id, '山田', 10, '内野手')
        saved = play_game(
            self.team, self.rival, home_score=5, away_score=3,
            batting={player.id: BattingLine(at_bats=4, singles=2, runs_batted_in=1)},
        )

        reloaded = DjangoGameRepository().find_by_id(saved.id)

        self.assertEqual(reloaded.home_score, 5)
        self.assertEqual(reloaded.result_for(self.team.id), 'win')
        self.assertEqual(len(reloaded.batting), 1)
        self.assertEqual(reloaded.batting[0].line.hits, 2)

    def test_same_team_is_rejected(self):
        with self.assertRaises(InvalidGame):
            play_game(self.team, self.team)

    def test_filter_by_season(self):
        play_game(self.team, self.rival, year=2025, day=1)
        play_game(self.team, self.rival, year=2026, day=1)

        self.assertEqual(len(DjangoGameRepository().find_all(2026)), 1)
        self.assertEqual(len(DjangoGameRepository().find_all()), 2)

    def test_recording_the_same_player_twice_overwrites(self):
        player = self.service.register_player(self.team.id, '山田', 10, '内野手')
        game = play_game(
            self.team, self.rival, batting={player.id: BattingLine(at_bats=4, singles=1)}
        )
        game.record_batting(player.id, BattingLine(at_bats=4, home_runs=2))
        DjangoGameRepository().save(game)

        self.assertEqual(
            orm_models.GameBattingLine.objects.filter(game_id=game.id).count(), 1
        )
        reloaded = DjangoGameRepository().find_by_id(game.id)
        self.assertEqual(reloaded.batting[0].line.home_runs, 2)


class ApplicationServiceTest(BaseCase):
    def test_register_player(self):
        self.service.register_player(self.team.id, '山田', 10, '内野手')

        rows = self.service.list_batters(self.team.id).rows
        self.assertEqual([r.name for r in rows], ['山田'])

    def test_register_duplicate_number_is_rejected(self):
        self.service.register_player(self.team.id, '山田', 10, '内野手')
        with self.assertRaises(DuplicateJerseyNumber):
            self.service.register_player(self.team.id, '田中', 10, '外野手')

    def test_retire_player_frees_the_number(self):
        player = self.service.register_player(self.team.id, '山田', 10, '内野手')
        self.service.retire_player(self.team.id, player.id)

        # 退団後は同じ背番号を使える
        self.service.register_player(self.team.id, '田中', 10, '外野手')
        self.assertEqual(len(self.service.list_batters(self.team.id).rows), 1)

    def test_team_summary_counts_active_players(self):
        self.service.register_player(self.team.id, '山田', 10, '内野手')
        self.service.register_player(self.team.id, '佐藤', 18, '投手')

        summary = self.service.list_teams().rows[0]
        self.assertEqual(summary.league_name, 'テストリーグ')
        self.assertEqual(summary.player_count, 2)


class PlayerListViewTest(BaseCase):
    def setUp(self):
        super().setUp()
        self.url = reverse('player_list', args=[self.team.id])

    def test_register_via_form(self):
        self.client.post(self.url, {'name': '山田', 'number': '10', 'position': '内野手'})
        self.assertEqual(orm_models.Player.objects.filter(number=10).count(), 1)

    def test_duplicate_number_is_rejected_via_form(self):
        self.client.post(self.url, {'name': '山田', 'number': '10', 'position': '内野手'})
        self.client.post(self.url, {'name': '田中', 'number': '10', 'position': '外野手'})
        self.assertEqual(orm_models.Player.objects.filter(number=10).count(), 1)

    def test_non_numeric_number_is_rejected_without_crashing(self):
        response = self.client.post(
            self.url, {'name': '山田', 'number': 'あいう', 'position': '内野手'}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(orm_models.Player.objects.count(), 0)

    def test_both_modes_render(self):
        self.assertEqual(self.client.get(f'{self.url}?pos=batter').status_code, 200)
        self.assertEqual(self.client.get(f'{self.url}?pos=pitcher').status_code, 200)

    def test_missing_team_returns_404(self):
        self.assertEqual(
            self.client.get(reverse('player_list', args=[9999])).status_code, 404
        )


class PlayerEditViewTest(BaseCase):
    def _url(self, player_id):
        return reverse('player_edit', args=[self.team.id, player_id])

    def test_designated_hitter_keeps_position(self):
        """指名打者を編集しても投手に化けないこと（旧バグの再発防止）。"""
        player = self.service.register_player(self.team.id, '大谷', 17, '指名打者')

        response = self.client.get(self._url(player.id))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<option value="指名打者" selected>', html=False)

    def test_update_basic_information(self):
        player = self.service.register_player(self.team.id, '山田', 10, '内野手')

        self.client.post(self._url(player.id), {
            'name': '山田太郎', 'number': '11', 'position': '外野手',
        })

        detail = self.service.get_player_detail(self.team.id, player.id)
        self.assertEqual(detail.name, '山田太郎')
        self.assertEqual(detail.number, 11)
        self.assertEqual(detail.position, '外野手')

    def test_stats_are_shown_but_not_editable(self):
        """成績は試合の集計結果なので、この画面からは変更できない。"""
        player = self.service.register_player(self.team.id, '山田', 10, '内野手')
        give_batting(self.team, self.rival, player.id, BattingLine(at_bats=10, singles=3))

        # 打数を送っても無視される
        self.client.post(self._url(player.id), {
            'name': '山田', 'number': '10', 'position': '内野手', 'at_bats': '999',
        })

        self.assertEqual(self.service.get_player_detail(self.team.id, player.id).at_bats, 10)

    def test_totals_reflect_games(self):
        player = self.service.register_player(self.team.id, '山田', 10, '内野手')
        give_batting(self.team, self.rival, player.id, BattingLine(at_bats=4, singles=2), day=1)
        give_batting(self.team, self.rival, player.id, BattingLine(at_bats=6, home_runs=1), day=2)

        detail = self.service.get_player_detail(self.team.id, player.id)

        self.assertEqual(detail.at_bats, 10)
        self.assertAlmostEqual(detail.batting_average, 0.3)

    def test_retire_from_the_screen(self):
        player = self.service.register_player(self.team.id, '山田', 10, '内野手')

        self.client.post(self._url(player.id), {'retire': '1'})

        self.assertFalse(orm_models.Player.objects.get(id=player.id).is_active)

    def test_duplicate_number_on_update_is_rejected(self):
        self.service.register_player(self.team.id, '山田', 10, '内野手')
        tanaka = self.service.register_player(self.team.id, '田中', 11, '外野手')

        self.client.post(self._url(tanaka.id), {
            'name': '田中', 'number': '10', 'position': '外野手',
        })

        self.assertEqual(
            self.service.get_player_detail(self.team.id, tanaka.id).number, 11
        )

    def test_missing_player_returns_404(self):
        self.assertEqual(self.client.get(self._url(9999)).status_code, 404)


class DashboardTest(BaseCase):
    def test_counts(self):
        self.service.register_player(self.team.id, '山田', 10, '内野手')
        self.service.register_player(self.team.id, '佐藤', 18, '投手')

        board = self.service.get_dashboard()

        self.assertEqual(board.team_count, 2)
        self.assertEqual(board.batter_count, 1)
        self.assertEqual(board.pitcher_count, 1)

    def test_ranking_spans_all_teams(self):
        a = self.service.register_player(self.team.id, '山田', 10, '内野手')
        b = self.service.register_player(self.rival.id, '田中', 10, '外野手')
        play_game(
            self.team, self.rival,
            batting={
                a.id: BattingLine(at_bats=10, singles=1),
                b.id: BattingLine(at_bats=10, home_runs=4),
            },
        )

        board = self.service.get_dashboard()

        self.assertEqual([e.player_name for e in board.ops_leaders], ['田中', '山田'])
        self.assertEqual(board.ops_leaders[0].team_name, '相手チーム')

    def test_page_renders(self):
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'テストチーム')

    def test_page_renders_without_any_data(self):
        orm_models.Team.objects.all().delete()
        self.assertEqual(self.client.get(reverse('dashboard')).status_code, 200)


class StandingsTest(BaseCase):
    def test_record_is_aggregated_from_games(self):
        play_game(self.team, self.rival, home_score=5, away_score=3, day=1)
        play_game(self.team, self.rival, home_score=2, away_score=2, day=2)
        play_game(self.team, self.rival, home_score=1, away_score=4, day=3)

        rows = {r.team_name: r for r in self.service.get_standings(2026).rows}

        self.assertEqual(rows['テストチーム'].wins, 1)
        self.assertEqual(rows['テストチーム'].losses, 1)
        self.assertEqual(rows['テストチーム'].ties, 1)
        self.assertEqual(rows['テストチーム'].games_played, 3)

    def test_rank_is_derived_from_winning_percentage(self):
        play_game(self.team, self.rival, home_score=5, away_score=3, day=1)
        play_game(self.team, self.rival, home_score=6, away_score=2, day=2)
        play_game(self.team, self.rival, home_score=1, away_score=4, day=3)

        rows = self.service.get_standings(2026).rows

        self.assertEqual([r.team_name for r in rows], ['テストチーム', '相手チーム'])
        self.assertEqual([r.rank for r in rows], [1, 2])
        self.assertEqual(rows[0].games_behind, '—')

    def test_teams_without_games_are_excluded(self):
        other = orm_models.Team.objects.create(league=self.league, name='未実施チーム')
        play_game(self.team, self.rival)

        names = [r.team_name for r in self.service.get_standings(2026).rows]

        self.assertNotIn(other.name, names)

    def test_defaults_to_the_latest_season(self):
        play_game(self.team, self.rival, year=2025, day=1)
        play_game(self.team, self.rival, year=2026, day=1)

        board = self.service.get_standings()

        self.assertEqual(board.year, 2026)
        self.assertEqual(board.available_years, [2026, 2025])

    def test_page_renders(self):
        play_game(self.team, self.rival, home_score=5, away_score=3)
        response = self.client.get(reverse('standings'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'テストチーム')

    def test_page_by_year(self):
        play_game(self.team, self.rival, year=2025, day=1)
        play_game(self.team, self.rival, year=2026, day=1)

        response = self.client.get(reverse('standings_by_year', args=[2025]))
        self.assertContains(response, '2025年')

    def test_page_without_any_game(self):
        response = self.client.get(reverse('standings'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '試合がまだ登録されていません')


class SortingViewTest(BaseCase):
    def setUp(self):
        super().setUp()
        a = self.service.register_player(self.team.id, '少打', 1, '内野手')
        b = self.service.register_player(self.team.id, '多打', 2, '外野手')
        play_game(
            self.team, self.rival,
            batting={
                a.id: BattingLine(at_bats=20, singles=4, home_runs=1),
                b.id: BattingLine(at_bats=20, singles=2, home_runs=5),
            },
        )
        self.url = reverse('player_list', args=[self.team.id])

    def _names(self, query=''):
        listing = self.client.get(f'{self.url}{query}').context['listing']
        return [r.name for r in listing.rows]

    def test_default_order_is_ops(self):
        self.assertEqual(self._names(), ['多打', '少打'])

    def test_sort_by_home_runs_ascending(self):
        self.assertEqual(self._names('?sort=home_runs&dir=asc'), ['少打', '多打'])

    def test_sort_by_home_runs_descending(self):
        self.assertEqual(self._names('?sort=home_runs&dir=desc'), ['多打', '少打'])

    def test_invalid_sort_key_does_not_break_the_page(self):
        response = self.client.get(f'{self.url}?sort=../../etc/passwd')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['listing'].sort, 'ops')

    def test_sort_link_keeps_other_query_params(self):
        body = self.client.get(f'{self.url}?pos=pitcher').content.decode()
        self.assertIn('pos=pitcher', body)
        self.assertIn('sort=era', body)

    def test_header_shows_the_active_direction(self):
        body = self.client.get(f'{self.url}?sort=home_runs&dir=desc').content.decode()
        self.assertIn('sort-link is-active', body)

    def test_team_list_can_be_sorted(self):
        response = self.client.get(f"{reverse('team_list')}?sort=name&dir=asc")
        names = [t.name for t in response.context['teams']]
        self.assertEqual(names, sorted(names))

    def test_team_list_defaults_to_manual_order(self):
        """名前順ではなく、管理画面で設定した表示順が既定になること。"""
        orm_models.Team.objects.update(display_order=5)
        orm_models.Team.objects.create(
            league=self.league, name='Zチーム', display_order=1
        )
        response = self.client.get(reverse('team_list'))

        # 名前順なら最後に来るはずの Z が、表示順1なので先頭に出る
        self.assertEqual(response.context['teams'][0].name, 'Zチーム')
        self.assertEqual(response.context['current_sort'], 'order')

    def test_standings_can_be_sorted(self):
        response = self.client.get(f"{reverse('standings')}?sort=wins&dir=desc")
        self.assertEqual(response.context['standings'].sort, 'wins')


class TeamOrderingTest(BaseCase):
    def setUp(self):
        super().setUp()
        orm_models.Team.objects.filter(id=self.team.id).update(display_order=2, name='Aチーム')
        orm_models.Team.objects.filter(id=self.rival.id).update(display_order=1, name='Bチーム')

    def test_display_order_beats_name(self):
        names = [t.name for t in self.service.list_teams().rows]
        self.assertEqual(names, ['Bチーム', 'Aチーム'])

    def test_dashboard_uses_the_same_order(self):
        names = [t.name for t in self.service.get_dashboard().teams]
        self.assertEqual(names, ['Bチーム', 'Aチーム'])

    def test_same_order_falls_back_to_name(self):
        orm_models.Team.objects.update(display_order=0)
        names = [t.name for t in self.service.list_teams().rows]
        self.assertEqual(names, ['Aチーム', 'Bチーム'])

    def _league_page(self):
        self.client.force_login(User.objects.create_superuser(username='root', password='x'))
        return self.client.get(f'/admin/myapp/league/{self.league.id}/change/')

    def test_admin_league_page_loads_the_sortable_script(self):
        self.assertContains(self._league_page(), 'admin-inline-sortable.js')

    def test_order_field_is_submitted_but_not_shown(self):
        body = self._league_page().content.decode()

        self.assertIn('type="hidden" name="teams-0-display_order"', body)
        self.assertIn('class="column-display_order required hidden"', body)
        self.assertNotIn('class="vIntegerField"', body)


class HeaderNavigationTest(TestCase):
    """ヘッダーの導線が権限に応じて出し分けられること。"""

    ADMIN_LINK = 'class="nav-admin-link"'

    def setUp(self):
        self.staff = User.objects.create_user(username='staff', password='x', is_staff=True)
        self.member = User.objects.create_user(username='member', password='x')

    def test_staff_sees_admin_link(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse('dashboard'))
        self.assertContains(response, self.ADMIN_LINK)

    def test_normal_user_does_not_see_admin_link(self):
        self.client.force_login(self.member)
        self.assertNotContains(self.client.get(reverse('dashboard')), self.ADMIN_LINK)

    def test_anonymous_does_not_see_admin_link(self):
        response = self.client.get(reverse('dashboard'))
        self.assertNotContains(response, self.ADMIN_LINK)
        self.assertContains(response, 'ログイン')

    def test_admin_page_actually_rejects_normal_user(self):
        self.client.force_login(self.member)
        response = self.client.get('/admin/')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/admin/login/', response['Location'])

    def test_admin_page_accepts_staff(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get('/admin/').status_code, 200)


class AdminTest(BaseCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser(username='root', password='x'))

    def test_admin_pages_use_the_admin_theme(self):
        for url in ['/admin/', '/admin/myapp/player/', '/admin/myapp/game/']:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertContains(response, 'myapp/css/admin-theme.css')
                self.assertNotContains(response, 'myapp/css/theme.css')

    def test_site_pages_do_not_use_the_admin_theme(self):
        response = self.client.get(reverse('dashboard'))
        self.assertContains(response, 'myapp/css/theme.css')
        self.assertNotContains(response, 'myapp/css/admin-theme.css')

    def test_game_list_shows_the_result(self):
        play_game(self.team, self.rival, home_score=5, away_score=3)
        response = self.client.get('/admin/myapp/game/')
        self.assertContains(response, 'テストチーム の勝ち')

    def test_game_edit_has_stat_inlines(self):
        game = play_game(self.team, self.rival)
        response = self.client.get(f'/admin/myapp/game/{game.id}/change/')
        self.assertContains(response, '打撃成績')
        self.assertContains(response, '投球成績')

    def test_player_list_shows_appearances(self):
        player = self.service.register_player(self.team.id, '山田', 10, '内野手')
        give_batting(self.team, self.rival, player.id, BattingLine(at_bats=4, singles=1))

        response = self.client.get('/admin/myapp/player/')
        self.assertContains(response, 'field-appearances')


class AdminIndexTest(BaseCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser(username='root', password='x'))

    def test_models_are_labelled_in_japanese(self):
        response = self.client.get('/admin/')
        for label in ['野球データ', 'リーグ', 'チーム', '選手', '試合']:
            with self.subTest(label=label):
                self.assertContains(response, label)

    def test_models_follow_domain_order(self):
        body = self.client.get('/admin/').content.decode()
        positions = [
            body.index('/admin/myapp/league/'),
            body.index('/admin/myapp/team/'),
            body.index('/admin/myapp/player/'),
            body.index('/admin/myapp/game/'),
        ]
        self.assertEqual(positions, sorted(positions))

    def test_baseball_data_comes_before_auth(self):
        body = self.client.get('/admin/').content.decode()
        self.assertLess(body.index('/admin/myapp/'), body.index('/admin/auth/'))

    def test_overview_counts(self):
        self.service.register_player(self.team.id, '山田', 10, '内野手')
        self.service.register_player(self.team.id, '佐藤', 18, '投手')

        overview = self.service.get_admin_overview()

        self.assertEqual(overview.team_count, 2)
        self.assertEqual(overview.player_count, 2)
        self.assertEqual(overview.pitcher_count, 1)

    def test_overview_flags_players_without_stats(self):
        player = self.service.register_player(self.team.id, '山田', 10, '内野手')
        self.assertEqual(self.service.get_admin_overview().players_without_stats, 1)

        give_batting(self.team, self.rival, player.id, BattingLine(at_bats=10, singles=3))
        self.assertEqual(self.service.get_admin_overview().players_without_stats, 0)

    def test_overview_flags_empty_teams_and_retired_players(self):
        player = self.service.register_player(self.team.id, '山田', 10, '内野手')
        orm_models.Player.objects.filter(id=player.id).update(is_active=False)

        overview = self.service.get_admin_overview()

        self.assertEqual(overview.teams_without_players, 2)
        self.assertEqual(overview.retired_count, 1)

    def test_notes_appear_on_the_page(self):
        self.service.register_player(self.team.id, '山田', 10, '内野手')
        self.assertContains(self.client.get('/admin/'), '成績が未入力の選手')


class GameViewTest(BaseCase):
    """試合一覧・試合詳細（フェーズ1）。"""

    def setUp(self):
        super().setUp()
        self.batter = self.service.register_player(self.team.id, '山田', 10, '内野手')
        self.pitcher = self.service.register_player(self.team.id, '佐藤', 18, '投手')
        self.game = play_game(
            self.team, self.rival, home_score=5, away_score=3, day=1,
            batting={self.batter.id: BattingLine(at_bats=4, singles=2, runs_batted_in=1)},
            pitching={self.pitcher.id: PitchingLine(
                innings=InningsPitched.from_notation('7.0'), earned_runs=2, strikeouts=8
            )},
        )

    def test_list_renders(self):
        response = self.client.get(reverse('game_list'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'テストチーム')
        self.assertContains(response, 'テストチーム の勝ち')

    def test_list_is_newest_first(self):
        play_game(self.team, self.rival, day=5)
        rows = self.client.get(reverse('game_list')).context['games']

        self.assertEqual(rows[0].played_on.day, 5)

    def test_list_can_be_filtered_by_team(self):
        other = orm_models.Team.objects.create(league=self.league, name='第三チーム')
        play_game(other, self.rival, day=9)

        rows = self.client.get(f"{reverse('game_list')}?team={other.id}").context['games']

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].home_team_name, '第三チーム')

    def test_list_can_be_filtered_by_year(self):
        play_game(self.team, self.rival, year=2025, day=1)
        rows = self.client.get(f"{reverse('game_list')}?year=2025").context['games']

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].year, 2025)

    def test_invalid_filter_is_ignored(self):
        response = self.client.get(f"{reverse('game_list')}?year=abc&team=xyz")
        self.assertEqual(response.status_code, 200)

    def test_detail_shows_both_stat_kinds(self):
        response = self.client.get(reverse('game_detail', args=[self.game.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '山田')
        self.assertContains(response, '佐藤')
        self.assertContains(response, '7.0')

    def test_detail_computes_rates_from_the_domain(self):
        detail = self.service.get_game_detail(self.game.id)

        self.assertAlmostEqual(detail.batting[0].batting_average, 0.5)
        # 7回で自責点2 → 2*27/21
        self.assertAlmostEqual(detail.pitching[0].earned_run_average, 2 * 27 / 21)

    def test_missing_game_returns_404(self):
        self.assertEqual(
            self.client.get(reverse('game_detail', args=[9999])).status_code, 404
        )


class PlayerDetailViewTest(BaseCase):
    """選手個人ページ（フェーズ1）。"""

    def setUp(self):
        super().setUp()
        self.player = self.service.register_player(self.team.id, '山田', 10, '内野手')
        play_game(
            self.team, self.rival, home_score=5, away_score=3, day=1,
            batting={self.player.id: BattingLine(at_bats=4, singles=2)},
        )
        play_game(
            self.team, self.rival, home_score=1, away_score=4, day=2,
            batting={self.player.id: BattingLine(at_bats=6, home_runs=1)},
        )
        self.url = reverse('player_detail', args=[self.team.id, self.player.id])

    def test_page_renders(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '山田')

    def test_shows_career_totals(self):
        profile = self.service.get_player_profile(self.team.id, self.player.id)

        self.assertEqual(profile.detail.at_bats, 10)
        self.assertAlmostEqual(profile.detail.batting_average, 0.3)

    def test_lists_each_game_newest_first(self):
        profile = self.service.get_player_profile(self.team.id, self.player.id)

        self.assertEqual(profile.appearances, 2)
        self.assertEqual([r.played_on.day for r in profile.games], [2, 1])
        self.assertEqual([r.result for r in profile.games], ['敗', '勝'])

    def test_opponent_is_shown_from_the_player_side(self):
        profile = self.service.get_player_profile(self.team.id, self.player.id)
        self.assertEqual(profile.games[0].opponent_name, '相手チーム')

    def test_games_without_the_player_are_excluded(self):
        play_game(self.team, self.rival, day=3)
        profile = self.service.get_player_profile(self.team.id, self.player.id)

        self.assertEqual(profile.appearances, 2)

    def test_player_without_games(self):
        other = self.service.register_player(self.team.id, '控え', 99, '内野手')
        response = self.client.get(reverse('player_detail', args=[self.team.id, other.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '出場した試合がまだありません')

    def test_player_list_links_to_the_profile(self):
        body = self.client.get(reverse('player_list', args=[self.team.id])).content.decode()
        self.assertIn(self.url, body)

    def test_missing_player_returns_404(self):
        self.assertEqual(
            self.client.get(
                reverse('player_detail', args=[self.team.id, 9999])
            ).status_code, 404
        )


class TeamListByLeagueTest(BaseCase):
    """チーム一覧もリーグごとに分ける。"""

    def setUp(self):
        super().setUp()
        self.other = orm_models.League.objects.create(name='別リーグ')
        self.x = orm_models.Team.objects.create(league=self.other, name='Xチーム')

    def test_grouped_by_league(self):
        listing = self.service.list_teams_by_league()

        grouped = {g.league_name: [t.name for t in g.teams] for g in listing.rows}
        self.assertEqual(
            grouped, {'テストリーグ': ['テストチーム', '相手チーム'], '別リーグ': ['Xチーム']}
        )

    def test_leagues_without_teams_are_omitted(self):
        orm_models.League.objects.create(name='空リーグ')
        names = [g.league_name for g in self.service.list_teams_by_league().rows]

        self.assertNotIn('空リーグ', names)

    def test_sorting_applies_within_each_league(self):
        orm_models.Team.objects.create(league=self.other, name='Aチーム')

        listing = self.service.list_teams_by_league(sort='name', descending=False)
        grouped = {g.league_name: [t.name for t in g.teams] for g in listing.rows}

        self.assertEqual(grouped['別リーグ'], ['Aチーム', 'Xチーム'])

    def test_page_shows_each_league_heading(self):
        response = self.client.get(reverse('team_list'))

        self.assertContains(response, 'テストリーグ')
        self.assertContains(response, '別リーグ')
        self.assertEqual(len(response.context['leagues']), 2)

    def test_flat_list_is_still_available_for_filters(self):
        """試合一覧の絞り込みなどは平坦な一覧を使う。"""
        rows = self.service.list_teams().rows
        self.assertEqual(len(rows), 3)


class LeagueScopedStandingsTest(BaseCase):
    """順位はリーグの中で争われる（フェーズ2）。"""

    def setUp(self):
        super().setUp()
        self.other_league = orm_models.League.objects.create(name='別リーグ')
        self.x = orm_models.Team.objects.create(league=self.other_league, name='Xチーム')
        self.y = orm_models.Team.objects.create(league=self.other_league, name='Yチーム')

        # テストリーグ側は僅差、別リーグ側は圧勝
        play_game(self.team, self.rival, home_score=2, away_score=1, day=1)
        play_game(self.x, self.y, home_score=10, away_score=0, day=1)

    def test_standings_are_split_by_league(self):
        board = self.service.get_standings(2026)

        names = {lg.league_name: [r.team_name for r in lg.rows] for lg in board.leagues}
        self.assertEqual(len(board.leagues), 2)
        self.assertEqual(names['テストリーグ'], ['テストチーム', '相手チーム'])
        self.assertEqual(names['別リーグ'], ['Xチーム', 'Yチーム'])

    def test_other_league_teams_do_not_share_the_rank(self):
        """別リーグの1位どうしが同じ表で2位に落ちたりしないこと。"""
        board = self.service.get_standings(2026)

        leaders = [lg.rows[0] for lg in board.leagues]
        self.assertTrue(all(row.rank == 1 for row in leaders))

    def test_leagues_without_games_are_omitted(self):
        orm_models.League.objects.create(name='未実施リーグ')
        board = self.service.get_standings(2026)

        self.assertNotIn('未実施リーグ', [lg.league_name for lg in board.leagues])

    def test_page_shows_each_league_heading(self):
        response = self.client.get(reverse('standings'))

        self.assertContains(response, 'テストリーグ')
        self.assertContains(response, '別リーグ')


class LeagueDetailTest(BaseCase):
    """リーグ画面（フェーズ2）。"""

    def setUp(self):
        super().setUp()
        self.outsider_league = orm_models.League.objects.create(name='別リーグ')
        self.outsider = orm_models.Team.objects.create(
            league=self.outsider_league, name='部外チーム'
        )
        play_game(self.team, self.rival, home_score=5, away_score=3, day=1)
        self.url = reverse('league_detail', args=[self.league.id])

    def test_page_renders(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'テストリーグ')

    def test_shows_only_member_teams(self):
        detail = self.service.get_league_detail(self.league.id)

        names = [t.name for t in detail.teams]
        self.assertIn('テストチーム', names)
        self.assertNotIn('部外チーム', names)

    def test_shows_standings_and_recent_games(self):
        detail = self.service.get_league_detail(self.league.id)

        self.assertEqual(detail.standings[0].team_name, 'テストチーム')
        self.assertEqual(len(detail.recent_games), 1)

    def test_games_of_other_leagues_are_excluded(self):
        another = orm_models.Team.objects.create(
            league=self.outsider_league, name='部外チーム2'
        )
        play_game(self.outsider, another, day=2)

        detail = self.service.get_league_detail(self.league.id)
        self.assertEqual(len(detail.recent_games), 1)

    def test_season_can_be_selected(self):
        play_game(self.team, self.rival, year=2025, day=1)

        detail = self.service.get_league_detail(self.league.id, 2025)

        self.assertEqual(detail.year, 2025)
        self.assertEqual(detail.available_years, [2026, 2025])

    def test_league_without_games(self):
        detail = self.service.get_league_detail(self.outsider_league.id)

        self.assertEqual(detail.standings, [])
        self.assertIsNone(detail.year)

    def test_missing_league_returns_404(self):
        self.assertEqual(
            self.client.get(reverse('league_detail', args=[9999])).status_code, 404
        )

    def test_team_list_links_to_the_league(self):
        body = self.client.get(reverse('team_list')).content.decode()
        self.assertIn(self.url, body)


class AdminGroupingTest(BaseCase):
    """管理画面の一覧をリーグ・チームごとに区切る。

    標準テンプレートを差し替えているため、グループ化しない一覧が
    従来どおり出ることも確かめる。
    """

    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser(username='root', password='x'))
        self.other = orm_models.League.objects.create(name='別リーグ')
        self.x = orm_models.Team.objects.create(league=self.other, name='Xチーム')
        self.service.register_player(self.team.id, '山田', 10, '内野手')
        self.service.register_player(self.x.id, '田中', 20, '外野手')

    def test_team_list_has_league_headings(self):
        response = self.client.get('/admin/myapp/team/')

        self.assertContains(response, 'group-heading-row')
        self.assertContains(response, 'テストリーグ')
        self.assertContains(response, '別リーグ')

    def test_team_list_shows_each_league_once(self):
        body = self.client.get('/admin/myapp/team/').content.decode()
        self.assertEqual(body.count('>テストリーグ</td>'), 1)

    def test_player_list_groups_by_team(self):
        response = self.client.get('/admin/myapp/player/')

        self.assertContains(response, 'group-heading-row')
        self.assertContains(response, 'テストリーグ · テストチーム')

    def test_grouping_is_dropped_when_the_user_sorts(self):
        """列で並べ替えるとまとまりが崩れるので、見出しを出さない。"""
        response = self.client.get('/admin/myapp/team/?o=1')

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'group-heading-row')

    def test_other_changelists_are_unaffected(self):
        """group_by を持たない一覧は従来どおり描画されること。"""
        for url in ['/admin/myapp/league/', '/admin/myapp/game/', '/admin/auth/user/']:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, 'group-heading-row')

    def test_result_rows_are_still_rendered(self):
        body = self.client.get('/admin/myapp/team/').content.decode()

        self.assertIn('result_list', body)
        self.assertIn('テストチーム', body)
        self.assertIn('Xチーム', body)


class GameEntryTest(BaseCase):
    """サイトからの試合登録と成績の一括入力（フェーズ3）。"""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(username='scorer', password='x')
        self.client.force_login(self.user)
        self.batter = self.service.register_player(self.team.id, '山田', 10, '内野手')
        self.pitcher = self.service.register_player(self.team.id, '佐藤', 18, '投手')

    def _create_game(self):
        return self.client.post(reverse('game_create'), {
            'year': '2026', 'played_on': '2026-04-01',
            'home_team': self.team.id, 'away_team': self.rival.id,
            'home_score': '5', 'away_score': '3',
        })

    def test_login_is_required(self):
        self.client.logout()
        response = self.client.get(reverse('game_create'))

        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/login/', response['Location'])

    def test_create_game_then_go_to_stats(self):
        response = self._create_game()

        game = orm_models.Game.objects.get()
        self.assertEqual(game.home_score, 5)
        self.assertRedirects(response, reverse('game_edit', args=[game.id]))

    def test_same_team_is_rejected_without_crashing(self):
        response = self.client.post(reverse('game_create'), {
            'year': '2026', 'played_on': '2026-04-01',
            'home_team': self.team.id, 'away_team': self.team.id,
            'home_score': '0', 'away_score': '0',
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(orm_models.Game.objects.count(), 0)

    def test_edit_page_lists_the_roster(self):
        self._create_game()
        game = orm_models.Game.objects.get()

        response = self.client.get(reverse('game_edit', args=[game.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '山田')
        self.assertContains(response, '佐藤')
        # 野手は打撃表、投手は投球表に振り分けられる
        self.assertEqual(len(response.context['batting_rows']), 1)
        self.assertEqual(len(response.context['pitching_rows']), 1)

    def _stats_payload(self, game, **overrides):
        payload = {
            'year': '2026', 'played_on': '2026-04-01',
            'home_team': self.team.id, 'away_team': self.rival.id,
            'home_score': '5', 'away_score': '3',
            'batting-TOTAL_FORMS': '1', 'batting-INITIAL_FORMS': '1',
            'batting-MIN_NUM_FORMS': '0', 'batting-MAX_NUM_FORMS': '1000',
            'batting-0-player_id': str(self.batter.id),
            'pitching-TOTAL_FORMS': '1', 'pitching-INITIAL_FORMS': '1',
            'pitching-MIN_NUM_FORMS': '0', 'pitching-MAX_NUM_FORMS': '1000',
            'pitching-0-player_id': str(self.pitcher.id),
        }
        payload.update(overrides)
        return payload

    def test_save_stats_for_the_roster(self):
        self._create_game()
        game = orm_models.Game.objects.get()

        self.client.post(reverse('game_edit', args=[game.id]), self._stats_payload(
            game,
            **{'batting-0-at_bats': '4', 'batting-0-singles': '2',
               'pitching-0-innings_pitched': '7.0', 'pitching-0-strikeouts': '8'},
        ))

        detail = self.service.get_player_detail(self.team.id, self.batter.id)
        self.assertEqual(detail.at_bats, 4)
        pitcher = self.service.get_player_detail(self.team.id, self.pitcher.id)
        self.assertEqual(pitcher.strikeouts, 8)
        self.assertEqual(pitcher.innings_pitched, '7.0')

    def test_blank_rows_are_not_recorded(self):
        """出場しなかった選手の行を残さない。"""
        self._create_game()
        game = orm_models.Game.objects.get()

        self.client.post(
            reverse('game_edit', args=[game.id]), self._stats_payload(game)
        )

        self.assertEqual(orm_models.GameBattingLine.objects.count(), 0)
        self.assertEqual(orm_models.GamePitchingLine.objects.count(), 0)

    def test_clearing_a_row_removes_the_record(self):
        """一度入力した選手を「出場していない」に戻せること。"""
        self._create_game()
        game = orm_models.Game.objects.get()
        url = reverse('game_edit', args=[game.id])

        self.client.post(url, self._stats_payload(game, **{'batting-0-at_bats': '4'}))
        self.assertEqual(orm_models.GameBattingLine.objects.count(), 1)

        self.client.post(url, self._stats_payload(game))
        self.assertEqual(orm_models.GameBattingLine.objects.count(), 0)

    def test_existing_stats_are_prefilled(self):
        self._create_game()
        game = orm_models.Game.objects.get()
        url = reverse('game_edit', args=[game.id])
        self.client.post(url, self._stats_payload(game, **{'batting-0-at_bats': '4'}))

        form = self.client.get(url).context['batting_rows'][0][0]
        self.assertEqual(form.initial['at_bats'], 4)

    def test_score_can_be_corrected(self):
        self._create_game()
        game = orm_models.Game.objects.get()

        self.client.post(
            reverse('game_edit', args=[game.id]),
            self._stats_payload(game, home_score='9'),
        )

        self.assertEqual(orm_models.Game.objects.get().home_score, 9)

    def test_missing_game_returns_404(self):
        self.assertEqual(
            self.client.get(reverse('game_edit', args=[9999])).status_code, 404
        )


class AuthTest(TestCase):
    def test_login_redirect_url_resolves(self):
        from django.conf import settings
        self.assertTrue(reverse(settings.LOGIN_REDIRECT_URL))

    def test_signup_page_is_reachable(self):
        self.assertEqual(self.client.get('/accounts/signup/').status_code, 200)
