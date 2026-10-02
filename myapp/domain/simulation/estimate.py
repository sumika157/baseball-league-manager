"""実成績から初期能力を推定する。`odds.py` の「能力 → 個人の率」の**逆写像**。

    rate = 基準値 × exp(β × (能力 − 50) / 15)   ←→   能力 = 50 + 15 / β × ln(rate / 基準値)

能力は入力で成績は出力なので、成績から能力を作り直すのは**世界の作成時の1回だけ**（設計書 3.2）。
推定した値は能力として保存され、以降は成績に引きずられない。

**収縮推定。** 少ない打席の率は偶然の振れが大きい。事象が起きた回数に「基準値で起きた
ことにする k 回ぶん」を足して平均へ寄せる。

    率 = (起きた回数 + k × 基準値) ÷ (機会 + k)

k は「安定するまでの機会数」（その機会数で、成績の半分が実力・半分が偶然になる）。
野手は三振 60・四球 120・本塁打 170・インプレーの安打 800 を目安にした（`StabilityPoints`）。
機会が 0 のときの寄せ先は、出場の少なさに応じて平均（50）より低い（`REPLACEMENT_GAP`）。

**項目ごとの推定の根拠**（使う実数 → 逆にする `odds.py` / `baserunning.py` の β）

- ミート: 三振 / 打数 と インプレーの安打 / インプレー → `contact_strikeout`・`contact_in_play_hit`。
  2つの根拠は情報量（β² × 起きた回数）で重みづけて合わせる
- パワー: 本塁打 / 三振でない打数 と 二塁打 / 安打 → `power_home_run`・`power_double`
- 選球眼: 四球 / 打席（死球を除く） → `eye_walk`
- 走力: 盗塁の企図 / 出塁、盗塁の成功率、三塁打 / 安打 → 盗塁の β（`BaserunningRules`）と `speed_triple`。
  企図と成功は**リーグ全体の率**（`LeagueNorms`）を 50 の目印にする（企図は「走者が一塁にいて二塁が空く」
  機会の数え方に依存し、基準値を定数にできないため）。シミュレーションは企図も成功もオッズを動かす
  （`baserunning.adjusted`）ので、どちらも odds の逆写像で読む。ただし企図の分母は「出塁」で、
  シミュレーションの「走者が一塁にいて二塁が空き、2アウト未満」より広い。**率の水準が違うだけで向きは同じ**
  なので、リーグ全体の率で割った相対値として使い、厳密な逆写像とは言わない
- 守備力: 失策 / 守備機会（`FieldingLine`） → `fielding.ERROR_SKILL_BETA`。守備位置の区分ごとの
  リーグの失策率が目印で、登録位置の傾向を事前の平均にする。**これも厳密な逆写像ではない**:
  シミュレーションは失策のたびに守備者を重みで選ぶ（`fielding.draw_error`。他の守備者の守備力にも
  影響される）ので、個人の失策率が `exp(β × (能力 − 50) / 15)` に比例するのは近似（βの向きと
  大きさの目安）。水準は元の実データの BABIP に合わせて偏りを決めて補っている
- 球威: 奪三振 / 打数 → `stuff_strikeout`
- 制球: 与四球 / 対戦打者（死球を除く） → `control_walk`
- 一発回避: 被本塁打 / 三振でない打数 → `avoidance_home_run`
- スタミナ: 先発登板数と1先発あたりの対戦打者数 → `manager.STARTER_BATTERS`。先発の割合で
  事前の平均（先発 62・救援 38）を決める

対戦打者数は成績に無いので、`アウト + 被安打 + 与四球 + 与死球` で数える（失策出塁と併殺の
ぶん、1〜2%少なめに出る。率が少し高く出るだけで、能力にして1点未満）。
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from ..value_objects import BattingLine, FieldingLine, PitchingLine, Position
from .baseline import NPB, LeagueBaseline
from .baserunning import DEFAULT_RULES, BaserunningRules
from .fielding import ERROR_SKILL_BETA
from .manager import STARTER_BATTERS, STARTER_BATTERS_PER_STAMINA
from .odds import DEFAULT_SENSITIVITY, MAX_RATE, MIN_RATE, RATING_UNIT, RatingSensitivity
from .randomness import GameRandom, weighted_index
from .ratings import (
    AVERAGE_RATING,
    RATING_MAX,
    RATING_MIN,
    BatterRatings,
    GrowthType,
    PitcherRatings,
)


@dataclass(frozen=True)
class StabilityPoints:
    """安定するまでの機会数（収縮推定の k）。単位は項目ごとの「機会」（コメント参照）。"""

    # 野手（設計書 3.2 の目安）
    strikeout: float = 60.0  # 打数
    walk: float = 120.0  # 打席（死球を除く）
    home_run: float = 170.0  # 三振でなかった打数
    in_play_hit: float = 800.0  # インプレー（打数 − 三振 − 本塁打）
    # 設計書に無い項目。分母が小さい・起きにくい事象ほど長くかかる
    double_share: float = 300.0  # 安打（本塁打を除く）
    triple_share: float = 600.0  # 二塁打でなかった安打（本塁打を除く）
    steal_attempt: float = 100.0  # 出塁（単打 + 四球 + 死球）
    steal_success: float = 60.0  # 盗塁の企図
    error: float = 600.0  # 守備機会（失策は年に数回しか起きず、守備位置による差も大きい）
    # 投手。被本塁打は打球の質より運の影響が大きく、野手より長くかかる（公開されている研究に
    # 基づく目安を、このアプリの規模に丸めた）
    pitcher_strikeout: float = 70.0  # 打数
    pitcher_walk: float = 170.0  # 対戦打者（死球を除く）
    pitcher_home_run: float = 1000.0  # 三振でなかった打数
    # スタミナ
    starter_role: float = 3.0  # 先発登板数。この回数で「先発」の事前の平均が半分入れ替わる
    batters_per_start: float = 10.0  # 先発登板数。この回数で1先発あたりの対戦打者数が半分効く


DEFAULT_STABILITY = StabilityPoints()

# **出場の少ない選手は、1軍の平均（50）より低いところへ寄せる。** 能力 50 は「1軍の平均」で、リーグの
# 率は主に1軍の選手が作っている。出場の無い選手を 50 へ寄せると、2軍の控えが1軍の平均並みに見え、
# 1軍の登録（能力順）に紛れ込んで水準が狂う（本塁打が増え、安打が減った）。`samples.spread_club` が
# 全員の平均を 44.5 に下げて水準を戻したのと同じ理由で、出場が無いときの事前の平均を 50 − 6 にする。
# 出場が `REGULAR_*` に届くまで、足りないぶんに比例して 50 へ近づく。
REPLACEMENT_GAP = 6.0
REGULAR_PLATE_APPEARANCES = 400  # 規定打席（3.1 × 143）に近い、1軍の主力の目安
REGULAR_PITCHER_OUTS = 180  # 60 回。先発でも抑えでも1軍の戦力と言える投球回

# 守備力の事前の平均。登録位置ごとの傾向（捕手・内野手は守備の重みが大きく、指名打者は守備が売りでない。
# `samples.spread_club` の偏りと同じ向き）に、先発の選択で上振れするぶんを引いた水準を足す。
# チームの守備力は、守備位置ごとに守備の良い選手が選ばれて 50 より上になり、BABIP が基準値より下がる。
# 水準（BABIP）が元の実データに合うまで下げた（実データ 2 リーグ・8 リーグの実測で決めた）
_FIELDING_BIAS: dict[Position, float] = {
    Position.CATCHER: 4.0,
    Position.INFIELDER: 2.0,
    Position.OUTFIELDER: 0.0,
    Position.DESIGNATED_HITTER: -8.0,
}
REGULAR_FIELDING_LEVEL = 45.0
# 成績が全く無い投手のスタミナ。先発 62・救援 38（`samples.spread_club`）の中間
UNKNOWN_STAMINA = 45
STARTER_STAMINA = 62
RELIEVER_STAMINA = 38
# 1先発あたりの対戦打者数から出すスタミナの幅（外れ値で飛ばないように）
_STAMINA_EVIDENCE_RANGE = (20.0, 90.0)

# 成長型の事前の分布（早熟 / 普通 / 晩成）。隠し値なので、成績からは決められない
GROWTH_PRIOR = {GrowthType.EARLY: 0.20, GrowthType.NORMAL: 0.60, GrowthType.LATE: 0.20}
# 若い選手は、すでに能力が高ければ早熟、低ければ晩成の可能性が高い（同じ年齢で今の能力が違う理由として）
GROWTH_TILT_BELOW_AGE = 26
GROWTH_TILT = 0.4
_GROWTH_STRENGTH_LIMIT = 2.0


@dataclass(frozen=True)
class CareerRecord:
    """推定の材料。ひとりの選手の通算成績と、登録位置・年齢。"""

    player_id: int
    position: Position
    batting: BattingLine = field(default_factory=BattingLine)
    pitching: PitchingLine = field(default_factory=PitchingLine)
    fielding: FieldingLine = field(default_factory=FieldingLine)
    age: int | None = None
    # 成長型の乱数の種になる選手の id。世界の作成では分岐元の選手の id を渡す（写した先の id は
    # 世界ごとに違うので、同じシード・同じ分岐元から作った世界が同じ成長型になるように）。省略は player_id
    seed_player_id: int | None = None


@dataclass(frozen=True)
class LeagueNorms:
    """基準値が定数で決まらない項目の、リーグ全体の実測。

    盗塁の企図と失策は、起きやすさが「走者が出るたびに二塁が空くか」「どの打球が飛ぶか」といった
    状況に依存し、`LeagueBaseline` の定数にできない。推定に使う選手全員の成績から求めて、
    その平均を能力 50 の目印にする。0 は「求められなかった」（その項目の根拠にしない）。
    """

    steal_attempts_per_reach: float = 0.0
    steal_success: float = 0.0
    error_rate: Mapping[Position, float] = field(default_factory=dict)

    # 求めるのに最低限必要な量。これに満たないリーグでは、その項目を根拠にしない
    MIN_STEAL_ATTEMPTS = 30
    MIN_CHANCES = 500

    @classmethod
    def of(cls, records: Iterable[CareerRecord]) -> LeagueNorms:
        attempts = reaches = stolen = 0
        chances: dict[Position, int] = {}
        errors: dict[Position, int] = {}
        for record in records:
            batting = record.batting
            attempts += batting.stolen_bases + batting.caught_stealing
            stolen += batting.stolen_bases
            reaches += _reaches(batting)
            if record.position.is_pitcher:
                continue
            chances[record.position] = chances.get(record.position, 0) + record.fielding.total_chances
            errors[record.position] = errors.get(record.position, 0) + record.fielding.errors

        has_steals = attempts >= cls.MIN_STEAL_ATTEMPTS and reaches > 0 and 0 < stolen < attempts
        return cls(
            steal_attempts_per_reach=attempts / reaches if has_steals else 0.0,
            steal_success=stolen / attempts if has_steals else 0.0,
            error_rate={
                position: errors[position] / total
                for position, total in chances.items()
                if total >= cls.MIN_CHANCES and errors[position] > 0
            },
        )


@dataclass(frozen=True)
class _Evidence:
    """ひとつの項目から読み取った、能力の 50 からのずれと、その確かさ（情報量）。"""

    offset: float
    weight: float


def _reaches(batting: BattingLine) -> int:
    """一塁に出た回数の目安。盗塁を試みる機会の数え方（単打 + 四球 + 死球）。"""
    return batting.singles + batting.walks + batting.hit_by_pitch


def _prior_gap(opportunities: int, regular: int) -> float:
    """出場の少なさに応じた、事前の平均の 50 からのずれ（0 以下）。`REPLACEMENT_GAP` の説明を参照。"""
    return -REPLACEMENT_GAP * (1.0 - min(1.0, max(0, opportunities) / regular))


def _evidence(
    events: int,
    trials: int,
    base: float,
    beta: float,
    stability: float,
    *,
    prior_offset: float = 0.0,
    odds_scale: bool = False,
) -> _Evidence:
    """収縮した率から、能力のずれを読む。`scaled_rate`（odds_scale なら `adjusted`）の逆。

    寄せる先（事前の率）は、基準値を能力 `50 + prior_offset` で読み替えた率。機会が 0 なら
    ずれは `prior_offset` に一致し、機会が増えるほど実際の率に寄る。

    情報量は、能力が 1 点動いたときに起きた回数の見込みがどれだけ変わるか（β²に比例）。
    β の大きい項目、機会の多い項目ほど重い。
    """
    trials = max(trials, 0)
    events = min(max(events, 0), trials)
    factor = math.exp(beta * prior_offset / RATING_UNIT)
    if odds_scale:
        prior_odds = base / (1.0 - base) * factor
        prior_rate = prior_odds / (1.0 + prior_odds)
    else:
        prior_rate = base * factor
    rate = (events + stability * prior_rate) / (trials + stability)
    rate = min(max(rate, MIN_RATE), MAX_RATE)
    if odds_scale:
        offset = RATING_UNIT / beta * (math.log(rate / (1.0 - rate)) - math.log(base / (1.0 - base)))
        spread = rate * (1.0 - rate)
    else:
        offset = RATING_UNIT / beta * math.log(rate / base)
        spread = rate
    return _Evidence(offset=offset, weight=(beta / RATING_UNIT) ** 2 * spread * (trials + stability))


def _combine(evidences: Sequence[_Evidence]) -> float:
    """複数の根拠を、情報量で重みづけて平均する。根拠が無ければ 0（= 平均）。"""
    total = sum(e.weight for e in evidences)
    if total <= 0.0:
        return 0.0
    return sum(e.offset * e.weight for e in evidences) / total


def _to_rating(value: float) -> int:
    return min(RATING_MAX, max(RATING_MIN, round(value)))


def estimate_batter_ratings(
    line: BattingLine,
    fielding: FieldingLine,
    position: Position,
    norms: LeagueNorms,
    *,
    growth: GrowthType = GrowthType.NORMAL,
    baseline: LeagueBaseline = NPB,
    sensitivity: RatingSensitivity = DEFAULT_SENSITIVITY,
    rules: BaserunningRules = DEFAULT_RULES,
    stability: StabilityPoints = DEFAULT_STABILITY,
) -> BatterRatings:
    """野手の通算成績から能力を推定する。

    出場が `REGULAR_PLATE_APPEARANCES` に届かない選手は、足りないぶんだけ平均より低い方へ寄る。
    """
    b, s, k = baseline, sensitivity, stability
    gap = _prior_gap(line.plate_appearances, REGULAR_PLATE_APPEARANCES)
    at_bats = line.at_bats
    not_strikeout = at_bats - line.strikeouts
    in_play = not_strikeout - line.home_runs
    non_home_run_hits = line.hits - line.home_runs

    contact = _combine(
        [
            _evidence(
                line.strikeouts,
                at_bats,
                b.strikeout_given_at_bat,
                s.contact_strikeout,
                k.strikeout,
                prior_offset=gap,
            ),
            _evidence(non_home_run_hits, in_play, b.babip, s.contact_in_play_hit, k.in_play_hit, prior_offset=gap),
        ]
    )
    power = _combine(
        [
            _evidence(
                line.home_runs,
                not_strikeout,
                b.home_run_given_not_strikeout,
                s.power_home_run,
                k.home_run,
                prior_offset=gap,
            ),
            _evidence(
                line.doubles, non_home_run_hits, b.double_share, s.power_double, k.double_share, prior_offset=gap
            ),
        ]
    )
    eye = _evidence(
        line.walks,
        line.plate_appearances - line.hit_by_pitch,
        b.walk_given_not_hit_by_pitch,
        s.eye_walk,
        k.walk,
        prior_offset=gap,
    ).offset

    speed_evidence = [
        _evidence(
            line.triples,
            non_home_run_hits - line.doubles,
            b.triple_given_not_double,
            s.speed_triple,
            k.triple_share,
            prior_offset=gap,
        )
    ]
    attempts = line.stolen_bases + line.caught_stealing
    if norms.steal_attempts_per_reach > 0.0:
        speed_evidence.append(
            _evidence(
                attempts,
                _reaches(line),
                norms.steal_attempts_per_reach,
                rules.speed_steal_attempt,
                k.steal_attempt,
                prior_offset=gap,
                odds_scale=True,
            )
        )
        speed_evidence.append(
            _evidence(
                line.stolen_bases,
                attempts,
                norms.steal_success,
                rules.speed_steal_success,
                k.steal_success,
                prior_offset=gap,
                odds_scale=True,
            )
        )

    # 守備力: 位置ごとの傾向を事前の平均にして、失策が少ない（多い）ぶんだけ動かす
    defense_prior = REGULAR_FIELDING_LEVEL - AVERAGE_RATING + _FIELDING_BIAS.get(position, 0.0) + gap
    defense_offset = defense_prior
    error_rate = norms.error_rate.get(position)
    if error_rate:
        defense_offset = _evidence(
            fielding.errors,
            fielding.total_chances,
            error_rate,
            ERROR_SKILL_BETA,
            k.error,
            prior_offset=defense_prior,
        ).offset

    return BatterRatings(
        contact=_to_rating(AVERAGE_RATING + contact),
        power=_to_rating(AVERAGE_RATING + power),
        eye=_to_rating(AVERAGE_RATING + eye),
        speed=_to_rating(AVERAGE_RATING + _combine(speed_evidence)),
        fielding=_to_rating(AVERAGE_RATING + defense_offset),
        growth=growth,
    )


def estimate_pitcher_ratings(
    line: PitchingLine,
    *,
    growth: GrowthType = GrowthType.NORMAL,
    baseline: LeagueBaseline = NPB,
    sensitivity: RatingSensitivity = DEFAULT_SENSITIVITY,
    stability: StabilityPoints = DEFAULT_STABILITY,
) -> PitcherRatings:
    """投手の通算成績から能力を推定する。

    投げた回が `REGULAR_PITCHER_OUTS` に届かない投手は、足りないぶんだけ平均より低い方へ寄る。
    """
    b, s, k = baseline, sensitivity, stability
    gap = _prior_gap(line.innings.outs, REGULAR_PITCHER_OUTS)
    faced = _batters_faced(line)
    at_bats = faced - line.walks_allowed - line.hit_by_pitch_allowed - round(b.sacrifice * faced)
    not_strikeout = at_bats - line.strikeouts

    stuff = _evidence(
        line.strikeouts,
        at_bats,
        b.strikeout_given_at_bat,
        s.stuff_strikeout,
        k.pitcher_strikeout,
        prior_offset=gap,
    )
    control = _evidence(
        line.walks_allowed,
        faced - line.hit_by_pitch_allowed,
        b.walk_given_not_hit_by_pitch,
        s.control_walk,
        k.pitcher_walk,
        prior_offset=gap,
    )
    avoidance = _evidence(
        line.home_runs_allowed,
        not_strikeout,
        b.home_run_given_not_strikeout,
        s.avoidance_home_run,
        k.pitcher_home_run,
        prior_offset=gap,
    )

    return PitcherRatings(
        stuff=_to_rating(AVERAGE_RATING + stuff.offset),
        control=_to_rating(AVERAGE_RATING + control.offset),
        home_run_avoidance=_to_rating(AVERAGE_RATING + avoidance.offset),
        stamina=_to_rating(_estimate_stamina(line, faced, k)),
        growth=growth,
    )


def _batters_faced(line: PitchingLine) -> int:
    """対戦打者数。成績に無いので、アウト + 被安打 + 与四球 + 与死球で数える（モジュールの説明を参照）。"""
    return line.innings.outs + line.hits_allowed + line.walks_allowed + line.hit_by_pitch_allowed


def _estimate_stamina(line: PitchingLine, faced: int, stability: StabilityPoints) -> float:
    """先発の割合で事前の平均を決め、先発の投げた長さがあれば寄せる。

    `manager.STARTER_BATTERS` の逆: 先発の受け持ち = 24 + 0.22 × (スタミナ − 50) 人。
    """
    if faced == 0:
        return float(UNKNOWN_STAMINA)
    starts = line.starts
    starter_share = starts / (starts + stability.starter_role)
    prior = RELIEVER_STAMINA + (STARTER_STAMINA - RELIEVER_STAMINA) * starter_share
    if starts == 0:
        return prior
    low, high = _STAMINA_EVIDENCE_RANGE
    observed = AVERAGE_RATING + (faced / starts - STARTER_BATTERS) / STARTER_BATTERS_PER_STAMINA
    observed = min(max(observed, low), high)
    weight = starts / (starts + stability.batters_per_start)
    return prior + weight * (observed - prior)


def draw_growth_type(rng: GameRandom, *, age: int | None, strength: float) -> GrowthType:
    """成長型を引く。隠し値で、成績からは分からない。

    `strength` は今の能力の高さ（50 を 0、10 点を 1 とする）。若い選手は、すでに能力が高ければ
    早熟、低ければ晩成の可能性を上げる。年齢が分からない・若くない選手は事前の分布のまま。
    `random()` を 1 回だけ使う。
    """
    weights = dict(GROWTH_PRIOR)
    if age is not None and age <= GROWTH_TILT_BELOW_AGE:
        limited = min(max(strength, -_GROWTH_STRENGTH_LIMIT), _GROWTH_STRENGTH_LIMIT)
        tilt = math.exp(GROWTH_TILT * limited)
        weights[GrowthType.EARLY] *= tilt
        weights[GrowthType.LATE] /= tilt
    order = list(weights)
    return order[weighted_index(rng, [weights[growth] for growth in order])]


def with_growth(ratings: BatterRatings | PitcherRatings, growth: GrowthType) -> BatterRatings | PitcherRatings:
    """成長型だけを差し替える。"""
    return replace(ratings, growth=growth)
