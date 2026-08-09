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
    name = models.CharField(max_length=50, unique=True, verbose_name='リーグ名')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='登録日時')

    class Meta:
        verbose_name = 'リーグ'
        # 日本語では単複同形。既定のままだと管理画面に 'Leagues' と出る
        verbose_name_plural = 'リーグ'

    def __str__(self):
        return self.name


class Team(models.Model):
    league = models.ForeignKey(
        League, on_delete=models.CASCADE, related_name='teams', verbose_name='リーグ'
    )
    name = models.CharField(max_length=100, verbose_name='チーム名')
    city = models.CharField(max_length=100, blank=True, verbose_name='本拠地')

    class Meta:
        verbose_name = 'チーム'
        verbose_name_plural = 'チーム'

    def __str__(self):
        return self.name


class Player(models.Model):
    team = models.ForeignKey(
        Team, on_delete=models.CASCADE, related_name='players', verbose_name='チーム'
    )
    name = models.CharField(max_length=100, verbose_name='選手名')
    number = models.IntegerField(verbose_name='背番号')
    position = models.CharField(
        max_length=10,
        choices=POSITION_CHOICES,
        default=Position.INFIELDER.value,
        verbose_name='守備位置',
    )
    is_active = models.BooleanField(default=True, verbose_name='在籍中')

    class Meta:
        verbose_name = '選手'
        verbose_name_plural = '選手'

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

    class Meta:
        verbose_name = '打撃成績'
        verbose_name_plural = '打撃成績'

    def __str__(self):
        return f"{self.player.name} の打撃成績"


class PitcherStats(models.Model):
    player = models.OneToOneField(
        Player, on_delete=models.CASCADE, related_name='pitcher_stats'
    )
    wins = models.IntegerField(default=0, verbose_name="勝利")
    losses = models.IntegerField(default=0, verbose_name="敗戦")
    saves = models.IntegerField(default=0, verbose_name="セーブ")
    # 野球表記の投球回（5.2 = 5回と2/3）。解釈は domain の InningsPitched が担う。
    innings_pitched = models.FloatField(default=0.0, verbose_name="投球回")
    earned_runs = models.IntegerField(default=0, verbose_name="自責点")
    strikeouts = models.IntegerField(default=0, verbose_name="奪三振")
    hits_allowed = models.IntegerField(default=0, verbose_name="被安打")
    walks_allowed = models.IntegerField(default=0, verbose_name="与四球")

    class Meta:
        verbose_name = '投球成績'
        verbose_name_plural = '投球成績'

    def __str__(self):
        return f"{self.player.name} の投球成績"
