from django.test import TestCase
from django.core.exceptions import ValidationError

from .models import League, Team, Player
from .services import BaseballService


class BaseballServiceTest(TestCase):
    def setUp(self):
        self.league = League.objects.create(name="テストリーグ")
        self.team = Team.objects.create(league=self.league, name="テストチーム", city="東京")

    # --- create_team ---

    def test_create_team_success(self):
        """新しいチームを作成できる"""
        team = BaseballService.create_team(self.league.id, "新チーム", "大阪")
        self.assertEqual(team.name, "新チーム")
        self.assertEqual(team.league, self.league)

    def test_create_team_duplicate_name(self):
        """同じリーグ内に同名のチームは作れない"""
        with self.assertRaises(ValidationError):
            BaseballService.create_team(self.league.id, "テストチーム")

    # --- add_player_to_team ---

    def test_add_player_success(self):
        """選手を登録できる"""
        player = BaseballService.add_player_to_team(self.team.id, "山田", 10, "内野手")
        self.assertEqual(player.name, "山田")
        self.assertEqual(player.number, 10)
        self.assertTrue(player.is_active)

    def test_add_player_duplicate_number(self):
        """同じチーム内で背番号は重複できない"""
        BaseballService.add_player_to_team(self.team.id, "山田", 10, "内野手")
        with self.assertRaises(ValidationError):
            BaseballService.add_player_to_team(self.team.id, "田中", 10, "外野手")

    # --- update_player ---

    def test_update_player_success(self):
        """選手情報を更新できる"""
        player = BaseballService.add_player_to_team(self.team.id, "山田", 10, "内野手")
        updated = BaseballService.update_player(player.id, "山田太郎", 11, "外野手")
        self.assertEqual(updated.name, "山田太郎")
        self.assertEqual(updated.number, 11)
        self.assertEqual(updated.position, "外野手")

    def test_update_player_duplicate_number(self):
        """他の選手が使っている背番号には変更できない"""
        BaseballService.add_player_to_team(self.team.id, "山田", 10, "内野手")
        player = BaseballService.add_player_to_team(self.team.id, "田中", 11, "外野手")
        with self.assertRaises(ValidationError):
            BaseballService.update_player(player.id, "田中", 10, "外野手")

    def test_update_player_keep_same_number(self):
        """背番号を変えずに更新した場合は重複エラーにならない"""
        player = BaseballService.add_player_to_team(self.team.id, "山田", 10, "内野手")
        updated = BaseballService.update_player(player.id, "山田次郎", 10, "捕手")
        self.assertEqual(updated.name, "山田次郎")
        self.assertEqual(updated.number, 10)

    # --- get_active_players ---

    def test_get_active_players_excludes_inactive(self):
        """引退した選手は取得されない"""
        BaseballService.add_player_to_team(self.team.id, "山田", 10, "内野手")
        retired = BaseballService.add_player_to_team(self.team.id, "田中", 11, "外野手")
        retired.is_active = False
        retired.save()

        actives = BaseballService.get_active_players(self.team.id)
        self.assertEqual([p.name for p in actives], ["山田"])

    # --- format_avg ---

    def test_format_avg(self):
        """打率が .333 形式で返る"""
        self.assertEqual(BaseballService.format_avg(1, 3), ".333")
        self.assertEqual(BaseballService.format_avg(0, 4), ".000")

    def test_format_avg_zero_at_bats(self):
        """打数0でもゼロ除算にならない"""
        self.assertEqual(BaseballService.format_avg(0, 0), ".000")


class PlayerListViewTest(TestCase):
    def setUp(self):
        self.league = League.objects.create(name="テストリーグ")
        self.team = Team.objects.create(league=self.league, name="テストチーム")

    def test_duplicate_number_is_rejected_on_create(self):
        """画面からの登録でも背番号の重複が弾かれる"""
        url = f"/team/{self.team.id}/"
        self.client.post(url, {'name': '山田', 'number': '10', 'position': '内野手'})
        self.client.post(url, {'name': '田中', 'number': '10', 'position': '外野手'})

        self.assertEqual(Player.objects.filter(team=self.team, number=10).count(), 1)

    def test_create_player_creates_stats_record(self):
        """登録するとポジションに応じた成績レコードが作られる"""
        url = f"/team/{self.team.id}/"
        self.client.post(url, {'name': '山田', 'number': '10', 'position': '内野手'})
        self.client.post(url, {'name': '佐藤', 'number': '18', 'position': '投手'})

        batter = Player.objects.get(number=10)
        pitcher = Player.objects.get(number=18)
        self.assertTrue(hasattr(batter, 'stats'))
        self.assertTrue(hasattr(pitcher, 'pitcher_stats'))


class AuthRedirectTest(TestCase):
    def test_login_redirect_url_resolves(self):
        """LOGIN_REDIRECT_URL が解決できる（NoReverseMatch の再発防止）"""
        from django.urls import reverse
        from django.conf import settings
        self.assertTrue(reverse(settings.LOGIN_REDIRECT_URL))

    def test_signup_page_is_reachable(self):
        """新規登録画面が表示できる"""
        response = self.client.get('/accounts/signup/')
        self.assertEqual(response.status_code, 200)
