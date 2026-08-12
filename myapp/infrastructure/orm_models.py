"""Django ORM のモデル定義（永続化専用）。

ここにあるのは「テーブルの形」だけで、業務ルールは持たせない。
ルールは myapp/domain 側にあり、両者の変換は repositories.py が担う。

チームの勝敗も選手の通算成績も**保持しない**。試合（Game）が唯一の出典で、
必要な値はそこから集計して求める。同じ事実の出典を2つ作らないため。
"""

from django.conf import settings
from django.db import models

from ..domain.value_objects import (
    AdvanceReason,
    Base,
    ErrorKind,
    FieldingPosition,
    Handedness,
    PlateAppearanceResult,
    Position,
    StadiumProfile,
)

# 守備位置の選択肢はドメインの Position を唯一の出典とする。
POSITION_CHOICES = [(position.value, position.value) for position in Position]
# 試合で就いた守備位置。登録位置（Position）とは別の概念
FIELDING_POSITION_CHOICES = [(p.value, p.value) for p in FieldingPosition]
# 打席まわりの選択肢も同じくドメインの値オブジェクトが出典。ここに文字列を
# 並べ直すと、種別を増やしたときに片方だけ古いまま静かにずれる。
PLATE_APPEARANCE_RESULT_CHOICES = [(r.value, r.value) for r in PlateAppearanceResult]
ADVANCE_REASON_CHOICES = [(r.value, r.value) for r in AdvanceReason]
ERROR_KIND_CHOICES = [(k.value, k.value) for k in ErrorKind]
# 塁は順序そのものが意味を持つ（大小比較が「進んだか」）ため数値で持つ
BASE_CHOICES = [(base.value, base.label) for base in Base]
# 打球の処理経路（スコアブックの 6-3）は守備位置をこの記号で連ねて1列に持つ
FIELDED_BY_SEPARATOR = "-"


class League(models.Model):
    name = models.CharField(max_length=50, unique=True, verbose_name="リーグ名")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="登録日時")
    # 管理画面でドラッグして並べ替えた結果がここに入る
    display_order = models.PositiveIntegerField(default=0, verbose_name="表示順")
    foreign_player_roster_limit = models.PositiveIntegerField(
        null=True,
        blank=True,
        default=5,
        verbose_name="外国人選手登録枠",
        help_text="登録できる外国人選手の上限人数。空欄なら無制限。",
    )
    foreign_player_game_limit = models.PositiveIntegerField(
        null=True,
        blank=True,
        default=3,
        verbose_name="外国人選手出場枠",
        help_text="1試合に出場できる外国人選手の上限人数。空欄なら無制限。",
    )

    class Meta:
        verbose_name = "リーグ"
        # 日本語では単複同形。既定のままだと管理画面に 'Leagues' と出る
        verbose_name_plural = "リーグ"
        # 手動の並び順を既定とし、未設定どうしは名前で安定させる
        ordering = ["display_order", "name"]

    def __str__(self) -> str:
        return self.name


class Stadium(models.Model):
    """球場。所在地はここが持つ（チーム側に地名を二重に持たせない）。"""

    # 選択肢はドメインの StadiumProfile が唯一の出典。ここには並べない
    SURFACE_CHOICES = [(s, s) for s in StadiumProfile.SURFACES]
    ROOF_CHOICES = [(r, r) for r in StadiumProfile.ROOFS]

    name = models.CharField(max_length=100, unique=True, verbose_name="球場名")
    city = models.CharField(max_length=100, blank=True, verbose_name="所在地")
    capacity = models.PositiveIntegerField(null=True, blank=True, verbose_name="収容人数")
    surface = models.CharField(max_length=10, choices=SURFACE_CHOICES, blank=True, verbose_name="グラウンド")
    roof = models.CharField(
        max_length=10,
        choices=ROOF_CHOICES,
        blank=True,
        verbose_name="屋根",
        help_text="開閉式は、屋根を閉じれば天候に左右されません。",
    )
    opened_year = models.IntegerField(null=True, blank=True, verbose_name="開場年")

    class Meta:
        verbose_name = "球場"
        verbose_name_plural = "球場"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Team(models.Model):
    league = models.ForeignKey(League, on_delete=models.CASCADE, related_name="teams", verbose_name="リーグ")
    name = models.CharField(max_length=100, verbose_name="チーム名")
    home_stadium = models.ForeignKey(
        Stadium,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="home_teams",
        verbose_name="本拠地球場",
    )
    # リーグ編集画面でドラッグして並べ替えた結果がここに入る
    display_order = models.PositiveIntegerField(default=0, verbose_name="表示順")
    # チーム担当者。ログインすれば誰でも全チームを編集できた状態をやめ、
    # 担当者はこのチームが関わる範囲（ロスター・このチームが絡む試合）だけ
    # 編集できるようにする。管理ユーザー（is_staff）は担当者に関わらず全権を持つ
    managers = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="managed_teams",
        verbose_name="担当者",
        help_text="このチームの選手・試合を編集できるユーザー。管理ユーザーは常に編集できます。",
    )

    class Meta:
        verbose_name = "チーム"
        verbose_name_plural = "チーム"
        # 手動の並び順を既定とし、未設定どうしは名前で安定させる
        ordering = ["display_order", "name"]

    def __str__(self) -> str:
        return self.name


class Player(models.Model):
    """選手。所属チームと背番号は在籍（PlayerStint）が持つ。

    移籍すると所属が変わるため、選手そのものにチームを持たせない。
    """

    name = models.CharField(max_length=100, verbose_name="選手名")
    name_kana = models.CharField(max_length=100, blank=True, verbose_name="よみがな")
    back_name = models.CharField(
        max_length=20,
        blank=True,
        verbose_name="背ネーム",
        help_text="ユニフォーム背面のアルファベット表記。",
    )
    position = models.CharField(
        max_length=10,
        choices=POSITION_CHOICES,
        default=Position.INFIELDER.value,
        verbose_name="守備位置",
    )
    # --- プロフィール。どれも任意で、分かっているものだけ埋める ---
    HANDEDNESS_CHOICES = [(h.value, h.value) for h in Handedness]

    birth_date = models.DateField(null=True, blank=True, verbose_name="生年月日")
    throws = models.CharField(max_length=2, choices=HANDEDNESS_CHOICES, blank=True, verbose_name="投")
    bats = models.CharField(max_length=2, choices=HANDEDNESS_CHOICES, blank=True, verbose_name="打")
    height_cm = models.PositiveIntegerField(null=True, blank=True, verbose_name="身長(cm)")
    weight_kg = models.PositiveIntegerField(null=True, blank=True, verbose_name="体重(kg)")
    birthplace = models.CharField(max_length=100, blank=True, verbose_name="出身地")
    debut_year = models.IntegerField(null=True, blank=True, verbose_name="入団年")
    # プロ入り前の経歴。通った所だけ埋める
    high_school = models.CharField(max_length=100, blank=True, verbose_name="出身高校")
    university = models.CharField(max_length=100, blank=True, verbose_name="出身大学")
    corporate_team = models.CharField(max_length=100, blank=True, verbose_name="出身社会人チーム")
    nationality = models.CharField(max_length=100, blank=True, verbose_name="国籍")
    is_foreign_player = models.BooleanField(
        default=False,
        verbose_name="外国人選手",
        help_text="外国人枠の対象として数える選手かどうか。",
    )

    class Meta:
        verbose_name = "選手"
        verbose_name_plural = "選手"
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.position})"


class PlayerStint(models.Model):
    """在籍。ある選手が、あるチームに、いつからいつまで在籍したか。

    背番号も在籍ごとに持つ。移籍で変わるため選手側には持たせない。
    to_year が空なら現在も在籍している。
    """

    player = models.ForeignKey(Player, on_delete=models.CASCADE, related_name="stints", verbose_name="選手")
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="stints", verbose_name="チーム")
    number = models.IntegerField(verbose_name="背番号")
    from_year = models.IntegerField(verbose_name="加入年")
    to_year = models.IntegerField(
        null=True,
        blank=True,
        verbose_name="退団年",
        help_text="空欄なら現在も在籍しています。",
    )

    class Meta:
        verbose_name = "在籍"
        verbose_name_plural = "在籍"
        ordering = ["-from_year", "number"]
        constraints = [
            # 同じチームに同じ年から二重に加入することはない
            models.UniqueConstraint(fields=["player", "team", "from_year"], name="unique_player_team_from"),
        ]

    def __str__(self) -> str:
        end = self.to_year or "現在"
        return f"{self.player.name} / {self.team.name} ({self.from_year}〜{end})"


class Captaincy(models.Model):
    """主将在任。PlayerStint と同じ形の期間テーブルだが、対象は別軸。

    to_year が空なら現在も主将。
    """

    player = models.ForeignKey(Player, on_delete=models.CASCADE, related_name="captaincies", verbose_name="選手")
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="captaincies", verbose_name="チーム")
    from_year = models.IntegerField(verbose_name="就任年")
    to_year = models.IntegerField(
        null=True,
        blank=True,
        verbose_name="退任年",
        help_text="空欄なら現在も主将です。",
    )

    class Meta:
        verbose_name = "主将在任"
        verbose_name_plural = "主将在任"
        ordering = ["-from_year"]
        constraints = [
            models.UniqueConstraint(fields=["player", "team", "from_year"], name="unique_captaincy_player_team_from"),
        ]

    def __str__(self) -> str:
        end = self.to_year or "現在"
        return f"{self.player.name} / {self.team.name} 主将 ({self.from_year}〜{end})"


class Game(models.Model):
    """試合。チームの勝敗も選手の成績も、すべてここから集計する。"""

    year = models.IntegerField(verbose_name="シーズン")
    played_on = models.DateField(verbose_name="試合日")
    home_team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="home_games", verbose_name="ホーム")
    away_team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="away_games", verbose_name="ビジター")
    home_score = models.PositiveIntegerField(default=0, verbose_name="ホーム得点")
    away_score = models.PositiveIntegerField(default=0, verbose_name="ビジター得点")

    class Meta:
        verbose_name = "試合"
        verbose_name_plural = "試合"
        ordering = ["-played_on", "-id"]
        constraints = [
            models.CheckConstraint(
                # check= は Django 5.1 で condition= に改名された（6.0 で削除）
                condition=~models.Q(home_team=models.F("away_team")),
                name="game_teams_differ",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.played_on} {self.home_team.name} {self.home_score}-{self.away_score} {self.away_team.name}"


class GameInningScore(models.Model):
    """イニングスコア。回ごとの得点。

    勝敗・セーブ・ホールドは継投した時点のスコアで決まるため、最終得点だけでは
    導けない。延長もあるので回数は固定しない。
    """

    game = models.ForeignKey(Game, on_delete=models.CASCADE, related_name="inning_scores", verbose_name="試合")
    inning = models.PositiveIntegerField(verbose_name="回")
    is_home = models.BooleanField(
        verbose_name="ホームの攻撃",
        help_text="ビジターが表、ホームが裏に攻めます。",
    )
    runs = models.PositiveIntegerField(default=0, verbose_name="得点")

    class Meta:
        verbose_name = "イニングスコア"
        verbose_name_plural = "イニングスコア"
        ordering = ["inning", "is_home"]
        constraints = [
            models.UniqueConstraint(fields=["game", "inning", "is_home"], name="unique_game_inning_half"),
        ]

    def __str__(self) -> str:
        half = "裏" if self.is_home else "表"
        return f"{self.inning}回{half} {self.runs}点"


class GameBattingLine(models.Model):
    """1試合ぶんの打撃成績。"""

    game = models.ForeignKey(Game, on_delete=models.CASCADE, related_name="batting_lines", verbose_name="試合")
    player = models.ForeignKey(Player, on_delete=models.CASCADE, related_name="game_batting", verbose_name="選手")
    # 打線での位置づけ。並びはこの3つで決まる（打順 → 交代の順）
    batting_order = models.IntegerField(
        null=True,
        blank=True,
        verbose_name="打順",
        help_text="1〜9。空欄なら並びは末尾になります。",
    )
    slot_sequence = models.IntegerField(
        default=0,
        verbose_name="交代の順",
        help_text="0 がスタメン。1以上は同じ打順への途中出場です。",
    )
    fielding_position = models.CharField(
        max_length=2,
        choices=FIELDING_POSITION_CHOICES,
        blank=True,
        verbose_name="守備位置",
        help_text="この試合で就いた守備。代打は「打」、代走は「走」。",
    )
    at_bats = models.IntegerField(default=0, verbose_name="打数")
    singles = models.IntegerField(default=0, verbose_name="単打")
    doubles = models.IntegerField(default=0, verbose_name="二塁打")
    triples = models.IntegerField(default=0, verbose_name="三塁打")
    home_runs = models.IntegerField(default=0, verbose_name="本塁打")
    runs_batted_in = models.IntegerField(default=0, verbose_name="打点")
    walks = models.IntegerField(default=0, verbose_name="四球")
    hit_by_pitch = models.IntegerField(default=0, verbose_name="死球")
    sacrifice_flies = models.IntegerField(default=0, verbose_name="犠飛")
    # ここから下は打席の記録から導く項目。手入力していた頃は数えられなかった
    runs = models.IntegerField(default=0, verbose_name="得点")
    strikeouts = models.IntegerField(default=0, verbose_name="三振")
    sacrifice_bunts = models.IntegerField(default=0, verbose_name="犠打")
    intentional_walks = models.IntegerField(default=0, verbose_name="故意四球")
    stolen_bases = models.IntegerField(default=0, verbose_name="盗塁")
    caught_stealing = models.IntegerField(default=0, verbose_name="盗塁刺")
    double_plays = models.IntegerField(default=0, verbose_name="併殺打")

    class Meta:
        verbose_name = "打撃成績"
        verbose_name_plural = "打撃成績"
        constraints = [
            models.UniqueConstraint(fields=["game", "player"], name="unique_game_batting"),
        ]

    def __str__(self) -> str:
        return f"{self.player.name} の打撃成績"


class GamePitchingLine(models.Model):
    """1試合ぶんの投球成績。"""

    game = models.ForeignKey(Game, on_delete=models.CASCADE, related_name="pitching_lines", verbose_name="試合")
    player = models.ForeignKey(Player, on_delete=models.CASCADE, related_name="game_pitching", verbose_name="選手")
    # 登板順。1 が先発。ボックススコアは投げた順に並べる
    appearance_order = models.IntegerField(default=1, verbose_name="登板順")
    # 何回から投げたか。セーブ・ホールドの条件は登板した時点のスコアで決まる
    entered_inning = models.PositiveIntegerField(default=1, verbose_name="登板した回")
    # 野球表記の投球回（5.2 = 5回と2/3）。解釈は domain の InningsPitched が担う。
    innings_pitched = models.FloatField(default=0.0, verbose_name="投球回")
    wins = models.IntegerField(default=0, verbose_name="勝利")
    losses = models.IntegerField(default=0, verbose_name="敗戦")
    saves = models.IntegerField(default=0, verbose_name="セーブ")
    # 日本プロ野球の公式記録。セーブが記録される状況で登板し、
    # リードを保ったまま次の投手へ引き継いだ救援投手に付く
    holds = models.IntegerField(default=0, verbose_name="ホールド")
    # 失点。自責点だけでは「失策絡みで失点したが自責点ではない」投手を評価できない
    runs_allowed = models.IntegerField(default=0, verbose_name="失点")
    earned_runs = models.IntegerField(default=0, verbose_name="自責点")
    strikeouts = models.IntegerField(default=0, verbose_name="奪三振")
    hits_allowed = models.IntegerField(default=0, verbose_name="被安打")
    walks_allowed = models.IntegerField(default=0, verbose_name="与四球")
    # FIP は本塁打・四死球・三振だけで投手を評価するため、この2つが要る
    home_runs_allowed = models.IntegerField(default=0, verbose_name="被本塁打")
    hit_by_pitch_allowed = models.IntegerField(default=0, verbose_name="与死球")

    class Meta:
        verbose_name = "投球成績"
        verbose_name_plural = "投球成績"
        constraints = [
            models.UniqueConstraint(fields=["game", "player"], name="unique_game_pitching"),
        ]

    def __str__(self) -> str:
        return f"{self.player.name} の投球成績"


class GamePlateAppearance(models.Model):
    """1打席。紙のスコアブックのマス目1つにあたる。

    **数えた結果はここに持たない。** 打数・安打・打点・投球回・失点は、この行と
    進塁（GameRunnerAdvance）から導出する。上の GameBattingLine / GamePitchingLine は
    移行が終わるまで並存する導出値で、去就は実測して決める。
    """

    game = models.ForeignKey(Game, on_delete=models.CASCADE, related_name="plate_appearances", verbose_name="試合")
    sequence = models.PositiveIntegerField(
        verbose_name="打席の順番",
        help_text="試合内の通し番号。1から欠けずに続きます。試合の時系列はこれだけで決まります。",
    )
    inning = models.PositiveIntegerField(verbose_name="回")
    is_bottom = models.BooleanField(
        verbose_name="ホームの攻撃",
        help_text="ビジターが表、ホームが裏に攻めます。",
    )
    batter = models.ForeignKey(Player, on_delete=models.CASCADE, related_name="plate_appearances", verbose_name="打者")
    pitcher = models.ForeignKey(
        Player, on_delete=models.CASCADE, related_name="plate_appearances_pitched", verbose_name="投手"
    )
    batting_order = models.PositiveIntegerField(verbose_name="打順", help_text="1〜9。")
    slot_sequence = models.IntegerField(
        default=0,
        verbose_name="交代の順",
        help_text="0 がスタメン。1以上は同じ打順への途中出場です。",
    )
    result = models.CharField(max_length=10, choices=PLATE_APPEARANCE_RESULT_CHOICES, verbose_name="結果")
    fielded_by = models.CharField(
        max_length=30,
        blank=True,
        verbose_name="打球の処理",
        help_text="守備位置を「-」で連ねます（遊ゴロ併殺なら 遊-二-一）。刺殺・補殺の出典です。",
    )

    class Meta:
        verbose_name = "打席"
        verbose_name_plural = "打席"
        ordering = ["sequence"]
        constraints = [
            models.UniqueConstraint(fields=["game", "sequence"], name="unique_game_plate_appearance"),
        ]

    def __str__(self) -> str:
        half = "裏" if self.is_bottom else "表"
        return f"{self.inning}回{half} {self.batting_order}番 {self.result}"


class GameRunnerAdvance(models.Model):
    """打席の中で走者が動いた記録。打者自身も走者として記録する（進塁前は「打者席」）。

    得点・打点・盗塁・残塁・自責点はすべてここから導く。**理由を持たない進塁は
    作らない** — 失策で還った走者に打点が付いてしまう。
    """

    plate_appearance = models.ForeignKey(
        GamePlateAppearance, on_delete=models.CASCADE, related_name="advances", verbose_name="打席"
    )
    runner = models.ForeignKey(Player, on_delete=models.CASCADE, related_name="runner_advances", verbose_name="走者")
    from_base = models.IntegerField(choices=BASE_CHOICES, verbose_name="進塁前")
    to_base = models.IntegerField(choices=BASE_CHOICES, verbose_name="進塁後")
    reason = models.CharField(max_length=10, choices=ADVANCE_REASON_CHOICES, verbose_name="理由")
    error_index = models.IntegerField(
        null=True,
        blank=True,
        verbose_name="失策の位置",
        help_text="失策に起因する進塁なら、同じ打席の何番目の失策か（0 から数えます）。",
    )

    class Meta:
        verbose_name = "進塁"
        verbose_name_plural = "進塁"
        # 失策の位置と同じく、打席の中での並びを保つ（読み書きで順が変わらないように）
        ordering = ["id"]

    def __str__(self) -> str:
        return f"{self.runner.name} {self.get_from_base_display()}→{self.get_to_base_display()}（{self.reason}）"


class GameRunnerSubstitution(models.Model):
    """代走。塁上の走者を別の選手に入れ替える。

    交代は進塁ではないため GameRunnerAdvance では表せない。これが無いと塁の状態を
    再生したときに「その塁にいない走者が進んだ」と誤って弾いてしまう。
    """

    plate_appearance = models.ForeignKey(
        GamePlateAppearance, on_delete=models.CASCADE, related_name="substitutions", verbose_name="打席"
    )
    base = models.IntegerField(choices=BASE_CHOICES, verbose_name="塁")
    leaving_runner = models.ForeignKey(
        Player, on_delete=models.CASCADE, related_name="replaced_on_base", verbose_name="退く走者"
    )
    entering_runner = models.ForeignKey(
        Player, on_delete=models.CASCADE, related_name="pinch_running", verbose_name="代走"
    )

    class Meta:
        verbose_name = "代走"
        verbose_name_plural = "代走"
        ordering = ["id"]

    def __str__(self) -> str:
        return f"{self.get_base_display()} {self.leaving_runner.name}→{self.entering_runner.name}"


class GameFieldingError(models.Model):
    """失策。誰がどこで何をしたか。

    自責点の判定（規則 9.16 の「失策が無かったものと仮定した再構成」）と守備成績の出典。
    捕逸は公式記録では失策として数えないため、ここではなく進塁の理由として記録する。
    """

    plate_appearance = models.ForeignKey(
        GamePlateAppearance, on_delete=models.CASCADE, related_name="errors", verbose_name="打席"
    )
    player = models.ForeignKey(Player, on_delete=models.CASCADE, related_name="fielding_errors", verbose_name="守備者")
    position = models.CharField(max_length=2, choices=FIELDING_POSITION_CHOICES, verbose_name="守備位置")
    kind = models.CharField(max_length=4, choices=ERROR_KIND_CHOICES, verbose_name="失策の種類")

    class Meta:
        verbose_name = "失策"
        verbose_name_plural = "失策"
        # 進塁の error_index がこの並び順を指すため、読み書きで順が変わってはいけない
        ordering = ["id"]

    def __str__(self) -> str:
        return f"{self.player.name}（{self.position}）の{self.kind}"
