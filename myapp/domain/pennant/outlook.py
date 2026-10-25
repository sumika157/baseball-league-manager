"""見込み（#308）。`PEAK_AGE` 歳ごろの能力の見通しと、スカウトの見立て。**純粋な関数**で、DB にも Django にも触れない。

**見込みの伸びの出典はここだけ**:

- `item_growth_to_peak`: `PEAK_AGE` 歳までの項目ごとの普通の伸び（普通型・伸びしろは普通・ぶれなしで、
  平均的な能力で測る。成長の式 `aging.expected_change` の積み上げ）。
  ドラフトの候補の見込み（`growth_to_peak`）も、在籍の選手の見込み（`projected_ratings`）もこれを使う
- 伸びしろは伸びの大きさだけに効く（成長の式と同じ）ので、見込み ＝ 今の能力 ＋ 普通の伸び × **推定の**伸びしろの倍率。
  本当の伸びしろは隠し値で、画面にも AI にも見せない。見るのはスカウトの見立て（推定）だけ
- 物差しは使う側で違う: ドラフトの候補は `scouting_value`（スタミナを除く項目の平均。候補を見比べる総合）、
  在籍の選手は画面の「総合」（`overall_value`。野手は打撃、投手は抑える力）。伸びの出典は同じ1つ

**在籍の選手の見立て**（#308 P3。2026-10-25 ユーザー決定 Q1 A）: 見る球団のスカウトの伸びしろの推定は、
本当の値 ＋ 誤差の向き z（見る球団と選手で決まり、年に依らない）× `potential_sd / √在籍年数` を5刻み。
年ごとにぶれず、長く抱えるほど本当の値に近づく。保存しない（同じ材料なら何度導いても同じ）。

`ScoutingAccuracy`・丸め（`rounded_estimate`）はドラフトの候補の推定（`draft_meeting.scouting_report`）と共有する。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from functools import cache
from math import sqrt
from types import MappingProxyType

from ..simulation.estimate import draw_potential, with_potential
from ..simulation.randomness import game_seed, hash_normals, make_random
from ..simulation.ratings import RATING_MAX, RATING_MIN, GrowthType, PitcherRatings, Potential, RatingGrade
from ..simulation.samples import BATTER_MEAN, PITCHER_MEAN, mean_ratings
from ..value_objects import Position
from .aging import BATTER_CURVES, PITCHER_CURVES, expected_change
from .offseason_player import Ratings, overall_value

# 見込みを測る年齢（ふつう伸びが止まるころ）。これより若い選手だけ伸びを見込み、この年齢以上の見込みは今の能力
PEAK_AGE = 27
# スカウトの推定の誤差（項目ごとの標準偏差。点）と、推定を丸める刻み。誤差の大きさは球団ごとに変えられる形にしておく
# （#208 のスカウトへの投資でつなぐ。`ScoutingAccuracy`）
SCOUT_SD = 4.0
SCOUT_STEP = 5
# 伸びしろの推定の誤差（標準偏差。点。#308 P2）。今の能力（`SCOUT_SD`）より大きい
# （伸びしろは試合で見えず、見立てが外れやすい）。在籍の選手は在籍年数の平方根で割って縮める
POTENTIAL_SCOUT_SD = 10.0
# 伸びしろの無い選手に引く伸びしろの乱数の「年」。年に依らない種にして、どの年に締めても同じ値を引く
POTENTIAL_FILL_SEED_YEAR = 0


@dataclass(frozen=True)
class ScoutingAccuracy:
    """スカウトの推定の精度（誤差の標準偏差）。今の能力の項目は `sd`、伸びしろは `potential_sd`。
    いまは全球団 `SCOUT_SD`・`POTENTIAL_SCOUT_SD`。#208 で球団ごとの値をここに渡す。"""

    sd: float = SCOUT_SD
    potential_sd: float = POTENTIAL_SCOUT_SD


def rounded_estimate(value: float) -> int:
    """推定の値を `SCOUT_STEP` 刻みに丸めて 1〜100 に収める。"""
    return min(RATING_MAX, max(RATING_MIN, SCOUT_STEP * round(value / SCOUT_STEP)))


def estimated_potential(value: float) -> Potential:
    """伸びしろの推定（誤差を足した値）を5刻みにした伸びしろ。5〜100（下端を1にすると刻みから外れる）。"""
    return Potential(max(SCOUT_STEP, rounded_estimate(value)))


def judged_items(ratings: Ratings) -> list[str]:
    """ドラフトの候補を見比べる項目（スタミナを除く。`draft_meeting.scouting_value`）。"""
    return [name for name in ratings.LABELS if name != "stamina"]


@cache
def item_growth_to_peak(age: int, position: Position) -> Mapping[str, int]:
    """`age` 歳の選手が `PEAK_AGE` 歳になるまでに見込める、項目ごとの普通の伸び（点。`PEAK_AGE` 歳以上は0）。

    普通型・伸びしろは普通・ぶれなしで、位置の平均的な能力（`samples.mean_ratings`）を1年ずつ進めて測る。
    伸びしろは使う側が倍率で掛ける。
    """
    pitcher = position.is_pitcher
    current: Ratings = mean_ratings(
        mean=PITCHER_MEAN if pitcher else BATTER_MEAN, position=position, stamina_mean=45.0
    )
    curves = PITCHER_CURVES if pitcher else BATTER_CURVES
    values = {name: float(getattr(current, name)) for name in curves}
    for year_age in range(age, PEAK_AGE):
        for name, curve in curves.items():
            values[name] += expected_change(curve, age=year_age, growth=GrowthType.NORMAL, value=round(values[name]))
    return MappingProxyType({name: _clamp(values[name]) - getattr(current, name) for name in curves})


@cache
def growth_to_peak(age: int, position: Position) -> float:
    """`age` 歳の新人が `PEAK_AGE` 歳になるまでに見込める、候補を見比べる総合（`scouting_value`）の普通の伸び。

    見比べる項目（`judged_items`）の伸び（`item_growth_to_peak`）の平均。ドラフトの見込み（`draft_meeting.projected_growth`）が
    推定の伸びしろの倍率を掛けて使う。
    """
    growth = item_growth_to_peak(age, position)
    names = [name for name in growth if name != "stamina"]
    return sum(growth[name] for name in names) / len(names)


def projected_ratings(ratings: Ratings, *, age: int, position: Position, potential_estimate: Potential) -> Ratings:
    """見込みの能力（`PEAK_AGE` 歳ごろ）: 項目ごとに
    今の値 ＋ 普通の伸び（`item_growth_to_peak`）× 推定の伸びしろの倍率
    を丸めて 1〜100 に収めた値。`PEAK_AGE` 歳以上は今の能力のまま。

    今の能力は本当の値（今の能力は誰にでも見せている）、伸びしろは推定（`potential_estimate`）を使う。
    75以上の伸びの半減は見ない近似（ドラフトの見込みと同じ）。成長型と伸びしろ（隠し値）はそのまま写す。
    """
    if age >= PEAK_AGE:
        return ratings
    growth = item_growth_to_peak(age, position)
    multiplier = potential_estimate.multiplier

    def item(name: str) -> int:
        return _clamp(getattr(ratings, name) + growth[name] * multiplier)

    # 作り直さず `replace` で項目だけを差し替える（`aging.age_ratings` と同じ。足したフィールドが既定値に戻らない）
    if isinstance(ratings, PitcherRatings):
        return replace(
            ratings,
            stuff=item("stuff"),
            control=item("control"),
            home_run_avoidance=item("home_run_avoidance"),
            stamina=item("stamina"),
        )
    return replace(
        ratings,
        contact=item("contact"),
        power=item("power"),
        eye=item("eye"),
        speed=item("speed"),
        fielding=item("fielding"),
    )


def with_filled_potential(ratings: Ratings, *, player_id: int, world_seed: int) -> Ratings:
    """伸びしろの無い能力（#308 より前に作った世界の選手）に、事前の分布から伸びしろを引いて付ける。

    締める処理が、翌年の能力を作る前に通す（その年の成長から効き、翌年の行に写る。締める年の行は書き換えない。
    2026-10-25 ユーザー決定。#308 Q4）。乱数は `potential-<世界の選手 id>` で年に依らない。
    在籍の選手の見立て（`roster_potential_estimate`）も、伸びしろの無い行にはこれと同じ値を導いて使う（書き込みはしない）。
    伸びしろを持つ能力はそのまま。
    """
    if ratings.potential is not None:
        return ratings
    rng = make_random(game_seed(world_seed, POTENTIAL_FILL_SEED_YEAR, f"potential-{player_id}"))
    return with_potential(ratings, draw_potential(rng))


def roster_potential_estimate(
    ratings: Ratings,
    *,
    world_seed: int,
    team_id: int,
    player_id: int,
    seasons: int,
    accuracy: ScoutingAccuracy = ScoutingAccuracy(),  # noqa: B008  不変の値（frozen）なので既定値に置いてよい
) -> Potential:
    """球団 `team_id` のスカウトが見た、在籍の選手 `player_id` の伸びしろ（5刻み）。

    本当の値 ＋ `accuracy.potential_sd / √seasons` × z。z（標準正規）の種は「世界・見る球団・選手」で年に依らないので、
    年ごとにぶれず、在籍年数（`seasons`。`offseason_player.club_seasons`。1未満は1と見る）が長いほど本当の値に近づく
    （1年目 SD 10 → 4年目 5 → 9年目 約3.3）。伸びしろの無い行（#308 より前の世界で、まだ締めていない年）は、
    締める処理が翌年に書くのと同じ値（`with_filled_potential`）を本当の値として見る。
    """
    truth = with_filled_potential(ratings, player_id=player_id, world_seed=world_seed).potential
    assert truth is not None, "with_filled_potential は伸びしろを付ける"
    error = hash_normals(f"{world_seed}:roster-scout-{team_id}-{player_id}", 1)[0]
    return estimated_potential(truth.value + accuracy.potential_sd / sqrt(max(1, seasons)) * error)


def roster_projection(
    ratings: Ratings,
    *,
    age: int,
    position: Position,
    world_seed: int,
    team_id: int,
    player_id: int,
    seasons: int,
    accuracy: ScoutingAccuracy = ScoutingAccuracy(),  # noqa: B008  不変の値（frozen）なので既定値に置いてよい
) -> Ratings:
    """球団 `team_id` のスカウトの見立てで見た、在籍の選手の見込みの能力（`PEAK_AGE` 歳ごろ）。

    **在籍の選手の見込みの出典**で、画面の見込みの区分（`outlook_grade`）が使う（P3b で AI の戦力外・引退も使う）。
    年齢 `age` は締める年・能力の年の4月1日時点（`offseason_player.player_age`）。
    """
    if age >= PEAK_AGE:
        return ratings
    estimate = roster_potential_estimate(
        ratings, world_seed=world_seed, team_id=team_id, player_id=player_id, seasons=seasons, accuracy=accuracy
    )
    return projected_ratings(ratings, age=age, position=position, potential_estimate=estimate)


def outlook_grade(projected: Ratings) -> RatingGrade:
    """見込みの区分（S〜G）。見込みの能力の総合（画面の「総合」と同じ `overall_value`）を丸めて能力の区分で引く。

    画面に出すのはこの区分だけで、伸びしろの値も倍率も見込みの数値も見せない。
    """
    return RatingGrade.from_value(_clamp(overall_value(projected)))


def roster_outlook_grade(
    ratings: Ratings,
    *,
    age: int,
    position: Position,
    world_seed: int,
    team_id: int,
    player_id: int,
    seasons: int,
    accuracy: ScoutingAccuracy = ScoutingAccuracy(),  # noqa: B008  不変の値（frozen）なので既定値に置いてよい
) -> RatingGrade | None:
    """在籍の選手の見込みの区分（画面に出す値）。`PEAK_AGE` 歳以上は None（見込み＝今の能力。画面は「—」）。"""
    if age >= PEAK_AGE:
        return None
    return outlook_grade(
        roster_projection(
            ratings,
            age=age,
            position=position,
            world_seed=world_seed,
            team_id=team_id,
            player_id=player_id,
            seasons=seasons,
            accuracy=accuracy,
        )
    )


def _clamp(value: float) -> int:
    return min(RATING_MAX, max(RATING_MIN, round(value)))
