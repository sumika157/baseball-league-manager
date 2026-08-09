"""Django ORM のモデル定義（永続化専用）。

ここにあるのは「テーブルの形」だけで、業務ルールは持たせない。
ルールは myapp/domain 側にあり、両者の変換は repositories.py が担う。

移動しても Django のアプリラベルは 'myapp' のままなので、
既存のマイグレーションとテーブル名はそのまま使える。
myapp/models.py が本モジュールを再輸出しており、Django のアプリ読み込み時に
確実にインポートされるようにしている。
"""

from django.db import models

from ..domain.value_objects import Position

# 守備位置の選択肢はドメインの Position を唯一の出典とする。
# （以前はこことテンプレート2枚に別々の一覧があり、テンプレート側だけ
#   '指名打者' が欠落して選手が投手に化けるバグが発生していた）
POSITION_CHOICES = [(position.value, position.value) for position in Position]


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
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='players')
    name = models.CharField(max_length=100)
    number = models.IntegerField()
    position = models.CharField(
        max_length=10,
        choices=POSITION_CHOICES,
        default=Position.INFIELDER.value,
    )
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.number} {self.name} ({self.position})"


class PlayerStats(models.Model):
    player = models.OneToOneField(Player, on_delete=models.CASCADE, related_name='stats')
    at_bats = models.IntegerField(default=0, verbose_name="打数")
    singles = models.IntegerField(default=0, verbose_name="単打")
    doubles = models.IntegerField(default=0, verbose_name="二塁打")
    triples = models.IntegerField(default=0, verbose_name="三塁打")
    home_runs = models.IntegerField(default=0, verbose_name="本塁打")
    runs_batted_in = models.IntegerField(default=0, verbose_name="打点")
    walks = models.IntegerField(default=0, verbose_name="四球")
    hit_by_pitch = models.IntegerField(default=0, verbose_name="死球")
    sacrifice_flies = models.IntegerField(default=0, verbose_name="犠飛")

    def __str__(self):
        return f"{self.player.name} の成績"


class PitcherStats(models.Model):
    player = models.OneToOneField(
        Player, on_delete=models.CASCADE, related_name='pitcher_stats'
    )
    wins = models.IntegerField(default=0)
    losses = models.IntegerField(default=0)
    saves = models.IntegerField(default=0)
    # 野球表記の投球回（5.2 = 5回と2/3）。解釈は domain の InningsPitched が担う。
    innings_pitched = models.FloatField(default=0.0)
    earned_runs = models.IntegerField(default=0)
    strikeouts = models.IntegerField(default=0)
    hits_allowed = models.IntegerField(default=0, verbose_name="被安打")
    walks_allowed = models.IntegerField(default=0, verbose_name="与四球")

    def __str__(self):
        return f"{self.player.name} の投手成績"
