"""Django ORM のモデル定義（永続化専用）。

ここにあるのは「テーブルの形」だけで、業務ルールは持たせない。
ルールは myapp/domain 側にあり、両者の変換は repositories.py が担う。

チームの勝敗も選手の通算成績も**保持しない**。試合（Game）が唯一の出典で、
必要な値はそこから集計して求める。同じ事実の出典を2つ作らないため。
"""

from django.db import models

from ..domain.value_objects import Handedness, Position, StadiumProfile

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


class Stadium(models.Model):
    """球場。所在地はここが持つ（チーム側に地名を二重に持たせない）。"""

    SURFACE_CHOICES = [(s, s) for s in StadiumProfile.SURFACES]

    name = models.CharField(max_length=100, unique=True, verbose_name='球場名')
    city = models.CharField(max_length=100, blank=True, verbose_name='所在地')
    capacity = models.PositiveIntegerField(null=True, blank=True, verbose_name='収容人数')
    surface = models.CharField(
        max_length=10, choices=SURFACE_CHOICES, blank=True, verbose_name='グラウンド'
    )
    opened_year = models.IntegerField(null=True, blank=True, verbose_name='開場年')

    class Meta:
        verbose_name = '球場'
        verbose_name_plural = '球場'
        ordering = ['name']

    def __str__(self):
        return self.name


class Team(models.Model):
    league = models.ForeignKey(
        League, on_delete=models.CASCADE, related_name='teams', verbose_name='リーグ'
    )
    name = models.CharField(max_length=100, verbose_name='チーム名')
    home_stadium = models.ForeignKey(
        Stadium,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='home_teams',
        verbose_name='本拠地球場',
    )
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
    """選手。所属チームと背番号は在籍（PlayerStint）が持つ。

    移籍すると所属が変わるため、選手そのものにチームを持たせない。
    """

    name = models.CharField(max_length=100, verbose_name='選手名')
    position = models.CharField(
        max_length=10,
        choices=POSITION_CHOICES,
        default=Position.INFIELDER.value,
        verbose_name='守備位置',
    )
    # --- プロフィール。どれも任意で、分かっているものだけ埋める ---
    HANDEDNESS_CHOICES = [(h.value, h.value) for h in Handedness]

    birth_date = models.DateField(null=True, blank=True, verbose_name='生年月日')
    throws = models.CharField(
        max_length=2, choices=HANDEDNESS_CHOICES, blank=True, verbose_name='投'
    )
    bats = models.CharField(
        max_length=2, choices=HANDEDNESS_CHOICES, blank=True, verbose_name='打'
    )
    height_cm = models.PositiveIntegerField(null=True, blank=True, verbose_name='身長(cm)')
    weight_kg = models.PositiveIntegerField(null=True, blank=True, verbose_name='体重(kg)')
    birthplace = models.CharField(max_length=100, blank=True, verbose_name='出身地')
    debut_year = models.IntegerField(null=True, blank=True, verbose_name='入団年')
    # プロ入り前の経歴。通った所だけ埋める
    high_school = models.CharField(max_length=100, blank=True, verbose_name='出身高校')
    university = models.CharField(max_length=100, blank=True, verbose_name='出身大学')
    corporate_team = models.CharField(
        max_length=100, blank=True, verbose_name='出身社会人チーム'
    )

    class Meta:
        verbose_name = '選手'
        verbose_name_plural = '選手'
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.position})"


class PlayerStint(models.Model):
    """在籍。ある選手が、あるチームに、いつからいつまで在籍したか。

    背番号も在籍ごとに持つ。移籍で変わるため選手側には持たせない。
    to_year が空なら現在も在籍している。
    """

    player = models.ForeignKey(
        Player, on_delete=models.CASCADE, related_name='stints', verbose_name='選手'
    )
    team = models.ForeignKey(
        Team, on_delete=models.CASCADE, related_name='stints', verbose_name='チーム'
    )
    number = models.IntegerField(verbose_name='背番号')
    from_year = models.IntegerField(verbose_name='加入年')
    to_year = models.IntegerField(
        null=True, blank=True, verbose_name='退団年',
        help_text='空欄なら現在も在籍しています。',
    )

    class Meta:
        verbose_name = '在籍'
        verbose_name_plural = '在籍'
        ordering = ['-from_year', 'number']
        constraints = [
            # 同じチームに同じ年から二重に加入することはない
            models.UniqueConstraint(
                fields=['player', 'team', 'from_year'], name='unique_player_team_from'
            ),
        ]

    def __str__(self):
        end = self.to_year or '現在'
        return f"{self.player.name} / {self.team.name} ({self.from_year}〜{end})"


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
