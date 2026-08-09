from .models import League, Team, Player, PlayerStats
from django.core.exceptions import ValidationError

class BaseballService:
    @staticmethod
    def create_team(league_id, name, city=""):
        league = League.objects.get(id=league_id)
        # 同じリーグ内に同名のチームがないかチェック
        if Team.objects.filter(league=league, name=name).exists():
            raise ValidationError(f"リーグ「{league.name}」に「{name}」は既に存在します。")
        
        return Team.objects.create(league=league, name=name, city=city)

    @staticmethod
    def add_player_to_team(team_id, name, number):
        team = Team.objects.get(id=team_id)
        
        # 【重要】同じチーム内で背番号が重複しないかバリデーション
        if Player.objects.filter(team=team, number=number, is_active=True).exists():
            raise ValidationError(f"背番号{number}は既に「{team.name}」で使用されています。")
        
        return Player.objects.create(team=team, name=name, number=number)

    @staticmethod
    def get_active_players(team_id):
        """現役選手（アーカイブされていない選手）のみ取得"""
        return Player.objects.filter(team_id=team_id, is_active=True).order_by('number')

    @staticmethod
    def calculate_batting_average(hits, at_bats):
        """打率を計算する (安打 / 打数)"""
        if at_bats == 0:
            return ".000"
        avg = hits / at_bats
        # 野球独特の「.333」のような形式にフォーマット
        return "{:.3f}".format(avg).lstrip('0')
    
    @staticmethod
    def add_player_to_team(team_id, name, number, position):
        team = Team.objects.get(id=team_id)
        
        # 背番号のバリデーション（現役選手内で重複チェック）
        if Player.objects.filter(team=team, number=number, is_active=True).exists():
            raise ValidationError(f"背番号 {number} は「{team.name}」で既に使用されています。")
        
        # 選手作成
        return Player.objects.create(team=team, name=name, number=number, position=position)
    
    @staticmethod
    def update_player(player_id, name, number, position):
        player = Player.objects.get(id=player_id)
        
        # 背番号が変わる場合、他の現役選手と重複しないかチェック
        if player.number != int(number):
            if Player.objects.filter(team=player.team, number=number, is_active=True).exists():
                raise ValidationError(f"背番号 {number} は既に他の選手が使用しています。")
        
        player.name = name
        player.number = number
        player.position = position
        player.save()
        return player
    
    @staticmethod
    def update_player_stats(player_id, at_bats, hits, home_runs, rbi):
        """成績を更新する。まだデータがない場合は作成する。"""
        player = Player.objects.get(id=player_id)
        # update_or_create は便利！
        stats, created = PlayerStats.objects.update_or_create(
            player=player,
            defaults={
                'at_bats': at_bats,
                'hits': hits,
                'home_runs': home_runs,
                'runs_batted_in': rbi
            }
        )
        return stats

    @staticmethod
    def format_avg(hits, at_bats):
        """打率を計算して .333 のような形式で返す"""
        if not at_bats or int(at_bats) == 0:
            return ".000"
        avg = int(hits) / int(at_bats)
        # 小数点以下3桁で、先頭の0を消す
        return "{:.3f}".format(avg).lstrip('0')