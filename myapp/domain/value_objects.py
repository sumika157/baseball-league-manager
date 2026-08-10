"""値オブジェクト。

いずれも不変（frozen）で、生成時に自身の正当性を検証する。
「不正な値のインスタンスは存在しえない」状態を作ることで、
呼び出し側での検証漏れを構造的に防ぐ。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_DOWN, Decimal
from enum import Enum

from .exceptions import (
    ForeignPlayerQuotaExceeded,
    InvalidInningsPitched,
    InvalidJerseyNumber,
    InvalidPosition,
    InvalidProfile,
    InvalidSeason,
    InvalidStatValue,
)


class Position(Enum):
    """守備位置。

    これまで '投手' という文字列リテラルが models.py・views.py・テンプレート2枚に
    散在しており、テンプレートだけ '指名打者' が欠落して選手が投手に化けるバグが
    発生していた。守備位置の語彙はこの Enum を唯一の出典とする。
    """

    PITCHER = "投手"
    CATCHER = "捕手"
    INFIELDER = "内野手"
    OUTFIELDER = "外野手"
    DESIGNATED_HITTER = "指名打者"

    @property
    def label(self) -> str:
        return self.value

    @property
    def is_pitcher(self) -> bool:
        """投手成績で評価すべきポジションか。"""
        return self is Position.PITCHER

    @classmethod
    def from_label(cls, label: str) -> Position:
        for position in cls:
            if position.value == label:
                return position
        raise InvalidPosition(f"「{label}」は守備位置として認識できません。")

    @classmethod
    def labels(cls) -> list[str]:
        return [position.value for position in cls]


class FieldingPosition(Enum):
    """試合で実際に就いた守備位置。

    選手の登録位置（Position）とは別の概念。登録は「主にどこを守るか」で
    投手／野手の振り分けに使い、こちらは「この試合でどこを守ったか」を表す。
    内野手として登録された選手が遊撃でも二塁でも出られるため、1つにまとめると
    どちらの問いにも答えられなくなる。

    代打・代走は守備に就かないまま打席・塁上にだけ現れるので、
    守備位置と同じ欄に並ぶ印として扱う（ボックススコアの慣例）。
    """

    PITCHER = "投"
    CATCHER = "捕"
    FIRST_BASE = "一"
    SECOND_BASE = "二"
    THIRD_BASE = "三"
    SHORTSTOP = "遊"
    LEFT_FIELD = "左"
    CENTER_FIELD = "中"
    RIGHT_FIELD = "右"
    DESIGNATED_HITTER = "指"
    PINCH_HITTER = "打"
    PINCH_RUNNER = "走"

    @property
    def label(self) -> str:
        return self.value

    @property
    def is_substitute_only(self) -> bool:
        """守備に就かず、代打・代走としてのみ出場したか。"""
        return self in (FieldingPosition.PINCH_HITTER, FieldingPosition.PINCH_RUNNER)

    @classmethod
    def from_label(cls, label: str) -> FieldingPosition | None:
        """未設定（空）は None を返す。守備位置を記録しない試合もあるため。"""
        if not label:
            return None
        for item in cls:
            if item.value == label:
                return item
        raise InvalidPosition(f"「{label}」は守備位置として認識できません。")

    @classmethod
    def labels(cls) -> list[str]:
        return [item.value for item in cls]

    @classmethod
    def defensive_labels(cls) -> list[str]:
        """守備に就く位置だけ。スタメンの選択肢に使う。"""
        return [item.value for item in cls if not item.is_substitute_only]


@dataclass(frozen=True)
class JerseyNumber:
    """背番号。

    日本の球団では育成選手が3桁を用いるため 0〜999 を許容する。
    """

    value: int

    MIN = 0
    MAX = 999

    def __post_init__(self) -> None:
        try:
            number = int(self.value)
        except (TypeError, ValueError):
            raise InvalidJerseyNumber("背番号は数値で入力してください。") from None

        if number != self.value:
            # int() を通した結果と食い違う場合（'10' や 10.5 など）は正規化して差し替える
            object.__setattr__(self, "value", number)

        if not (self.MIN <= number <= self.MAX):
            raise InvalidJerseyNumber(f"背番号は {self.MIN}〜{self.MAX} の範囲で入力してください。")

    def __str__(self) -> str:
        return str(self.value)


@dataclass(frozen=True)
class InningsPitched:
    """投球回。

    野球では「5.2」が 5回と2/3（＝17アウト）を意味し、10進数の 5.2 ではない。
    この特殊な表記を扱うルールは、これまで次の3箇所に別々の言語で実装されていた。

      - DB          : FloatField にそのまま 5.2 を保存
      - views.py    : ORM 式 floor(x)*3 + (x*10 % 10) でアウト数に変換
      - テンプレート : JavaScript で .3 以上の繰り上がり／繰り下がりを補正

    ここでは内部状態を常に「アウト数」の整数で保持し、表記との変換を
    この型に閉じ込める。5.3 のような不正な表記は 6.0 に正規化される。
    """

    outs: int

    OUTS_PER_INNING = 3

    def __post_init__(self) -> None:
        if not isinstance(self.outs, int):
            raise InvalidInningsPitched("投球回はアウト数（整数）で保持します。")
        if self.outs < 0:
            raise InvalidInningsPitched("投球回に負の値は指定できません。")

    @classmethod
    def zero(cls) -> InningsPitched:
        return cls(outs=0)

    @classmethod
    def from_notation(cls, value) -> InningsPitched:
        """野球表記（5.2 など）からインスタンスを作る。

        小数第1位はアウト数（0〜2）を表す。3 以上が来た場合は
        繰り上げて正規化する（5.3 → 6.0）。
        """
        if value is None or value == "":
            return cls.zero()

        try:
            notation = Decimal(str(value)).quantize(Decimal("0.1"), rounding=ROUND_DOWN)
        except Exception:
            raise InvalidInningsPitched(f"投球回として解釈できない値です: {value!r}") from None

        if notation < 0:
            raise InvalidInningsPitched("投球回に負の値は指定できません。")

        whole = int(notation)
        fraction = int((notation - whole) * 10)
        # fraction が 3 以上でも outs の計算上そのまま繰り上がる（5.3 → 18アウト → 6.0）
        return cls(outs=whole * cls.OUTS_PER_INNING + fraction)

    def to_notation(self) -> Decimal:
        """野球表記（5.2 など）に戻す。"""
        innings, remainder = divmod(self.outs, self.OUTS_PER_INNING)
        return Decimal(innings) + Decimal(remainder) / 10

    @property
    def as_innings(self) -> float:
        """実数としての投球回（5.2 → 5.666...）。指標計算に用いる。"""
        return self.outs / self.OUTS_PER_INNING

    def __add__(self, other: InningsPitched) -> InningsPitched:
        """試合ごとの投球回を積み上げる。アウト数で持っているので単純加算でよい。"""
        if not isinstance(other, InningsPitched):
            return NotImplemented
        return InningsPitched(outs=self.outs + other.outs)

    def __str__(self) -> str:
        return f"{self.to_notation():.1f}"


def _require_non_negative(name: str, value) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise InvalidStatValue(f"{name}は数値で入力してください。") from None
    if number < 0:
        raise InvalidStatValue(f"{name}に負の値は入力できません。")
    return number


@dataclass(frozen=True)
class BattingLine:
    """打撃成績。打率・出塁率・長打率・OPS の算出責務を持つ。

    これまで views.py の ORM アノテーションとして SQL に埋め込まれていたため、
    DB を用意しないと検証できなかった計算をここに移した。
    """

    at_bats: int = 0
    singles: int = 0
    doubles: int = 0
    triples: int = 0
    home_runs: int = 0
    runs_batted_in: int = 0
    walks: int = 0
    hit_by_pitch: int = 0
    sacrifice_flies: int = 0

    def __post_init__(self) -> None:
        for field_name, label in (
            ("at_bats", "打数"),
            ("singles", "単打"),
            ("doubles", "二塁打"),
            ("triples", "三塁打"),
            ("home_runs", "本塁打"),
            ("runs_batted_in", "打点"),
            ("walks", "四球"),
            ("hit_by_pitch", "死球"),
            ("sacrifice_flies", "犠飛"),
        ):
            object.__setattr__(self, field_name, _require_non_negative(label, getattr(self, field_name)))

        if self.hits > self.at_bats:
            raise InvalidStatValue(f"安打数（{self.hits}）が打数（{self.at_bats}）を超えています。")

    @property
    def hits(self) -> int:
        """安打数。単打＋二塁打＋三塁打＋本塁打。"""
        return self.singles + self.doubles + self.triples + self.home_runs

    @property
    def total_bases(self) -> int:
        """塁打数。"""
        return self.singles + self.doubles * 2 + self.triples * 3 + self.home_runs * 4

    @property
    def plate_appearances_for_obp(self) -> int:
        """出塁率の分母。打数＋四球＋死球＋犠飛。"""
        return self.at_bats + self.walks + self.hit_by_pitch + self.sacrifice_flies

    @property
    def plate_appearances(self) -> int:
        """打席数。規定打席の判定に使う。

        本来は犠打も含むが、このアプリでは記録していないため、
        記録している項目（打数・四球・死球・犠飛）の合計とする。
        """
        return self.plate_appearances_for_obp

    @property
    def batting_average(self) -> float:
        """打率。"""
        if self.at_bats == 0:
            return 0.0
        return self.hits / self.at_bats

    @property
    def on_base_percentage(self) -> float:
        """出塁率（OBP）。"""
        denominator = self.plate_appearances_for_obp
        if denominator == 0:
            return 0.0
        return (self.hits + self.walks + self.hit_by_pitch) / denominator

    @property
    def slugging_percentage(self) -> float:
        """長打率（SLG）。"""
        if self.at_bats == 0:
            return 0.0
        return self.total_bases / self.at_bats

    @property
    def ops(self) -> float:
        """OPS。出塁率＋長打率。"""
        return self.on_base_percentage + self.slugging_percentage

    @property
    def isolated_power(self) -> float:
        """長打力（IsoP）。長打率 − 打率。

        単打を差し引くことで「安打のうちどれだけ長打だったか」だけが残る。
        打率が高いだけの打者と、一発のある打者を区別するために使う。
        """
        return self.slugging_percentage - self.batting_average

    def ops_plus(self, league_ops: float) -> float:
        """OPS+。リーグ平均の OPS を100とした指数。

        リーグによって得点環境（投高打低か打高投低か）が異なるため、
        OPS の素点だけではリーグ間・シーズン間を比べられない。
        リーグ平均に対する比率にすることで、環境の違いを均して比べられる。
        打席が無ければ比べる相手がいないため 0。
        """
        if self.at_bats == 0 or league_ops == 0:
            return 0.0
        return self.ops / league_ops * 100

    def __add__(self, other: BattingLine) -> BattingLine:
        """試合ごとの成績を積み上げて通算にする。

        率（打率・OPS など）は足し合わせず、合算した実数から計算し直す。
        率の平均は正しい率にならないため。
        """
        if not isinstance(other, BattingLine):
            return NotImplemented
        return BattingLine(
            at_bats=self.at_bats + other.at_bats,
            singles=self.singles + other.singles,
            doubles=self.doubles + other.doubles,
            triples=self.triples + other.triples,
            home_runs=self.home_runs + other.home_runs,
            runs_batted_in=self.runs_batted_in + other.runs_batted_in,
            walks=self.walks + other.walks,
            hit_by_pitch=self.hit_by_pitch + other.hit_by_pitch,
            sacrifice_flies=self.sacrifice_flies + other.sacrifice_flies,
        )

    @classmethod
    def total(cls, lines) -> BattingLine:
        """複数試合の合計。"""
        result = cls()
        for line in lines:
            result = result + line
        return result


@dataclass(frozen=True)
class PitchingLine:
    """投球成績。防御率・WHIP・奪三振率・FIP の算出責務を持つ。"""

    innings: InningsPitched = InningsPitched(outs=0)
    wins: int = 0
    losses: int = 0
    saves: int = 0
    earned_runs: int = 0
    strikeouts: int = 0
    hits_allowed: int = 0
    walks_allowed: int = 0
    # FIP を求めるには被本塁打と与死球が要る。被安打の内訳（本塁打）と
    # 与四球とは別の事実なので、それぞれ独立して記録する
    home_runs_allowed: int = 0
    hit_by_pitch_allowed: int = 0
    # ホールド。日本プロ野球では公式記録で、セーブが記録される状況で登板し、
    # リードを保ったまま次の投手に引き継いだ救援投手に付く
    holds: int = 0
    # 先発登板数と、救援での勝利。HP（ホールドポイント）は
    # ホールド＋救援勝利で決まるため、勝利のうち救援ぶんを分けて持つ
    starts: int = 0
    relief_wins: int = 0

    # FIP の重み。本塁打・与四球死球・奪三振が失点にどれだけ効くかの係数で、
    # 野球の指標として定まった値のためドメインに置く
    FIP_HOME_RUN_WEIGHT = 13
    FIP_WALK_WEIGHT = 3
    FIP_STRIKEOUT_WEIGHT = 2

    # ERA+ の表示上の上限。自責点0（防御率0）だとリーグ平均との比率が
    # 無限大になり、少ない登板でも際限なく大きな値になってしまうため、
    # 表示に耐える値で頭打ちにする。実力の順序（自責点0どうしは同値）は保たれる
    ERA_PLUS_CAP = 300.0

    def __post_init__(self) -> None:
        if not isinstance(self.innings, InningsPitched):
            raise InvalidInningsPitched("innings には InningsPitched を渡してください。")
        for field_name, label in (
            ("wins", "勝利"),
            ("losses", "敗戦"),
            ("saves", "セーブ"),
            ("earned_runs", "自責点"),
            ("strikeouts", "奪三振"),
            ("hits_allowed", "被安打"),
            ("walks_allowed", "与四球"),
            ("home_runs_allowed", "被本塁打"),
            ("hit_by_pitch_allowed", "与死球"),
            ("holds", "ホールド"),
            ("starts", "先発登板"),
            ("relief_wins", "救援勝利"),
        ):
            object.__setattr__(self, field_name, _require_non_negative(label, getattr(self, field_name)))

        if self.home_runs_allowed > self.hits_allowed:
            raise InvalidStatValue(
                f"被本塁打（{self.home_runs_allowed}）が被安打（{self.hits_allowed}）を超えています。"
            )

        if self.relief_wins > self.wins:
            raise InvalidStatValue(f"救援勝利（{self.relief_wins}）が勝利（{self.wins}）を超えています。")

    @property
    def _outs(self) -> int:
        return self.innings.outs

    @property
    def earned_run_average(self) -> float:
        """防御率（ERA）。自責点 × 9 ÷ 投球回。"""
        if self._outs == 0:
            return 0.0
        return self.earned_runs * 27.0 / self._outs

    @property
    def whip(self) -> float:
        """WHIP。（被安打＋与四球）÷ 投球回。"""
        if self._outs == 0:
            return 0.0
        return (self.hits_allowed + self.walks_allowed) * 3.0 / self._outs

    @property
    def strikeouts_per_nine(self) -> float:
        """奪三振率（K/9）。奪三振 × 9 ÷ 投球回。"""
        if self._outs == 0:
            return 0.0
        return self.strikeouts * 27.0 / self._outs

    @property
    def walks_per_nine(self) -> float:
        """与四球率（BB/9）。与四球 × 9 ÷ 投球回。

        死球は含めない。四球は投手の制御そのものを表す記録で、
        BB/9 という指標も四球だけを分子に取る。
        """
        if self._outs == 0:
            return 0.0
        return self.walks_allowed * 27.0 / self._outs

    @property
    def fip_base(self) -> float:
        """FIP の素点。(13×被本塁打 + 3×(与四球+与死球) − 2×奪三振) ÷ 投球回。

        FIP は「野手の守備に左右されない結果（本塁打・四死球・三振）だけで
        投手を評価する」指標。防御率と同じ尺度で読めるようにリーグごとの定数を
        足して仕上げるが、その定数はリーグ全体の成績から決まるため、
        1人ぶんの記録だけでは確定できない。ここでは定数を足す前までを求め、
        リーグとの突き合わせは fip() に委ねる。
        """
        if self._outs == 0:
            return 0.0
        numerator = (
            self.FIP_HOME_RUN_WEIGHT * self.home_runs_allowed
            + self.FIP_WALK_WEIGHT * (self.walks_allowed + self.hit_by_pitch_allowed)
            - self.FIP_STRIKEOUT_WEIGHT * self.strikeouts
        )
        return numerator * 3.0 / self._outs

    def fip(self, constant: float) -> float:
        """FIP。素点にリーグの定数を足したもの。

        constant はリーグの得点環境に合わせる補正で、
        domain.services.fip_constant() が求める。未登板なら 0。
        """
        if self._outs == 0:
            return 0.0
        return self.fip_base + constant

    @property
    def hold_points(self) -> int:
        """HP（ホールドポイント）。ホールド＋救援勝利。

        日本プロ野球の救援投手の指標。ホールドだけでは「リードを守って
        引き継いだ」働きしか見えず、逆転して勝利投手になった登板が抜け落ちる。
        """
        return self.holds + self.relief_wins

    def era_plus(self, league_era: float) -> float:
        """ERA+。リーグ平均防御率 ÷ 自身の防御率 × 100。高いほど良い（FIP と逆）。

        防御率は低いほど良いが、指数はどの指標でも「大きいほど良い」に
        揃えたいため、比率を反転させる。未登板、またはリーグに比べる
        相手がいなければ 0。自責点0は ERA_PLUS_CAP で頭打ちにする。
        """
        if self._outs == 0 or league_era == 0:
            return 0.0
        if self.earned_run_average == 0:
            return self.ERA_PLUS_CAP
        return min(league_era / self.earned_run_average * 100, self.ERA_PLUS_CAP)

    def __add__(self, other: PitchingLine) -> PitchingLine:
        """試合ごとの成績を積み上げて通算にする。率は合算後に計算し直す。"""
        if not isinstance(other, PitchingLine):
            return NotImplemented
        return PitchingLine(
            innings=self.innings + other.innings,
            wins=self.wins + other.wins,
            losses=self.losses + other.losses,
            saves=self.saves + other.saves,
            earned_runs=self.earned_runs + other.earned_runs,
            strikeouts=self.strikeouts + other.strikeouts,
            hits_allowed=self.hits_allowed + other.hits_allowed,
            walks_allowed=self.walks_allowed + other.walks_allowed,
            home_runs_allowed=self.home_runs_allowed + other.home_runs_allowed,
            hit_by_pitch_allowed=self.hit_by_pitch_allowed + other.hit_by_pitch_allowed,
            holds=self.holds + other.holds,
            starts=self.starts + other.starts,
            relief_wins=self.relief_wins + other.relief_wins,
        )

    @classmethod
    def total(cls, lines) -> PitchingLine:
        """複数試合の合計。"""
        result = cls()
        for line in lines:
            result = result + line
        return result


@dataclass(frozen=True)
class Season:
    """シーズン（年）。

    チームの成績に時間軸を与える。同じチームでも年ごとに別の記録を持つ。
    """

    year: int

    MIN_YEAR = 1900
    MAX_YEAR = 2100

    def __post_init__(self) -> None:
        try:
            year = int(self.year)
        except (TypeError, ValueError):
            raise InvalidSeason("シーズンは西暦の数値で入力してください。") from None

        if year != self.year:
            object.__setattr__(self, "year", year)

        if not (self.MIN_YEAR <= year <= self.MAX_YEAR):
            raise InvalidSeason(f"シーズンは {self.MIN_YEAR}〜{self.MAX_YEAR} の範囲で入力してください。")

    def __str__(self) -> str:
        return f"{self.year}年"


@dataclass(frozen=True)
class LineScore:
    """イニングスコア。回ごとの得点。

    勝敗・セーブ・ホールドは「継投した時点のスコア」で決まるため、最終得点だけでは
    決められない。回ごとの経過を持つことで、日本プロ野球の規則どおりに導ける。

    ビジターが表、ホームが裏に攻める。ホームが最終回を攻めずに終わる（サヨナラ勝ち
    以外でリードしている）場合、裏の得点は記録されないので長さが表と揃わないことがある。
    """

    away: tuple[int, ...] = ()
    home: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        for name in ("away", "home"):
            values = tuple(_require_non_negative("イニングの得点", value) for value in getattr(self, name))
            object.__setattr__(self, name, values)

    @property
    def innings(self) -> int:
        """記録されている回数。延長も含む。"""
        return max(len(self.away), len(self.home))

    @property
    def away_total(self) -> int:
        return sum(self.away)

    @property
    def home_total(self) -> int:
        return sum(self.home)

    @property
    def is_empty(self) -> bool:
        return self.innings == 0

    def runs_in(self, inning: int, *, home: bool) -> int:
        """指定の回の得点。記録が無ければ 0。"""
        values = self.home if home else self.away
        index = inning - 1
        return values[index] if 0 <= index < len(values) else 0

    def score_after(self, inning: int, *, bottom: bool) -> tuple[int, int]:
        """指定の半回を終えた時点の (ビジター, ホーム) の得点。

        bottom が False なら表を終えた時点（ホームはまだその回を攻めていない）。
        """
        away = sum(self.away[:inning])
        home = sum(self.home[: inning if bottom else inning - 1])
        return away, home

    def matches(self, away_total: int, home_total: int) -> bool:
        """合計が最終得点と一致するか。ずれた入力を弾くために使う。"""
        return self.away_total == away_total and self.home_total == home_total


@dataclass(frozen=True)
class TeamRecord:
    """チームの年間成績（勝敗）。

    勝率は日本プロ野球の規則にならい 勝 ÷ (勝 + 敗) で求める。
    引分は分母に含めない。全て引分なら勝率は 0 とする。
    """

    wins: int = 0
    losses: int = 0
    ties: int = 0

    def __post_init__(self) -> None:
        for field_name, label in (
            ("wins", "勝"),
            ("losses", "敗"),
            ("ties", "分"),
        ):
            object.__setattr__(self, field_name, _require_non_negative(label, getattr(self, field_name)))

    @property
    def games_played(self) -> int:
        """試合数。引分を含む。"""
        return self.wins + self.losses + self.ties

    @property
    def decisions(self) -> int:
        """勝敗のついた試合数。勝率の分母。"""
        return self.wins + self.losses

    @property
    def winning_percentage(self) -> float:
        if self.decisions == 0:
            return 0.0
        return self.wins / self.decisions

    def games_behind(self, leader: TeamRecord) -> float:
        """首位とのゲーム差。((首位の勝 - 勝) + (敗 - 首位の敗)) ÷ 2。"""
        diff = (leader.wins - self.wins) + (self.losses - leader.losses)
        return diff / 2


class Handedness(Enum):
    """投打の左右。"""

    RIGHT = "右"
    LEFT = "左"
    BOTH = "両"

    @property
    def label(self) -> str:
        return self.value

    @classmethod
    def from_label(cls, label: str) -> Handedness | None:
        """未設定（空）は None を返す。全選手に入力を強いないため。"""
        if not label:
            return None
        for item in cls:
            if item.value == label:
                return item
        raise InvalidProfile(f"「{label}」は投打として認識できません。")

    @classmethod
    def labels(cls) -> list[str]:
        return [item.value for item in cls]


@dataclass(frozen=True)
class Profile:
    """選手のプロフィール。

    どの項目も任意。分かっているものだけ埋められるようにする。
    年齢は生年月日から求めるので保持しない（保持すると翌年ずれる）。
    """

    birth_date: date | None = None
    throws: Handedness | None = None
    bats: Handedness | None = None
    height_cm: int | None = None
    weight_kg: int | None = None
    birthplace: str = ""
    debut_year: int | None = None
    # プロ入り前の経歴。日本では 高校 →（大学 または 社会人）→ プロ が多いが、
    # 高校からプロ、大学から社会人を経てプロなど、順路はさまざま
    high_school: str = ""
    university: str = ""
    corporate_team: str = ""
    # 表示用の記述情報。既存の birthplace と同じ扱いで、検証は行わない
    nationality: str = ""
    name_kana: str = ""  # 氏名のよみがな（カタカナ）
    back_name: str = ""  # ユニフォーム背面の表記（例: T.YAMADA）
    # 外国人枠の判定に使う唯一の出典。nationality（実際の国籍という事実）とは
    # 別の概念（帰化選手など、枠制度上の扱いは国籍と一致しないことがある）
    is_foreign_player: bool = False

    def __post_init__(self) -> None:
        for name, label, upper in (
            ("height_cm", "身長", 300),
            ("weight_kg", "体重", 300),
        ):
            value = getattr(self, name)
            if value is None:
                continue
            try:
                number = int(value)
            except (TypeError, ValueError):
                raise InvalidProfile(f"{label}は数値で入力してください。") from None
            if not (0 < number <= upper):
                raise InvalidProfile(f"{label}の値が現実的ではありません。")
            object.__setattr__(self, name, number)

        if self.debut_year is not None:
            season = Season(self.debut_year)  # 年としての妥当性はここで検査する
            object.__setattr__(self, "debut_year", season.year)

    def age(self, as_of: date) -> int | None:
        """指定日時点の満年齢。生年月日が未設定なら None。"""
        if self.birth_date is None:
            return None
        if as_of < self.birth_date:
            raise InvalidProfile("生年月日より前の日付では年齢を求められません。")
        had_birthday = (as_of.month, as_of.day) >= (self.birth_date.month, self.birth_date.day)
        return as_of.year - self.birth_date.year - (0 if had_birthday else 1)

    @property
    def throws_bats(self) -> str:
        """「右投左打」のような表記。片方でも欠けていれば空。"""
        if self.throws is None or self.bats is None:
            return ""
        return f"{self.throws.label}投{self.bats.label}打"

    @property
    def amateur_career(self) -> list[tuple[str, str]]:
        """プロ入り前の経歴を、通った順に並べる。

        入力されているものだけを返す。高校からそのままプロ、
        大学を経ずに社会人へ、といった順路にも対応する。
        """
        return [
            (label, value)
            for label, value in (
                ("高校", self.high_school),
                ("大学", self.university),
                ("社会人", self.corporate_team),
            )
            if value
        ]

    @property
    def amateur_path(self) -> str:
        """「○○高校 → ○○大学」のような1行表記。"""
        return " → ".join(value for _, value in self.amateur_career)

    @property
    def is_empty(self) -> bool:
        return not any(
            [
                self.birth_date,
                self.throws,
                self.bats,
                self.height_cm,
                self.weight_kg,
                self.birthplace,
                self.debut_year,
                self.high_school,
                self.university,
                self.corporate_team,
                self.nationality,
            ]
        )


@dataclass(frozen=True)
class StadiumProfile:
    """球場の属性。所在地・収容人数・グラウンドの種類・屋根。"""

    city: str = ""
    capacity: int | None = None
    surface: str = ""
    opened_year: int | None = None
    roof: str = ""

    SURFACES = ("天然芝", "人工芝", "土")
    # 屋根の有無は「雨天中止があり得るか」を分ける。開閉式は屋根を閉じれば
    # ドームと同じように試合できるため、屋外とは別に扱う
    ROOFS = ("屋外", "ドーム", "開閉式屋根")

    def __post_init__(self) -> None:
        if self.capacity is not None:
            try:
                capacity = int(self.capacity)
            except (TypeError, ValueError):
                raise InvalidProfile("収容人数は数値で入力してください。") from None
            if capacity < 0:
                raise InvalidProfile("収容人数に負の値は入力できません。")
            object.__setattr__(self, "capacity", capacity)

        if self.surface and self.surface not in self.SURFACES:
            raise InvalidProfile(f"「{self.surface}」はグラウンドの種類として認識できません。")

        if self.roof and self.roof not in self.ROOFS:
            raise InvalidProfile(f"「{self.roof}」は屋根の種類として認識できません。")

        if self.opened_year is not None:
            object.__setattr__(self, "opened_year", Season(self.opened_year).year)

    @property
    def is_covered(self) -> bool:
        """屋根で覆える球場か。天候に左右されないかの判断に使う。

        未設定のときは「分からない」ではなく False とする。覆えると
        言い切れないため。
        """
        return self.roof in ("ドーム", "開閉式屋根")


def format_average(value: float) -> str:
    """打率・出塁率を野球慣例の「.333」形式で表す。"""
    return f"{value:.3f}".lstrip("0") if value < 1 else f"{value:.3f}"


def ensure_quota_not_exceeded(count: int, limit: int | None, message: str) -> None:
    """人数が上限を超えていないか確認する。limit が None なら無制限。

    外国人選手の登録枠・試合出場枠のどちらも、この同じ規則で判定する。
    """
    if limit is not None and count > limit:
        raise ForeignPlayerQuotaExceeded(message)
