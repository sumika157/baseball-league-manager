"""Django ORM のモデル定義（永続化専用）。

ここにあるのは「テーブルの形」だけで、業務ルールは持たせない。
ルールは myapp/domain 側にあり、両者の変換は repositories.py が担う。

チームの勝敗も選手の通算成績も**保持しない**。試合（Game）が唯一の出典で、
必要な値はそこから集計して求める。同じ事実の出典を2つ作らないため。
"""

from django.db import models

from ..domain.value_objects import Position

# 守備位置の選択肢はドメインの Position を唯一の出典とする。
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
    # リーグ編集画面でドラッグして並べ替えた結果がここに入る
    display_order = models.PositiveIntegerField(default=0, verbose_name='表示順')

    class Meta:
        verbose_name = 'チーム'
        verbose_name_plural = 'チーム'
        # 手動の並び順を既定とし、未設定どうしは名前で安定させる
        ordering = ['display_order', 'name']

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
        ordering = ['team', 'number']

    def __str__(self):
        return f"{self.number} {self.name} ({self.position})"


class Game(models.Model):
    """試合。チームの勝敗も選手の成績も、すべてここから集計する。"""

    year = models.IntegerField(verbose_name='シーズン')
    played_on = models.DateField(verbose_name='試合日')
    home_team = models.ForeignKey(
        Team, on_delete=models.CASCADE, related_name='home_games', verbose_name='ホーム'
    )
    away_team = models.ForeignKey(
        Team, on_delete=models.CASCADE, related_name='away_games', verbose_name='ビジター'
    )
    home_score = models.PositiveIntegerField(default=0, verbose_name='ホーム得点')
    away_score = models.PositiveIntegerField(default=0, verbose_name='ビジター得点')

    class Meta:
        verbose_name = '試合'
        verbose_name_plural = '試合'
        ordering = ['-played_on', '-id']
        constraints = [
            models.CheckConstraint(
                check=~models.Q(home_team=models.F('away_team')),
                name='game_teams_differ',
            ),
        ]

    def __str__(self):
        return f"{self.played_on} {self.home_team.name} {self.home_score}-{self.away_score} {self.away_team.name}"


class GameBattingLine(models.Model):
    """1試合ぶんの打撃成績。"""

    game = models.ForeignKey(
        Game, on_delete=models.CASCADE, related_name='batting_lines', verbose_name='試合'
    )
    player = models.ForeignKey(
        Player, on_delete=models.CASCADE, related_name='game_batting', verbose_name='選手'
    )
    at_bats = models.IntegerField(default=0, verbose_name='打数')
    singles = models.IntegerField(default=0, verbose_name='単打')
    doubles = models.IntegerField(default=0, verbose_name='二塁打')
    triples = models.IntegerField(default=0, verbose_name='三塁打')
    home_runs = models.IntegerField(default=0, verbose_name='本塁打')
    runs_batted_in = models.IntegerField(default=0, verbose_name='打点')
    walks = models.IntegerField(default=0, verbose_name='四球')
    hit_by_pitch = models.IntegerField(default=0, verbose_name='死球')
    sacrifice_flies = models.IntegerField(default=0, verbose_name='犠飛')

    class Meta:
        verbose_name = '打撃成績'
        verbose_name_plural = '打撃成績'
        constraints = [
            models.UniqueConstraint(
                fields=['game', 'player'], name='unique_game_batting'
            ),
        ]

    def __str__(self):
        return f"{self.player.name} の打撃成績"


class GamePitchingLine(models.Model):
    """1試合ぶんの投球成績。"""

    game = models.ForeignKey(
        Game, on_delete=models.CASCADE, related_name='pitching_lines', verbose_name='試合'
    )
    player = models.ForeignKey(
        Player, on_delete=models.CASCADE, related_name='game_pitching', verbose_name='選手'
    )
    # 野球表記の投球回（5.2 = 5回と2/3）。解釈は domain の InningsPitched が担う。
    innings_pitched = models.FloatField(default=0.0, verbose_name='投球回')
    wins = models.IntegerField(default=0, verbose_name='勝利')
    losses = models.IntegerField(default=0, verbose_name='敗戦')
    saves = models.IntegerField(default=0, verbose_name='セーブ')
    earned_runs = models.IntegerField(default=0, verbose_name='自責点')
    strikeouts = models.IntegerField(default=0, verbose_name='奪三振')
    hits_allowed = models.IntegerField(default=0, verbose_name='被安打')
    walks_allowed = models.IntegerField(default=0, verbose_name='与四球')

    class Meta:
        verbose_name = '投球成績'
        verbose_name_plural = '投球成績'
        constraints = [
            models.UniqueConstraint(
                fields=['game', 'player'], name='unique_game_pitching'
            ),
        ]

    def __str__(self):
        return f"{self.player.name} の投球成績"
