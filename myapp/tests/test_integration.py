"""結合テスト。リポジトリの永続化と画面の動作を確認する。"""

from django.test import TestCase
from django.urls import reverse

from myapp.application.services import TeamApplicationService
from myapp.domain.exceptions import DuplicateJerseyNumber
from myapp.domain.value_objects import (
    BattingLine,
    InningsPitched,
    JerseyNumber,
    PitchingLine,
    Position,
)
from myapp.infrastructure import orm_models
from myapp.infrastructure.queries import DjangoTeamListQuery
from myapp.infrastructure.repositories import DjangoTeamRepository


class RepositoryRoundTripTest(TestCase):
    """ORM ⇄ ドメインの往復でデータが失われないこと。"""

    def setUp(self):
        self.league = orm_models.League.objects.create(name='テストリーグ')
        self.team_row = orm_models.Team.objects.create(
            league=self.league, name='テストチーム', city='東京'
        )
        self.repo = DjangoTeamRepository()

    def test_save_and_reload_a_batter(self):
        team = self.repo.find_by_id(self.team_row.id)
        player = team.add_player('山田', JerseyNumber(10), Position.INFIELDER)
        player.record_batting(BattingLine(at_bats=10, singles=2, home_runs=1))
        self.repo.save(team)

        reloaded = self.repo.find_by_id(self.team_row.id)
        saved = reloaded.find_player(player.id)

        self.assertEqual(saved.name, '山田')
        self.assertEqual(saved.number.value, 10)
        self.assertEqual(saved.position, Position.INFIELDER)
        self.assertEqual(saved.batting.at_bats, 10)
        self.assertEqual(saved.batting.hits, 3)

    def test_innings_pitched_survives_the_round_trip(self):
        """5.2（17アウト）が保存・再読込で変質しないこと。"""
        team = self.repo.find_by_id(self.team_row.id)
        player = team.add_player('佐藤', JerseyNumber(18), Position.PITCHER)
        player.record_pitching(
            PitchingLine(innings=InningsPitched.from_notation('5.2'), earned_runs=2)
        )
        self.repo.save(team)

        reloaded = self.repo.find_by_id(self.team_row.id)
        saved = reloaded.find_player(player.id)

        self.assertEqual(saved.pitching.innings.outs, 17)
        self.assertEqual(str(saved.pitching.innings), '5.2')

    def test_duplicate_number_is_rejected_on_the_aggregate(self):
        team = self.repo.find_by_id(self.team_row.id)
        team.add_player('山田', JerseyNumber(10), Position.INFIELDER)
        self.repo.save(team)

        reloaded = self.repo.find_by_id(self.team_row.id)
        with self.assertRaises(DuplicateJerseyNumber):
            reloaded.add_player('田中', JerseyNumber(10), Position.OUTFIELDER)


class ApplicationServiceTest(TestCase):
    def setUp(self):
        self.league = orm_models.League.objects.create(name='テストリーグ')
        self.team_row = orm_models.Team.objects.create(
            league=self.league, name='テストチーム'
        )
        self.service = TeamApplicationService(
            teams=DjangoTeamRepository(), team_list_query=DjangoTeamListQuery()
        )

    def test_register_player(self):
        self.service.register_player(self.team_row.id, '山田', 10, '内野手')

        rows = self.service.list_batters(self.team_row.id).rows
        self.assertEqual([r.name for r in rows], ['山田'])

    def test_register_duplicate_number_is_rejected(self):
        self.service.register_player(self.team_row.id, '山田', 10, '内野手')
        with self.assertRaises(DuplicateJerseyNumber):
            self.service.register_player(self.team_row.id, '田中', 10, '外野手')

    def test_team_summary_counts_active_players(self):
        self.service.register_player(self.team_row.id, '山田', 10, '内野手')
        self.service.register_player(self.team_row.id, '佐藤', 18, '投手')

        summary = self.service.list_teams().rows[0]
        self.assertEqual(summary.name, 'テストチーム')
        self.assertEqual(summary.league_name, 'テストリーグ')
        self.assertEqual(summary.player_count, 2)


class PlayerListViewTest(TestCase):
    def setUp(self):
        self.league = orm_models.League.objects.create(name='テストリーグ')
        self.team = orm_models.Team.objects.create(league=self.league, name='テストチーム')
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


class PlayerEditViewTest(TestCase):
    def setUp(self):
        self.league = orm_models.League.objects.create(name='テストリーグ')
        self.team = orm_models.Team.objects.create(league=self.league, name='テストチーム')
        self.service = TeamApplicationService(
            teams=DjangoTeamRepository(), team_list_query=DjangoTeamListQuery()
        )

    def _edit_url(self, player_id):
        return reverse('player_edit', args=[self.team.id, player_id])

    def test_designated_hitter_keeps_position(self):
        """指名打者を編集しても投手に化けないこと（旧バグの再発防止）。"""
        player = self.service.register_player(self.team.id, '大谷', 17, '指名打者')

        response = self.client.get(self._edit_url(player.id))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('指名打者', html)
        self.assertIn('<option value="指名打者" selected>', html)

    def test_update_batting_stats(self):
        player = self.service.register_player(self.team.id, '山田', 10, '内野手')

        self.client.post(self._edit_url(player.id), {
            'name': '山田太郎', 'number': '10', 'position': '内野手',
            'at_bats': '10', 'singles': '2', 'doubles': '1', 'triples': '0',
            'home_runs': '1', 'runs_batted_in': '5', 'walks': '2',
            'hit_by_pitch': '0', 'sacrifice_flies': '0',
        })

        detail = self.service.get_player_detail(self.team.id, player.id)
        self.assertEqual(detail.name, '山田太郎')
        self.assertEqual(detail.at_bats, 10)
        self.assertEqual(detail.home_runs, 1)

    def test_update_pitching_normalises_innings(self):
        """5.3 のような表記は 6.0 に正規化されて保存される。"""
        player = self.service.register_player(self.team.id, '佐藤', 18, '投手')

        self.client.post(self._edit_url(player.id), {
            'name': '佐藤', 'number': '18', 'position': '投手',
            'innings_pitched': '5.3', 'earned_runs': '2', 'wins': '1', 'losses': '0',
            'saves': '0', 'strikeouts': '7', 'hits_allowed': '4', 'walks_allowed': '1',
        })

        detail = self.service.get_player_detail(self.team.id, player.id)
        self.assertEqual(detail.innings_pitched, '6.0')

    def test_duplicate_number_on_update_is_rejected(self):
        self.service.register_player(self.team.id, '山田', 10, '内野手')
        tanaka = self.service.register_player(self.team.id, '田中', 11, '外野手')

        self.client.post(self._edit_url(tanaka.id), {
            'name': '田中', 'number': '10', 'position': '外野手',
            'at_bats': '0', 'singles': '0', 'doubles': '0', 'triples': '0',
            'home_runs': '0', 'runs_batted_in': '0', 'walks': '0',
            'hit_by_pitch': '0', 'sacrifice_flies': '0',
        })

        detail = self.service.get_player_detail(self.team.id, tanaka.id)
        self.assertEqual(detail.number, 11)

    def test_missing_player_returns_404(self):
        self.assertEqual(self.client.get(self._edit_url(9999)).status_code, 404)


class DashboardTest(TestCase):
    def setUp(self):
        self.league = orm_models.League.objects.create(name='テストリーグ')
        self.team = orm_models.Team.objects.create(league=self.league, name='テストチーム')
        self.service = TeamApplicationService(
            teams=DjangoTeamRepository(), team_list_query=DjangoTeamListQuery()
        )

    def test_counts(self):
        self.service.register_player(self.team.id, '山田', 10, '内野手')
        self.service.register_player(self.team.id, '佐藤', 18, '投手')

        board = self.service.get_dashboard()

        self.assertEqual(board.league_count, 1)
        self.assertEqual(board.team_count, 1)
        self.assertEqual(board.batter_count, 1)
        self.assertEqual(board.pitcher_count, 1)
        self.assertEqual(board.player_count, 2)

    def test_ranking_spans_all_teams(self):
        """ランキングはチームをまたいで集計される。"""
        other = orm_models.Team.objects.create(league=self.league, name='別チーム')
        a = self.service.register_player(self.team.id, '山田', 10, '内野手')
        b = self.service.register_player(other.id, '田中', 10, '外野手')

        self.service.update_player(
            self.team.id, a.id, name='山田', number=10, position_label='内野手',
            batting=BattingLine(at_bats=10, singles=1),
        )
        self.service.update_player(
            other.id, b.id, name='田中', number=10, position_label='外野手',
            batting=BattingLine(at_bats=10, home_runs=4),
        )

        board = self.service.get_dashboard()
        names = [e.player_name for e in board.ops_leaders]

        self.assertEqual(names, ['田中', '山田'])
        self.assertEqual(board.ops_leaders[0].team_name, '別チーム')

    def test_page_renders(self):
        self.service.register_player(self.team.id, '山田', 10, '内野手')
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'テストチーム')

    def test_page_renders_without_any_data(self):
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 200)


class HeaderNavigationTest(TestCase):
    """ヘッダーの導線が権限に応じて出し分けられること。"""

    def setUp(self):
        from django.contrib.auth.models import User

        self.staff = User.objects.create_user(
            username='staff', password='x', is_staff=True
        )
        self.member = User.objects.create_user(username='member', password='x')

    # 空状態の文言にも「管理画面」が出てくるため、リンク要素そのもので判定する
    ADMIN_LINK = 'class="nav-admin-link"'

    def test_staff_sees_admin_link(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse('dashboard'))
        self.assertContains(response, self.ADMIN_LINK)
        self.assertContains(response, 'href="/admin/"')

    def test_normal_user_does_not_see_admin_link(self):
        """一般ユーザーには管理画面への導線を出さない。"""
        self.client.force_login(self.member)
        response = self.client.get(reverse('dashboard'))
        self.assertNotContains(response, self.ADMIN_LINK)

    def test_anonymous_does_not_see_admin_link(self):
        response = self.client.get(reverse('dashboard'))
        self.assertNotContains(response, self.ADMIN_LINK)
        self.assertContains(response, 'ログイン')

    def test_admin_page_actually_rejects_normal_user(self):
        """導線を隠すだけでなく、管理画面側でも入れないこと。"""
        self.client.force_login(self.member)
        response = self.client.get('/admin/')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/admin/login/', response['Location'])

    def test_admin_page_accepts_staff(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get('/admin/').status_code, 200)


class AdminTest(TestCase):
    """管理画面のテーマ適用と一覧表示。"""

    def setUp(self):
        from django.contrib.auth.models import User

        self.staff = User.objects.create_superuser(username='root', password='x')
        self.client.force_login(self.staff)
        self.league = orm_models.League.objects.create(name='テストリーグ')
        self.team = orm_models.Team.objects.create(league=self.league, name='テストチーム')
        self.service = TeamApplicationService(
            teams=DjangoTeamRepository(), team_list_query=DjangoTeamListQuery()
        )

    def test_admin_pages_use_the_admin_theme(self):
        for url in ['/admin/', '/admin/myapp/player/', '/admin/myapp/team/']:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertContains(response, 'myapp/css/admin-theme.css')
                # サイト側のテーマが混ざっていないこと
                self.assertNotContains(response, 'myapp/css/theme.css')

    def test_site_pages_do_not_use_the_admin_theme(self):
        response = self.client.get(reverse('dashboard'))
        self.assertContains(response, 'myapp/css/theme.css')
        self.assertNotContains(response, 'myapp/css/admin-theme.css')

    def test_player_list_shows_key_stat_from_domain(self):
        """一覧の主要成績はドメイン層の計算結果と一致すること。"""
        player = self.service.register_player(self.team.id, '山田', 10, '内野手')
        self.service.update_player(
            self.team.id, player.id, name='山田', number=10, position_label='内野手',
            batting=BattingLine(at_bats=10, singles=2, home_runs=1),
        )

        detail = self.service.get_player_detail(self.team.id, player.id)
        response = self.client.get('/admin/myapp/player/')

        self.assertContains(response, f'OPS {detail.ops:.3f}')

    def test_pitcher_without_innings_is_labelled(self):
        self.service.register_player(self.team.id, '佐藤', 18, '投手')
        response = self.client.get('/admin/myapp/player/')
        self.assertContains(response, '未登板')


class AdminIndexTest(TestCase):
    """管理画面トップの構成。"""

    def setUp(self):
        from django.contrib.auth.models import User

        self.client.force_login(User.objects.create_superuser(username='root', password='x'))
        self.league = orm_models.League.objects.create(name='テストリーグ')
        self.team = orm_models.Team.objects.create(league=self.league, name='テストチーム')
        self.service = TeamApplicationService(
            teams=DjangoTeamRepository(), team_list_query=DjangoTeamListQuery()
        )

    def test_models_are_labelled_in_japanese(self):
        response = self.client.get('/admin/')
        for label in ['野球データ', 'リーグ', 'チーム', '選手']:
            with self.subTest(label=label):
                self.assertContains(response, label)

    def test_stats_models_are_hidden_from_index(self):
        """成績は選手のインラインで扱うので、トップの導線には出さない。"""
        response = self.client.get('/admin/')
        self.assertNotContains(response, '/admin/myapp/playerstats/')
        self.assertNotContains(response, '/admin/myapp/pitcherstats/')

    def test_stats_models_are_still_reachable_by_url(self):
        """導線に出さないだけで、URL からは開ける。"""
        self.assertEqual(self.client.get('/admin/myapp/playerstats/').status_code, 200)

    def test_models_follow_domain_order(self):
        """リーグ → チーム → 選手 の順で並ぶこと。"""
        body = self.client.get('/admin/').content.decode()
        positions = [
            body.index('/admin/myapp/league/'),
            body.index('/admin/myapp/team/'),
            body.index('/admin/myapp/player/'),
        ]
        self.assertEqual(positions, sorted(positions))

    def test_baseball_data_comes_before_auth(self):
        body = self.client.get('/admin/').content.decode()
        self.assertLess(body.index('/admin/myapp/'), body.index('/admin/auth/'))

    def test_overview_counts(self):
        self.service.register_player(self.team.id, '山田', 10, '内野手')
        self.service.register_player(self.team.id, '佐藤', 18, '投手')

        overview = self.service.get_admin_overview()

        self.assertEqual(overview.league_count, 1)
        self.assertEqual(overview.team_count, 1)
        self.assertEqual(overview.player_count, 2)
        self.assertEqual(overview.pitcher_count, 1)

    def test_overview_flags_players_without_stats(self):
        """成績未入力はランキング対象外なので、管理者に知らせる。"""
        self.service.register_player(self.team.id, '山田', 10, '内野手')

        self.assertEqual(self.service.get_admin_overview().players_without_stats, 1)

        self.service.update_player(
            self.team.id,
            orm_models.Player.objects.get(number=10).id,
            name='山田', number=10, position_label='内野手',
            batting=BattingLine(at_bats=10, singles=3),
        )
        self.assertEqual(self.service.get_admin_overview().players_without_stats, 0)

    def test_overview_flags_empty_teams_and_retired_players(self):
        orm_models.Team.objects.create(league=self.league, name='空チーム')
        player = self.service.register_player(self.team.id, '山田', 10, '内野手')
        orm_models.Player.objects.filter(id=player.id).update(is_active=False)

        overview = self.service.get_admin_overview()

        self.assertEqual(overview.teams_without_players, 2)
        self.assertEqual(overview.retired_count, 1)

    def test_notes_appear_on_the_page(self):
        self.service.register_player(self.team.id, '山田', 10, '内野手')
        response = self.client.get('/admin/')
        self.assertContains(response, '成績が未入力の選手')


class StandingsTest(TestCase):
    """シーズン成績の永続化と順位表画面。"""

    def setUp(self):
        self.league = orm_models.League.objects.create(name='テストリーグ')
        self.a = orm_models.Team.objects.create(league=self.league, name='Aチーム')
        self.b = orm_models.Team.objects.create(league=self.league, name='Bチーム')
        self.service = TeamApplicationService(
            teams=DjangoTeamRepository(), team_list_query=DjangoTeamListQuery()
        )

    def test_record_survives_the_round_trip(self):
        self.service.record_team_season(self.a.id, 2026, wins=80, losses=55, ties=8)

        board = self.service.get_standings(2026)
        row = board.rows[0]

        self.assertEqual(row.wins, 80)
        self.assertEqual(row.losses, 55)
        self.assertEqual(row.ties, 8)
        self.assertEqual(row.games_played, 143)
        self.assertEqual(row.winning_percentage, '.593')

    def test_same_season_updates_instead_of_duplicating(self):
        self.service.record_team_season(self.a.id, 2026, wins=80, losses=55, ties=8)
        self.service.record_team_season(self.a.id, 2026, wins=90, losses=45, ties=8)

        self.assertEqual(
            orm_models.TeamSeasonRecord.objects.filter(team=self.a, year=2026).count(), 1
        )
        self.assertEqual(self.service.get_standings(2026).rows[0].wins, 90)

    def test_rank_is_derived_from_winning_percentage(self):
        self.service.record_team_season(self.a.id, 2026, wins=60, losses=75, ties=8)
        self.service.record_team_season(self.b.id, 2026, wins=80, losses=55, ties=8)

        rows = self.service.get_standings(2026).rows

        self.assertEqual([r.team_name for r in rows], ['Bチーム', 'Aチーム'])
        self.assertEqual([r.rank for r in rows], [1, 2])
        self.assertEqual(rows[0].games_behind, '—')
        self.assertEqual(rows[1].games_behind, '20.0')

    def test_defaults_to_the_latest_season(self):
        self.service.record_team_season(self.a.id, 2025, wins=70, losses=65, ties=8)
        self.service.record_team_season(self.a.id, 2026, wins=80, losses=55, ties=8)

        board = self.service.get_standings()

        self.assertEqual(board.year, 2026)
        self.assertEqual(board.available_years, [2026, 2025])

    def test_page_renders(self):
        self.service.record_team_season(self.a.id, 2026, wins=80, losses=55, ties=8)

        response = self.client.get(reverse('standings'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Aチーム')
        self.assertContains(response, '.593')

    def test_page_by_year(self):
        self.service.record_team_season(self.a.id, 2025, wins=70, losses=65, ties=8)
        self.service.record_team_season(self.a.id, 2026, wins=80, losses=55, ties=8)

        response = self.client.get(reverse('standings_by_year', args=[2025]))
        self.assertContains(response, '2025年')

    def test_page_without_any_record(self):
        response = self.client.get(reverse('standings'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'シーズン成績がまだ登録されていません')

    def test_duplicate_is_blocked_at_the_database_too(self):
        """集約だけでなく DB 制約でも一意性を担保していること。"""
        from django.db import IntegrityError, transaction

        orm_models.TeamSeasonRecord.objects.create(team=self.a, year=2026, wins=1, losses=1)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                orm_models.TeamSeasonRecord.objects.create(
                    team=self.a, year=2026, wins=2, losses=2
                )


class SortingViewTest(TestCase):
    """画面から URL でソートできること。"""

    def setUp(self):
        self.league = orm_models.League.objects.create(name='テストリーグ')
        self.team = orm_models.Team.objects.create(league=self.league, name='テストチーム')
        self.service = TeamApplicationService(
            teams=DjangoTeamRepository(), team_list_query=DjangoTeamListQuery()
        )
        a = self.service.register_player(self.team.id, '少打', 1, '内野手')
        b = self.service.register_player(self.team.id, '多打', 2, '外野手')
        self.service.update_player(
            self.team.id, a.id, name='少打', number=1, position_label='内野手',
            batting=BattingLine(at_bats=20, singles=4, home_runs=1),
        )
        self.service.update_player(
            self.team.id, b.id, name='多打', number=2, position_label='外野手',
            batting=BattingLine(at_bats=20, singles=2, home_runs=5),
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
        """pos=pitcher のような条件を落とさないこと。"""
        body = self.client.get(f'{self.url}?pos=pitcher').content.decode()
        self.assertIn('pos=pitcher', body)
        self.assertIn('sort=era', body)

    def test_header_shows_the_active_direction(self):
        body = self.client.get(f'{self.url}?sort=home_runs&dir=desc').content.decode()
        self.assertIn('sort-link is-active', body)

    def test_team_list_can_be_sorted(self):
        orm_models.Team.objects.create(league=self.league, name='Aチーム')
        response = self.client.get(f"{reverse('team_list')}?sort=name&dir=asc")
        names = [t.name for t in response.context['teams']]
        self.assertEqual(names, sorted(names))

    def test_team_list_defaults_to_manual_order(self):
        """既定は管理画面で設定した表示順。"""
        orm_models.Team.objects.filter(id=self.team.id).update(display_order=5)
        first = orm_models.Team.objects.create(
            league=self.league, name='Zチーム', display_order=1
        )
        response = self.client.get(reverse('team_list'))
        self.assertEqual(response.context['teams'][0].name, 'Zチーム')
        self.assertEqual(response.context['current_sort'], 'order')

    def test_standings_can_be_sorted(self):
        self.service.record_team_season(self.team.id, 2026, wins=80, losses=55, ties=8)
        response = self.client.get(f"{reverse('standings')}?sort=wins&dir=desc")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['standings'].sort, 'wins')


class TeamOrderingTest(TestCase):
    """管理画面で設定した表示順が各所に反映されること。"""

    def setUp(self):
        self.league = orm_models.League.objects.create(name='テストリーグ')
        self.b = orm_models.Team.objects.create(
            league=self.league, name='Bチーム', display_order=1
        )
        self.a = orm_models.Team.objects.create(
            league=self.league, name='Aチーム', display_order=2
        )
        self.service = TeamApplicationService(
            teams=DjangoTeamRepository(), team_list_query=DjangoTeamListQuery()
        )

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

    def test_admin_league_page_includes_the_order_field(self):
        from django.contrib.auth.models import User

        self.client.force_login(User.objects.create_superuser(username='root', password='x'))
        response = self.client.get(f'/admin/myapp/league/{self.league.id}/change/')
        self.assertContains(response, 'display_order')
        self.assertContains(response, 'admin-inline-sortable.js')


class AuthTest(TestCase):
    def test_login_redirect_url_resolves(self):
        from django.conf import settings
        self.assertTrue(reverse(settings.LOGIN_REDIRECT_URL))

    def test_signup_page_is_reachable(self):
        self.assertEqual(self.client.get('/accounts/signup/').status_code, 200)
