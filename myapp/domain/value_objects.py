"""値オブジェクト。

いずれも不変（frozen）で、生成時に自身の正当性を検証する。
「不正な値のインスタンスは存在しえない」状態を作ることで、
呼び出し側での検証漏れを構造的に防ぐ。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from enum import Enum

from .exceptions import (
    InvalidInningsPitched,
    InvalidJerseyNumber,
    InvalidPosition,
    InvalidStatValue,
)


class Position(Enum):
    """守備位置。

    これまで '投手' という文字列リテラルが models.py・views.py・テンプレート2枚に
    散在しており、テンプレートだけ '指名打者' が欠落して選手が投手に化けるバグが
    発生していた。守備位置の語彙はこの Enum を唯一の出典とする。
    """

    PITCHER = '投手'
    CATCHER = '捕手'
    INFIELDER = '内野手'
    OUTFIELDER = '外野手'
    DESIGNATED_HITTER = '指名打者'

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
            object.__setattr__(self, 'value', number)

        if not (self.MIN <= number <= self.MAX):
            raise InvalidJerseyNumber(
                f"背番号は {self.MIN}〜{self.MAX} の範囲で入力してください。"
            )

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
        if value is None or value == '':
            return cls.zero()

        try:
            notation = Decimal(str(value)).quantize(Decimal('0.1'), rounding=ROUND_DOWN)
        except Exception:
            raise InvalidInningsPitched(
                f"投球回として解釈できない値です: {value!r}"
            ) from None

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
            ('at_bats', '打数'), ('singles', '単打'), ('doubles', '二塁打'),
            ('triples', '三塁打'), ('home_runs', '本塁打'), ('runs_batted_in', '打点'),
            ('walks', '四球'), ('hit_by_pitch', '死球'), ('sacrifice_flies', '犠飛'),
        ):
            object.__setattr__(
                self, field_name, _require_non_negative(label, getattr(self, field_name))
            )

        if self.hits > self.at_bats:
            raise InvalidStatValue(
                f"安打数（{self.hits}）が打数（{self.at_bats}）を超えています。"
            )

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


@dataclass(frozen=True)
class PitchingLine:
    """投球成績。防御率・WHIP・奪三振率の算出責務を持つ。"""

    innings: InningsPitched = InningsPitched(outs=0)
    wins: int = 0
    losses: int = 0
    saves: int = 0
    earned_runs: int = 0
    strikeouts: int = 0
    hits_allowed: int = 0
    walks_allowed: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.innings, InningsPitched):
            raise InvalidInningsPitched("innings には InningsPitched を渡してください。")
        for field_name, label in (
            ('wins', '勝利'), ('losses', '敗戦'), ('saves', 'セーブ'),
            ('earned_runs', '自責点'), ('strikeouts', '奪三振'),
            ('hits_allowed', '被安打'), ('walks_allowed', '与四球'),
        ):
            object.__setattr__(
                self, field_name, _require_non_negative(label, getattr(self, field_name))
            )

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


def format_average(value: float) -> str:
    """打率・出塁率を野球慣例の「.333」形式で表す。"""
    return f"{value:.3f}".lstrip('0') if value < 1 else f"{value:.3f}"
