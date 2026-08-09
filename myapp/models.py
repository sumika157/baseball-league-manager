from django.db import models
from django.contrib.auth.models import User

class League(models.Model):
    name = models.CharField(max_length=50, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

class Team(models.Model):
    league = models.ForeignKey(League, on_delete=models.CASCADE, related_name='teams')
    name = models.CharField(max_length=100)
    city = models.CharField(max_length=100, blank=True)

    def __str__(self):
        return self.name

class Player(models.Model):
    # ポジションの選択肢を定義
    POSITION_CHOICES = [
        ('投手', '投手'),
        ('捕手', '捕手'),
        ('内野手', '内野手'),
        ('外野手', '外野手'),
        ('指名打者', '指名打者'),
    ]

    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='players')
    name = models.CharField(max_length=100)
    number = models.IntegerField()
    position = models.CharField(
        max_length=10, 
        choices=POSITION_CHOICES, 
        default='内野手'
    ) # 追加
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.number} {self.name} ({self.position})"
    
class PlayerStats(models.Model):
    player = models.OneToOneField(Player, on_delete=models.CASCADE, related_name='stats')
    at_bats = models.IntegerField(default=0, verbose_name="打数")
    singles = models.IntegerField(default=0, verbose_name="単打")
    doubles = models.IntegerField(default=0, verbose_name="二塁打") # 追加
    triples = models.IntegerField(default=0, verbose_name="三塁打") # 追加
    home_runs = models.IntegerField(default=0, verbose_name="本塁打")
    runs_batted_in = models.IntegerField(default=0, verbose_name="打点")
    walks = models.IntegerField(default=0, verbose_name="四球")   # 追加
    hit_by_pitch = models.IntegerField(default=0, verbose_name="死球") # 追加
    sacrifice_flies = models.IntegerField(default=0, verbose_name="犠飛") # 追加

    def __str__(self):
        return f"{self.player.name} の成績"
    
class PitcherStats(models.Model):
    player = models.OneToOneField(Player, on_delete=models.CASCADE, related_name='pitcher_stats')
    wins = models.IntegerField(default=0)
    losses = models.IntegerField(default=0)
    saves = models.IntegerField(default=0)
    innings_pitched = models.FloatField(default=0.0) # 投球回
    earned_runs = models.IntegerField(default=0)     # 自責点
    strikeouts = models.IntegerField(default=0)      # 奪三振
    hits_allowed = models.IntegerField(default=0, verbose_name="被安打")
    walks_allowed = models.IntegerField(default=0, verbose_name="与四球")

    def __str__(self):
        return f"{self.player.name} の投手成績"