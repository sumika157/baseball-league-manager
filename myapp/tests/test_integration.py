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

        rows = self.service.list_batters(self.team_row.id)
        self.assertEqual([r.name for r in rows], ['山田'])

    def test_register_duplicate_number_is_rejected(self):
        self.service.register_player(self.team_row.id, '山田', 10, '内野手')
        with self.assertRaises(DuplicateJerseyNumber):
            self.service.register_player(self.team_row.id, '田中', 10, '外野手')

    def test_team_summary_counts_active_players(self):
        self.service.register_player(self.team_row.id, '山田', 10, '内野手')
        self.service.register_player(self.team_row.id, '佐藤', 18, '投手')

        summary = self.service.list_teams()[0]
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


class AuthTest(TestCase):
    def test_login_redirect_url_resolves(self):
        from django.conf import settings
        self.assertTrue(reverse(settings.LOGIN_REDIRECT_URL))

    def test_signup_page_is_reachable(self):
        self.assertEqual(self.client.get('/accounts/signup/').status_code, 200)
