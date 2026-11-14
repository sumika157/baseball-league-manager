"""ドラフト会議（本指名と育成の巡）。**純粋な関数と値**で、DB にも Django にも触れない。

会議は世界で1回、全リーグの球団が同じ会議で、その年の候補（`ProspectPool`）を取り合う。指名の順番は世界の規則の
戦略（`draft_order.py`）が決め、ここは規則を知らない。

- **候補**は会議の年ごとに、世界のシード・年・世界の全選手の名前から決定的に作る（`draw_pool`。保存しない）
- 候補の本当の能力は見せない。各球団は**スカウトの推定**（`scouting_report` の `ScoutingReport`。項目ごとと
  伸びしろに誤差を足して5刻み・成長型なし）で見比べる。誤差は「世界・年・候補・見る球団」で決まるので、球団ごとに見立てが違う
- **見込み**（#308 P2）: `PEAK_AGE` 歳までの伸びの見込みは `projected_growth`（普通型の伸び × 推定の伸びしろの倍率）が
  唯一の出典で、将来値（`future_value`）・1位の好み（`DraftTaste.evaluate`）・見込みの区分（`projected_grade`）が使う
- 会議の状態（`DraftState`）は、行動（入札・指名・選択終了。`DraftAction`）の並びと設定（規則・人数の幅・候補）から
  **導く**。「いまの巡」「次の番」は持たない（同じ行動の並びなら、何度作り直しても同じ状態になる）
- 入札の巡は、入札がそろったところで抽選まで解く（重複は1球団だけ当たり、外れた球団は同じ巡で再入札）。
  抽選の乱数は `game_seed(世界のシード, 年, "draft-lottery-main-<巡>-<回>-<候補>")`
- AI の指名（`ai_action`）は、自分のスカウトの推定の**将来値**（`future_value`）に、位置の残りの重みを足した評価が
  最大の候補。位置は、その球団の新人の位置の計画（`draft.plan_team_draft`。今までの自動ドラフトと同じ）の中から選ぶ
- **1位の好み**（#302）: 本指名の1巡目の入札（1位）だけ、将来値の代わりに球団の好み（`DraftTaste`。経路・
  即戦力か将来性か・投手か野手か。世界のシードと球団から導く）と候補ひとりずつへの惚れ込み（`draft_liking`）を
  入れた評価で選ぶ。際立った候補に全球団の入札が集まらないようにするため。2巡目以降は将来値のままなので、
  新人の分布は変わらない
- 位置の候補が尽きたら、その場で1人足す（`ProspectPool.with_extra`。締める処理を失敗させない）
- **育成の巡**（#198 D2）: 本指名が全球団で終わった時点で、本指名の合計が「球団数 × `MAX_DRAFTEES`」（NPB の120名の
  一般化）に達しておらず、育成の枠（`DraftSlot.development_max`）のある球団がいれば続けて開く。巡は1から数え、
  順番は**本指名の最後の巡の続き**（戦略に `本指名の最後の巡 + 育成の巡` を渡す。全巡入札なら育成の巡も入札と抽選で、
  乱数の用途は `draft-lottery-development-<巡>-<回>-<候補>`）。候補は本指名の残り（同じ `ProspectPool`）と、
  本指名の候補の後ろに足した**育成の層の候補**（#362。今の能力が低めの高校生中心。`DEVELOPMENT_POOL_*`）。本指名の候補の
  人数と合わせ込みは本指名の計画だけから決まり、AI は本指名で育成の層を指名しない（`Prospect.development_tier`）ので、
  育成の巡を足しても AI の本指名の分布は変わらない（本指名の途中で候補を足すと番号 `len(pool)` に育成の層の人数が乗り、
  足す人は変わる。GM が本指名で育成の層を取れば AI の結果も変わる）。選択終了はいつでもでき、
  位置の最低人数（支配下の話）は見ない。区分（本指名 / 育成）も行動の並びから導く（`DraftAction` は区分を持たない）。
  **社会人の候補は育成で指名できない**（#307。規則は `DraftRoute.allows_development_pick` の1か所で、AI の指名・
  `DraftState.apply` の検査・育成の層と育成の巡で足す候補の経路が同じ判定を使う）。本指名では今までどおり指名できる

GM の指名（D3。#297）は同じ `DraftState.apply` を通り、行動の並びは会議の記録（`draft_record.DraftRecord`）に保存する。
人数の幅は `meeting_slots`、候補は `meeting_pool` が唯一の出典で、どちらも `open_meeting` が通す（会議を始めるときに
幅を記録に写し、締めるときに比べる。1手ごとに写した幅から候補を作り直す）。会議を始めなかった年は、
締める処理が `auto_complete` で全球団を AI として最後まで進める。
外国人は会議の前に獲得する（`foreign_market.py`。#169）。
"""

from __future__ import annotations

import bisect
import hashlib
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from enum import Enum

from ..exceptions import InvalidDraftAction
from ..simulation.randomness import GameRandom, game_seed, hash_normals, hash_words, make_random, weighted_index
from ..simulation.ratings import (
    POTENTIAL_AVERAGE,
    RATING_MAX,
    RATING_MIN,
    BatterRatings,
    PitcherRatings,
    Potential,
    RatingGrade,
)
from ..value_objects import DraftPick, Position
from ..virtual_players import generator
from ..virtual_players.generator import AmateurPath
from .draft import (
    MAX_DEVELOPMENT_PICKS,
    MAX_DRAFTEES,
    MIN_BY_POSITION,
    Draftee,
    DraftRoute,
    DraftTeam,
    Rookie,
    TeamDraftPlan,
    club_draftees,
    draw_rookie,
    keeps_position_minimum,
    plan_team_draft,
    position_shortfall,
    with_drawn_potential,
)
from .draft_order import DraftOrderRule, DraftOrderStrategy, DraftStandings, strategy_for
from .initial_ratings import SEASON_START_MONTH
from .offseason_player import Ratings
from .outlook import ScoutingAccuracy, estimated_potential, growth_to_peak, judged_items, rounded_estimate

# 候補の人数（指名の枠1つあたり）。位置ごとに、全球団がその位置に計画した指名の数 × この数の候補を作る。
# 会議が候補の上から取る割合（約4人に1人）を、指名の人数と位置の偏りによらず一定にする（帯の下の球団が
# 投手を10人ずつ取る年も、新人の分布が変わらない）
POOL_PER_PICK = 4
# 位置ごとの候補の最少（どの球団も計画していない位置にも、GM が選べる候補を置く。D3）
POOL_MIN_PER_POSITION = 4
# 候補の経路の割合と、能力の平均のずらし（経路の平均 `DraftRoute.rating_mean` からの点）。
# **指名された日本人の新人**の錨の前の平均が経路ごとに今までの値（35/40/43）、経路の割合が今までの割合（30/50/20）、
# 標準偏差が今までの値（6）に近くなるよう、定常状態の実測で決めた値（`test_offseason_population.py` が見張る。
# 実測は Wiki「ペナントモードの仕組み」の「実測値」）。会議は候補の上の方から取るので、候補の平均は新人より低い
# #308 P2 で AI が伸びしろの推定を見るようになり、社会人の能力を年齢で傾けたので合わせ直した
POOL_ROUTE_WEIGHTS = {AmateurPath.HIGH_SCHOOL: 14.0, AmateurPath.UNIVERSITY: 65.0, AmateurPath.CORPORATE: 72.0}
# 候補の能力の標準偏差（新人の `prospects.PROSPECT_RATING_SD` 6 より広い。会議が上から取ると散らばりが狭くなるため）
POOL_RATING_SD = 6.75
# 投手の候補だけに足すずらし（投手は見比べる項目が3つで野手の5つより総合が散らばり、上から取ると野手より高く出るため）
POOL_PITCHER_SHIFT = -1.4
POOL_RATING_SHIFT = {AmateurPath.HIGH_SCHOOL: -0.1, AmateurPath.UNIVERSITY: -3.5, AmateurPath.CORPORATE: -5.6}
# 候補の伸びしろの平均（経路ごと。#308 P2・P3）。AI は推定の伸びしろの大きい候補を上に見るので、指名された新人の
# 伸びしろは候補より高く出る。上がり方は経路で違う（候補の多い大学は上から取る選び方が効き、ほとんどが指名される
# 高校は効かない）ので、**指名された日本人の新人**の倍率の平均が**どの経路も**約1.0（年齢曲線を引き直さずに平衡を
# 保つ条件。「伸びしろは経路で変えない」が指名された新人について成り立つ）になるよう、上の合わせ込みと同じ定常状態の
# 実測で決めた値（2026-10-25 ユーザー決定 B）。標準偏差と範囲は事前の分布（`estimate.POTENTIAL_SD`・`POTENTIAL_RANGE`）
POOL_POTENTIAL_MEAN = {AmateurPath.HIGH_SCHOOL: 48.0, AmateurPath.UNIVERSITY: 42.7, AmateurPath.CORPORATE: 46.3}
# **育成の層の候補**（#362。#170 Q3 B「育成の新人は高校生を増やす」。2026-11-12 ユーザー決定）。本指名の候補の後ろに、
# 全球団の育成の位置の計画（`DraftSlot.development_planned`）× `DEVELOPMENT_POOL_PER_PICK` 人を足す。
# 本指名の候補だけだと、将来値の高い高校生は本指名で先に取られ、育成の巡には大学生しか残らなかった（育成の新人の
# 高校 約3〜7%・伸びしろの倍率 0.82〜0.86）。経路は育成で指名できる経路だけ（`DraftRoute.allows_development_pick`）で、
# 目安は NPB の育成ドラフト（2021〜2024年度。高校 54%・大学 24%・独立リーグ 22%。独立リーグはこのアプリに無く、
# 年頃の近い大学に寄せると 高校 約半分）。
# 今の能力は本指名の候補（`POOL_RATING_SHIFT`）より低くずらす。AI は本指名で育成の層を指名しない
# （`Prospect.development_tier`。見ると本指名が将来値の高い高校生に偏る。合成の母集団で本指名の高校が 31% → 85%）ので、
# AI の本指名の新人の分布は変わらない。伸びしろの平均は本指名の候補（`POOL_POTENTIAL_MEAN`）より低い
# （AI が推定の伸びしろで上から取るので、同じ平均では育成の新人の倍率が 1.08〜1.14 になった）。
# 値は「育成の新人の高校が約半分・倍率が 1.0 の少し下・母集団テストの帯（2年目の錨の補正 1.5 点以下など）の中」に
# なるよう、定常状態（48球団 × 64人）で実測して決めた（Wiki「実測値」）
DEVELOPMENT_POOL_PER_PICK = 2
DEVELOPMENT_POOL_ROUTE_WEIGHTS = {AmateurPath.HIGH_SCHOOL: 60.0, AmateurPath.UNIVERSITY: 40.0}
DEVELOPMENT_POOL_RATING_SHIFT = {AmateurPath.HIGH_SCHOOL: -9.0, AmateurPath.UNIVERSITY: -4.0}
DEVELOPMENT_POOL_POTENTIAL_MEAN = {AmateurPath.HIGH_SCHOOL: 40.0, AmateurPath.UNIVERSITY: 37.0}
# AI の評価: 将来値 + NEED_WEIGHT × その位置の残りの人数（計画した位置のうち、まだ指名していない数）
NEED_WEIGHT = 2.0
# 1位の好み（`DraftTaste`。#302）の幅。球団の色は、経路の加点（球団の中で平均0にそろえる前に ±TASTE_ROUTE 点）・
# 伸びの重み（1 ± TASTE_GROWTH）・投手への加点（±TASTE_PITCHER 点）で、どれも一様。候補ひとりずつへの惚れ込みは
# 標準偏差 TASTE_LIKING_SD 点の正規。球団の色だけでは際立った候補への集中がほとんど減らなかった（48球団の合成の会議で
# 最多 40 → 38球団）ので惚れ込みを足し、最多が数球団〜10球団程度になる大きさを実測で決めた（Wiki「実測値」）
TASTE_ROUTE = 3.0
TASTE_GROWTH = 0.4
TASTE_PITCHER = 2.0
TASTE_LIKING_SD = 6.0


class DraftActionKind(Enum):
    """会議の行動の種類。値は画面に出す名前。"""

    BID = "入札"
    PICK = "指名"
    PASS = "選択終了"


@dataclass(frozen=True)
class DraftAction:
    """会議の行動ひとつ。巡・入札の回は行動の並びから導く（持たない）。

    `extra_position` は、位置の候補が尽きたときにその場で足した候補（`prospect_key` は足す前の候補の数）の位置。
    """

    kind: DraftActionKind
    team_id: int
    prospect_key: int | None = None
    extra_position: Position | None = None


@dataclass(frozen=True)
class Prospect:
    """候補ひとり。`key` は候補の番号（会議の中で一意）、`age` は入団の年の4月1日時点の満年齢。

    能力は錨の前で、本当の値（球団にはスカウトの推定 `scouting_report` しか見せない）。

    `development_tier` は育成の層の候補か（#362。`draw_pool` の育成の層と、育成の巡で足した候補）。AI は本指名で
    育成の層の候補を指名しない（スカウトが育成で指名する候補として見ている。`DraftState.ai_action`）。GM は本指名でも
    指名できる（NPB に「育成でしか指名できない選手」の規則は無い）。
    """

    key: int
    rookie: Rookie
    age: int
    development_tier: bool = False

    @property
    def position(self) -> Position:
        return self.rookie.position

    @property
    def route(self) -> DraftRoute:
        return self.rookie.route

    @property
    def allows_development_pick(self) -> bool:
        """育成の巡で指名できる候補か（規則は `DraftRoute.allows_development_pick`）。

        AI の指名と `DraftState.apply` の検査が使う。"""
        return self.route.allows_development_pick


@dataclass(frozen=True)
class ProspectPool:
    """その年の候補。`taken_names` は世界の選手と候補の名前（足す候補の名前が重ならないように）。"""

    prospects: tuple[Prospect, ...]
    taken_names: frozenset[str]
    world_seed: int
    year: int

    def __len__(self) -> int:
        return len(self.prospects)

    def get(self, key: int) -> Prospect:
        if not 0 <= key < len(self.prospects):
            raise InvalidDraftAction("その選手は候補にいません。")
        return self.prospects[key]

    @property
    def signature(self) -> str:
        """候補の指紋。会議を始めたときに会議の記録（`draft_record.DraftRecord`）に写し、再生するときに作り直した
        候補と比べる（コードが変わって候補が入れ替わったことを検出する）。

        **候補の身元だけ**（番号・名前・位置・経歴・生年月日）から作る（#297 Q2。2026-10-25 ユーザー決定）。
        能力・伸びしろ（隠し値）・成長型は入れないので、能力の式や隠し値を後から変えても途中の会議は壊れない。
        """
        digest = hashlib.blake2b(digest_size=16)
        for prospect in self.prospects:
            profile = prospect.rookie.profile
            identity = (
                str(prospect.key),
                prospect.rookie.name,
                prospect.position.value,
                prospect.route.value,
                profile.high_school,
                profile.university,
                profile.corporate_team,
                profile.birth_date.isoformat() if profile.birth_date else "",
            )
            digest.update(("|".join(identity) + ";").encode())
        return digest.hexdigest()

    def with_extra(self, position: Position, *, development: bool = False) -> ProspectPool:
        """位置 `position` の候補を1人足した候補（乱数は `draft-extra-<番号>`。同じ並びなら同じ人）。

        育成の巡（`development`）で足す候補は、育成の層の候補（`draw_pool` の育成の層と同じ分布。育成で指名できる
        経路 `DraftRoute.allows_development_pick` だけ）から引く。
        """
        key = len(self.prospects)
        rng = make_random(game_seed(self.world_seed, self.year, f"draft-extra-{key}"))
        names = set(self.taken_names)
        prospect = _draw_prospect(
            rng,
            key,
            position,
            path=None,
            year=self.year,
            world_seed=self.world_seed,
            used_names=names,
            development=development,
        )
        return replace(self, prospects=(*self.prospects, prospect), taken_names=frozenset(names))


def _draw_prospect(
    rng: GameRandom,
    key: int,
    position: Position,
    *,
    path: AmateurPath | None,
    year: int,
    world_seed: int,
    used_names: set[str],
    development: bool = False,
) -> Prospect:
    """候補ひとり。伸びしろは候補の番号ごとの乱数（`draft-potential-<番号>`）から、
    経路の平均 `POOL_POTENTIAL_MEAN` で引く
    （`rng` の流れに足さないので、名前・経歴・今の能力・成長型は伸びしろを足す前と同じ）。

    `development` なら育成の層の候補（経路の重み `DEVELOPMENT_POOL_ROUTE_WEIGHTS`・能力のずらし
    `DEVELOPMENT_POOL_RATING_SHIFT`・伸びしろの平均 `DEVELOPMENT_POOL_POTENTIAL_MEAN`。
    経路は育成で指名できる経路（`DraftRoute.development_amateur_paths`。規則の出典）に絞ってから引く）。
    """
    weights = DEVELOPMENT_POOL_ROUTE_WEIGHTS if development else POOL_ROUTE_WEIGHTS
    shifts = DEVELOPMENT_POOL_RATING_SHIFT if development else POOL_RATING_SHIFT
    potentials = DEVELOPMENT_POOL_POTENTIAL_MEAN if development else POOL_POTENTIAL_MEAN
    if path is None:
        allowed = DraftRoute.development_amateur_paths() if development else tuple(AmateurPath)
        paths = [item for item in weights if item in allowed]
        path = paths[weighted_index(rng, [weights[item] for item in paths])]
    rookie = draw_rookie(
        rng,
        position,
        year=year,
        used_names=used_names,
        path=path,
        rating_shift=shifts[path] + (POOL_PITCHER_SHIFT if position.is_pitcher else 0.0),
        rating_sd=POOL_RATING_SD,
    )
    rookie = with_drawn_potential(
        rookie, world_seed=world_seed, year=year, key=f"draft-potential-{key}", mean=potentials[path]
    )
    age = rookie.profile.age(date(year + 1, SEASON_START_MONTH, 1))
    assert age is not None, "候補は生年月日を持つ"
    return Prospect(key=key, rookie=rookie, age=age, development_tier=development)


def draw_pool(
    planned: Mapping[Position, int],
    *,
    world_seed: int,
    year: int,
    used_names: Collection[str],
    development_planned: Mapping[Position, int] | None = None,
) -> ProspectPool:
    """締める年 `year` の会議の候補。乱数は本指名の候補が `draft-pool`、育成の層の候補が `draft-pool-development`。

    本指名の候補の位置ごとの人数は、全球団がその位置に計画した指名の数（`planned`）× `POOL_PER_PICK`
    （最少 `POOL_MIN_PER_POSITION`。指名が1つも無い会議は0人）。経路は `POOL_ROUTE_WEIGHTS` で1人ずつ引く。
    その後ろに、育成の層の候補を全球団の育成の位置の計画（`development_planned`）× `DEVELOPMENT_POOL_PER_PICK` 人
    足す（#362。経路は `DEVELOPMENT_POOL_ROUTE_WEIGHTS`）。本指名の候補は育成の層を足す前と同じ（番号・身元・能力）。
    名前は `used_names`（世界の全選手）と重ならない。
    """
    rng = make_random(game_seed(world_seed, year, "draft-pool"))
    names = set(used_names)
    total = sum(planned.values())
    positions = [
        position
        for position in generator.POSITION_RATIOS
        for _ in range(max(POOL_PER_PICK * planned.get(position, 0), POOL_MIN_PER_POSITION) if total else 0)
    ]
    prospects = [
        _draw_prospect(rng, key, position, path=None, year=year, world_seed=world_seed, used_names=names)
        for key, position in enumerate(positions)
    ]
    development_rng = make_random(game_seed(world_seed, year, "draft-pool-development"))
    development_positions = [
        position
        for position in generator.POSITION_RATIOS
        for _ in range(DEVELOPMENT_POOL_PER_PICK * (development_planned or {}).get(position, 0))
    ]
    prospects.extend(
        _draw_prospect(
            development_rng,
            key,
            position,
            path=None,
            year=year,
            world_seed=world_seed,
            used_names=names,
            development=True,
        )
        for key, position in enumerate(development_positions, start=len(prospects))
    )
    return ProspectPool(prospects=tuple(prospects), taken_names=frozenset(names), world_seed=world_seed, year=year)


@dataclass(frozen=True)
class ScoutingReport:
    """球団のスカウトが見た候補（`scouting_report`）。

    - `ratings`: 今の能力の推定（5刻み。成長型は普通、伸びしろは持たない＝隠し値は見せない）
    - `potential`: 伸びしろの推定（5刻み）。見込み（`projected_growth`）に使う
    """

    ratings: Ratings
    potential: Potential


def _scout_noise(world_seed: int, year: int, team_id: int, key: int, count: int) -> list[float]:
    """推定の誤差（標準正規）を `count` 個（8個まで）。種は「世界・年・候補・見る球団」。

    添字ごとに決まる（`count` を増やしても前の添字の値は変わらない）。"""
    return hash_normals(f"{world_seed}:{year}:draft-scout-{team_id}-{key}", count)


def scouting_report(
    prospect: Prospect,
    team_id: int,
    *,
    world_seed: int,
    year: int,
    accuracy: ScoutingAccuracy = ScoutingAccuracy(),  # noqa: B008  不変の値（frozen）なので既定値に置いてよい
) -> ScoutingReport:
    """球団 `team_id` のスカウトが見た候補。項目ごとに真の値 + N(0, `accuracy.sd`)、伸びしろは
    真の値 + N(0, `accuracy.potential_sd`) を、どれも5刻みに丸める。

    誤差は項目の宣言の順に引き、伸びしろはその次の添字（伸びしろを足す前と、今の項目の推定は変わらない）。
    推定の能力は成長型が普通で伸びしろを持たない（隠し値を見せない）。伸びしろの無い候補（ふつうは無い）は普通として見る。
    """
    ratings = prospect.rookie.ratings
    noise = _scout_noise(world_seed, year, team_id, prospect.key, len(ratings.LABELS) + 1)
    return scout_with_noise(ratings, noise, accuracy=accuracy)


def scout_noise(text: str, ratings: Ratings) -> list[float]:
    """`ratings` を見る推定の誤差（標準正規。項目の数 + 伸びしろの1つ）。種は用途を表す文字列 `text`。

    会議の外の候補（外国人の獲得の候補。`foreign_market.py`）が、会議の候補と種の重ならない用途で見るのに使う。
    """
    return hash_normals(text, len(ratings.LABELS) + 1)


def scout_with_noise(
    ratings: Ratings,
    noise: Sequence[float],
    *,
    accuracy: ScoutingAccuracy = ScoutingAccuracy(),  # noqa: B008  不変の値（frozen）なので既定値に置いてよい
) -> ScoutingReport:
    """真の能力 `ratings` に誤差 `noise`（項目の宣言の順、続けて伸びしろ）を足した推定（`scouting_report` の本体）。"""
    labels = ratings.LABELS
    values = {
        name: rounded_estimate(getattr(ratings, name) + accuracy.sd * error)
        for name, error in zip(labels, noise[: len(labels)], strict=True)
    }
    truth = POTENTIAL_AVERAGE if ratings.potential is None else ratings.potential.value
    potential = estimated_potential(truth + accuracy.potential_sd * noise[len(labels)])
    # 成長型は既定の普通、伸びしろは既定の None（見せない）
    estimate: Ratings
    if isinstance(ratings, PitcherRatings):
        estimate = PitcherRatings(
            stuff=values["stuff"],
            control=values["control"],
            home_run_avoidance=values["home_run_avoidance"],
            stamina=values["stamina"],
        )
    else:
        estimate = BatterRatings(
            contact=values["contact"],
            power=values["power"],
            eye=values["eye"],
            speed=values["speed"],
            fielding=values["fielding"],
        )
    return ScoutingReport(ratings=estimate, potential=potential)


def scouting_value(ratings: Ratings) -> float:
    """AI が候補を見比べる総合: スタミナを除く項目の平均（野手は守備・走力も、投手は球威・制球・一発回避）。

    打撃だけ（`overall_value`）で選ぶと、指名された新人の守備・走力だけが候補の平均のまま残り、項目の間の
    つり合いが崩れる（錨の前の分布がずれる）。項目をそろえて見るので、合わせ込み（`POOL_RATING_SHIFT`）は全項目に同じだけ効く。
    """
    names = judged_items(ratings)
    return sum(getattr(ratings, name) for name in names) / len(names)


def _clamp(value: float) -> int:
    return min(RATING_MAX, max(RATING_MIN, round(value)))


def projected_growth(prospect: Prospect, report: ScoutingReport) -> float:
    """候補の見込みの伸び: `PEAK_AGE` 歳までの普通の伸び（`outlook.growth_to_peak`）× 推定の伸びしろの倍率。

    **候補の見込みの伸びの唯一の出典**（将来値・1位の好み・見込みの区分が使う）。伸びの出典は在籍の選手の見込みと同じ
    `outlook.item_growth_to_peak` で、物差しは候補を見比べる総合（`scouting_value`）。
    成長の式（`aging.expected_change`）と同じく、伸びしろは伸びの大きさだけに効く
    （75以上の半減は見ない近似。新人は平均より低く入るので届かない）。
    """
    return growth_to_peak(prospect.age, prospect.position) * report.potential.multiplier


def future_value(prospect: Prospect, report: ScoutingReport) -> float:
    """将来値（見込み）: 推定の総合（`scouting_value`）に、見込みの伸び（`projected_growth`）を足したもの。"""
    return scouting_value(report.ratings) + projected_growth(prospect, report)


def projected_grade(prospect: Prospect, report: ScoutingReport) -> RatingGrade:
    """見込みの区分（S〜G）。将来値（`future_value`）を丸めて能力の区分（`RatingGrade`）で引く。

    画面（D3 の会議）に出すのはこの区分で、伸びしろの値も倍率も見せない（在籍の選手の区分は `outlook.outlook_grade`）。
    """
    return RatingGrade.from_value(_clamp(future_value(prospect, report)))


@dataclass(frozen=True)
class DraftTaste:
    """球団の1位の好み（#302。球団の色）。本指名の1巡目の入札だけ AI の評価に足し、1位の入札が際立った1人に
    集まらないよう散らす。

    保存しない（`draft_taste` が世界のシードと球団から導く。年では変わらない）。

    - `route_bonus`: 経路（高校・大学・社会人）ごとの加点。球団の中で平均0
    - `growth_weight`: `PEAK_AGE` 歳までの見込みの伸び（`projected_growth`）に掛ける重み。1より大きいと将来性を、
      小さいと即戦力を重く見る
    - `pitcher_bonus`: 投手の候補への加点（負なら野手を好む。野手の候補は0）
    """

    route_bonus: Mapping[DraftRoute, float] = field(default_factory=dict)
    growth_weight: float = 1.0
    pitcher_bonus: float = 0.0

    def evaluate(self, prospect: Prospect, report: ScoutingReport, *, liking: float = 0.0) -> float:
        """スカウトの見立て `report` に好みを入れた評価:
        将来値（`future_value`）+ (重み − 1) × 見込みの伸び（`projected_growth`）+ 経路の加点 + 投手の加点
        + 惚れ込み（`draft_liking`）。好みが既定（`DraftTaste()`）なら将来値 + 惚れ込み。"""
        return (
            future_value(prospect, report)
            + (self.growth_weight - 1.0) * projected_growth(prospect, report)
            + self.route_bonus.get(prospect.route, 0.0)
            + (self.pitcher_bonus if prospect.position.is_pitcher else 0.0)
            + liking
        )


# 日本人の経路（`DraftRoute` の並びのまま。並びは hash から引く乱数の位置に対応するので、変えると全球団の好みが変わる）
_TASTE_ROUTES = tuple(DraftRoute)


def draft_taste(world_seed: int, team_id: int) -> DraftTaste:
    """球団 `team_id` の好み。種は「世界・球団」（年に依らない）。乱数源を作らずに hash から一様に引く。"""
    words = hash_words(f"{world_seed}:draft-taste-{team_id}")
    uniform = [2.0 * word * 2.0**-32 - 1.0 for word in words]  # [-1, 1)
    count = len(_TASTE_ROUTES)
    raw = [TASTE_ROUTE * value for value in uniform[:count]]
    mean = sum(raw) / count
    return DraftTaste(
        route_bonus={route: value - mean for route, value in zip(_TASTE_ROUTES, raw, strict=True)},
        growth_weight=1.0 + TASTE_GROWTH * uniform[count],
        pitcher_bonus=TASTE_PITCHER * uniform[count + 1],
    )


def draft_liking(world_seed: int, year: int, team_id: int, prospect_key: int) -> float:
    """球団 `team_id` の候補 `prospect_key` への惚れ込み（点。N(0, `TASTE_LIKING_SD`)）。種は「世界・年・候補・球団」。

    スカウトの推定（`scouting_report`）とは別の値で、見立ての誤差ではなく球団の好み（1巡目の評価にだけ足す）。
    """
    return TASTE_LIKING_SD * hash_normals(f"{world_seed}:{year}:draft-liking-{team_id}-{prospect_key}", 1)[0]


@dataclass(frozen=True)
class DraftSlot:
    """球団の本指名の人数の幅と位置の計画（会議の外で決まる。会議を始めたときに写す＝D3）。

    - `minimum`・`maximum`: 指名する日本人の人数の幅。AI は最多まで指名する。最少に届けば自分の番で選択終了できる
    - `planned`: AI が埋める位置（`draft.plan_team_draft` の日本人の位置）
    - `base_counts`: 会議の前の支配下の位置ごとの人数に、獲得した外国人の位置を足したもの（位置の最低人数の検査に使う）
    - `development_planned`: 育成の巡で AI が埋める位置（`draft.plan_team_draft` の育成の位置）
    - `development_max`: 育成の巡で指名できる最多（AI は計画の人数。D3 で GM の球団は
      `MAX_DEVELOPMENT_PICKS` を写す）。
      最少は無い（育成の巡はいつでも選択終了できる）
    """

    team_id: int
    minimum: int
    maximum: int
    planned: tuple[Position, ...]
    base_counts: Mapping[Position, int]
    development_planned: tuple[Position, ...] = ()
    development_max: int = 0

    def __post_init__(self) -> None:
        if not 0 <= self.minimum <= self.maximum <= MAX_DRAFTEES:
            raise ValueError("指名の人数の幅が正しくありません。")
        if not 0 <= self.development_max <= MAX_DEVELOPMENT_PICKS:
            raise ValueError("育成の指名の人数が正しくありません。")

    @classmethod
    def of(cls, team: DraftTeam, plan: TeamDraftPlan) -> DraftSlot:
        """球団の計画から。位置の最低人数は、会議の前の支配下に獲得した外国人を足して数える。"""
        base = Counter({position: team.position_counts.get(position, 0) for position in MIN_BY_POSITION})
        base.update(plan.foreign_positions)
        return cls(
            team_id=team.team_id,
            minimum=min(plan.japanese_minimum, plan.japanese_count),
            maximum=plan.japanese_count,
            planned=plan.japanese_positions,
            base_counts=dict(base),
            development_planned=plan.development_positions,
            development_max=len(plan.development_positions),
        )


def meeting_slots(
    teams: Sequence[DraftTeam], plans: Mapping[int, TeamDraftPlan], *, managed_team_id: int | None
) -> tuple[DraftSlot, ...]:
    """会議の人数の幅（球団 id の昇順）。**会議を始めるときと締めるときの唯一の関数**（#297）。

    AI の球団は計画どおり（`DraftSlot.of`）。受け持つ球団（`managed_team_id`。GM）だけ、育成の最多を
    `MAX_DEVELOPMENT_PICKS` にする（GM は育成の巡で 0〜最多を自由に選ぶ）。GM の球団の番を AI が進めたとき
    （会議を始めなかった年・お任せ）は、育成の計画の人数で選択を終える（`DraftState.ai_action`）ので、
    新人は受け持つ球団の無い会議と同じになる。
    """
    slots = []
    for team in sorted(teams, key=lambda item: item.team_id):
        slot = DraftSlot.of(team, plans[team.team_id])
        if team.team_id == managed_team_id:
            slot = replace(slot, development_max=MAX_DEVELOPMENT_PICKS)
        slots.append(slot)
    return tuple(slots)


@dataclass(frozen=True)
class DraftContext:
    """締める処理に渡す会議の設定（世界の規則と、その年の公式戦の最終順位、受け持つ球団）。

    `managed_team_id` は人数の幅（`meeting_slots`）だけに効く。会議を始めなかった年の新人は、
    受け持つ球団の有無で変わらない。
    """

    rule: DraftOrderRule
    standings: DraftStandings
    managed_team_id: int | None = None


@dataclass(frozen=True)
class DraftTurn:
    """次に動く球団。`is_bid` は入札の巡か、`attempt` は入札の回（1から。指名の巡は1）。
    `round_number` は区分の中の巡（本指名・育成それぞれ1から）、`is_development` は育成の巡か。"""

    team_id: int
    round_number: int
    is_bid: bool
    attempt: int
    is_development: bool = False


@dataclass(frozen=True)
class Lottery:
    """入札の重複と抽選の結果。`bidders` は入札した球団（id の順）。`round_number` は区分の中の巡。"""

    round_number: int
    attempt: int
    prospect_key: int
    bidders: tuple[int, ...]
    winner: int
    is_development: bool = False


@dataclass(frozen=True)
class DraftSelection:
    """指名の結果ひとつ（巡は区分の中の巡。本指名・育成それぞれ1から）。"""

    team_id: int
    prospect_key: int
    round_number: int
    is_development: bool = False


@dataclass(frozen=True)
class DraftMove:
    """会議で確定した動きひとつ（会議の画面の「直近の動き」。`DraftState.resolved_moves`）。

    - `seq`: 確定させた行動の番号（1から。行動の並びの位置 + 1。保存した行の `seq` と同じ数え方）
    - `kind`: `BID` は入札で確定（重複して抽選になったら `lottery`）、`PICK` は指名、
      `PASS` は選択終了（`prospect_key` は None）
    - `round_number`・`is_development`: 区分の中の巡と、育成の巡か
    """

    seq: int
    team_id: int
    kind: DraftActionKind
    round_number: int
    is_development: bool = False
    prospect_key: int | None = None
    lottery: Lottery | None = None


@dataclass
class DraftState:
    """会議の状態。行動の並び（`actions`）から導く。`apply` で1手ずつ進める（不変条件は `InvalidDraftAction`）。

    中で覚えるのは導いた値だけ（作り直せば同じ）。保存するのは行動の並びだけ（D3）。
    """

    strategy: DraftOrderStrategy
    slots: Mapping[int, DraftSlot]
    pool: ProspectPool
    accuracy: ScoutingAccuracy = field(default_factory=ScoutingAccuracy)
    actions: list[DraftAction] = field(default_factory=list)
    selections: list[DraftSelection] = field(default_factory=list)
    lotteries: list[Lottery] = field(default_factory=list)
    # 確定した動き（指名・入札の確定・選択終了）の順。保留中の入札は入らない
    moves: list[DraftMove] = field(default_factory=list)
    # 区分の中の巡（本指名・育成それぞれ1から）と、育成の巡に入ったか（どちらも行動の並びから導いた値）
    round_number: int = 1
    attempt: int = 1
    is_development: bool = False
    _bids: dict[int, int] = field(default_factory=dict)
    _settled: set[int] = field(default_factory=set)
    _taken: set[int] = field(default_factory=set)
    # 本指名の選択終了・指名の数・指名した位置
    _passed: set[int] = field(default_factory=set)
    _picked: Counter[int] = field(default_factory=Counter)
    _positions: dict[int, Counter[Position]] = field(default_factory=dict)
    # 育成の巡の選択終了・指名の数・指名した位置
    _development_passed: set[int] = field(default_factory=set)
    _development_picked: Counter[int] = field(default_factory=Counter)
    _development_positions: dict[int, Counter[Position]] = field(default_factory=dict)
    # 本指名で行動のあった最後の巡（育成の巡の順番の起点）
    _main_last_round: int = 0
    # 球団 × 位置ごとの、候補の評価（`evaluation`）の高い順（AI が使う。候補を足したら差し込む）
    _rankings: dict[tuple[int, Position, bool], list[tuple[float, int]]] = field(default_factory=dict)
    # 球団ごとの好み（世界のシードと球団から導いた値。初めて要るときに導いて覚える）
    _tastes: dict[int, DraftTaste] = field(default_factory=dict)
    # 球団 × 候補のスカウトの見立て（1位の順位と2巡目以降の順位の両方が使うので、推定を1回だけ作る）
    _reports: dict[tuple[int, int], ScoutingReport] = field(default_factory=dict)
    # 球団 × 候補の将来値（2巡目以降の順位を作り直すたびに使う。見立てから導いた値を覚える）
    _futures: dict[tuple[int, int], float] = field(default_factory=dict)

    @classmethod
    def open(
        cls,
        strategy: DraftOrderStrategy,
        slots: Sequence[DraftSlot],
        pool: ProspectPool,
        actions: Sequence[DraftAction] = (),
        *,
        accuracy: ScoutingAccuracy | None = None,
    ) -> DraftState:
        """設定と行動の並びから状態を作る（行動は先頭から当て直す。当てられない行動があれば `InvalidDraftAction`）。"""
        state = cls(
            strategy=strategy,
            slots={slot.team_id: slot for slot in slots},
            pool=pool,
            accuracy=accuracy or ScoutingAccuracy(),
        )
        state._normalize()
        for action in actions:
            state.apply(action)
        return state

    # --- 導く値 ---

    def _active(self, team_id: int) -> bool:
        slot = self.slots[team_id]
        if self.is_development:
            return team_id not in self._development_passed and self._development_picked[team_id] < slot.development_max
        return team_id not in self._passed and self._picked[team_id] < slot.maximum

    def _strategy_round(self) -> int:
        """戦略に渡す巡。育成の巡は本指名の最後の巡の続き（本指名が1巡も無ければ1巡目の続きとみなし、育成の1巡目は2巡目。
        NPB 方式で育成の1巡目が入札にならないように）。"""
        if not self.is_development:
            return self.round_number
        return max(1, self._main_last_round) + self.round_number

    def _bidding(self) -> bool:
        return self.strategy.is_bidding(self._strategy_round())

    def _participants(self) -> list[int]:
        return [
            team_id
            for team_id in self.strategy.order(self._strategy_round())
            if team_id in self.slots and self._active(team_id) and team_id not in self._settled
        ]

    def _phase_over(self) -> bool:
        """いまの区分（本指名 / 育成）が終わったか。"""
        limit = MAX_DEVELOPMENT_PICKS if self.is_development else MAX_DRAFTEES
        return self.round_number > limit or not any(self._active(team_id) for team_id in self.slots)

    def _holds_development(self) -> bool:
        """本指名の後に育成の巡を開くか。本指名の合計が「球団数 × `MAX_DRAFTEES`」（NPB の120名）に届いておらず、
        育成の枠のある球団がいるとき。"""
        return len(self.selections) < len(self.slots) * MAX_DRAFTEES and any(
            slot.development_max > 0 for slot in self.slots.values()
        )

    @property
    def is_complete(self) -> bool:
        return self._phase_over() and (self.is_development or not self._holds_development())

    def next_turn(self) -> DraftTurn | None:
        """次に動く球団。会議が終わっていれば None。"""
        if self.is_complete:
            return None
        bidding = self._bidding()
        waiting = [team_id for team_id in self._participants() if not (bidding and team_id in self._bids)]
        if not waiting:  # `_normalize` が巡を進めるので来ない
            return None
        return DraftTurn(
            team_id=waiting[0],
            round_number=self.round_number,
            is_bid=bidding,
            attempt=self.attempt if bidding else 1,
            is_development=self.is_development,
        )

    def picked_positions(self, team_id: int) -> Counter[Position]:
        return Counter(self._positions.get(team_id, Counter()))

    def drafted(self) -> dict[int, list[tuple[Rookie, DraftPick]]]:
        """球団ごとの指名（本指名の巡の順、続けて育成の巡の順）。指名は締める年のドラフトで、順位は区分の中の巡。"""
        result: dict[int, list[tuple[Rookie, DraftPick]]] = {team_id: [] for team_id in self.slots}
        for selection in sorted(
            self.selections, key=lambda item: (item.team_id, item.is_development, item.round_number)
        ):
            prospect = self.pool.get(selection.prospect_key)
            pick = DraftPick(
                year=self.pool.year, round=selection.round_number, is_development=selection.is_development
            )
            result[selection.team_id].append((prospect.rookie, pick))
        return result

    def shortfall_after(self, team_id: int, position: Position | None) -> int:
        """`position` を1人指名した後（None は指名しないまま）の、位置の最低人数までの不足の合計。"""
        counts = Counter(self.slots[team_id].base_counts)
        counts.update(self._positions.get(team_id, Counter()))
        if position is not None:
            counts[position] += 1
        return position_shortfall(counts)

    def position_allowed(self, team_id: int, position: Position) -> bool:
        """位置の最低人数の硬い制約（AI も GM も。規則は `draft.keeps_position_minimum`）。
        育成の巡はどの位置も選べる（位置の最低人数は支配下の話）。"""
        if self.is_development:
            return True
        counts = Counter(self.slots[team_id].base_counts)
        counts.update(self._positions.get(team_id, Counter()))
        remaining = self.slots[team_id].maximum - self._picked[team_id] - 1
        return keeps_position_minimum(counts, position, remaining)

    def selectable(self, team_id: int, prospect: Prospect) -> bool:
        """球団 `team_id` がいま候補 `prospect` を入札・指名できるか（会議の画面の「指名する」の出し分け）。

        `apply` と同じ検査（`_selection_error`）を通すので、画面に出したボタンが `apply` で断られることはない。
        """
        turn = self.next_turn()
        return turn is not None and turn.team_id == team_id and self._selection_error(team_id, prospect) is None

    def passable(self, team_id: int) -> bool:
        """球団 `team_id` がいま選択を終えられるか（会議の画面の「選択終了」の出し分け）。

        `apply` と同じ検査（`_pass_error`）を通すので、画面に出したボタンが `apply` で断られることはない。
        """
        turn = self.next_turn()
        return turn is not None and turn.team_id == team_id and self._pass_error(team_id) is None

    def _pass_error(self, team_id: int) -> str | None:
        """選択を終えられない理由（終えられるなら None）。`apply` と `passable` が使う唯一の検査。

        育成の巡はいつでも選択を終えられる（最少は無く、位置の最低人数は支配下の話）。
        """
        if self.is_development:
            return None
        slot = self.slots[team_id]
        if self._picked[team_id] < slot.minimum:
            return f"少なくとも{slot.minimum}人を指名するまで、選択を終えられません。"
        if self.shortfall_after(team_id, None) > 0:
            return "位置の最低人数に届くまで、選択を終えられません。"
        return None

    @property
    def round_order(self) -> tuple[int, ...]:
        """いまの巡の指名順（会議に参加する球団。選択を終えた球団・指名し終えた球団も含む。会議の画面の「この巡の指名順」）。

        会議が終わっていれば空。"""
        if self.is_complete:
            return ()
        return tuple(team_id for team_id in self.strategy.order(self._strategy_round()) if team_id in self.slots)

    def resolved_moves(self, after_seq: int = 0) -> tuple[DraftMove, ...]:
        """行動の番号 `after_seq` より後の行動で確定した動き（確定した順）。保留中の入札は出さない
        （入札がそろって抽選まで解けたときに、その回の全球団の分がまとめて出る）。"""
        return tuple(move for move in self.moves if move.seq > after_seq)

    def _selection_error(self, team_id: int, prospect: Prospect) -> str | None:
        """候補を入札・指名できない理由（できるなら None）。`apply` と `selectable` が使う唯一の検査。"""
        if prospect.key in self._taken:
            return "その選手は既に指名されています。"
        if self.is_development and not prospect.allows_development_pick:
            return f"{prospect.route.label}の選手は育成で指名できません。"
        if not self.position_allowed(team_id, prospect.position):
            return "位置の最低人数に届かなくなるので、その位置の選手は指名できません。"
        return None

    # --- 1手ずつ進める ---

    def apply(self, action: DraftAction) -> None:
        """行動を1つ当てる。成り立たなければ `InvalidDraftAction`（何も変えない）。"""
        turn = self.next_turn()
        if turn is None:
            raise InvalidDraftAction("ドラフト会議は終わっています。")
        if action.team_id != turn.team_id:
            raise InvalidDraftAction("この球団の番ではありません。")
        if action.kind is DraftActionKind.PASS:
            error = self._pass_error(action.team_id)
            if error is not None:
                raise InvalidDraftAction(error)
            self._record(action)
            self.moves.append(
                DraftMove(len(self.actions), action.team_id, action.kind, self.round_number, self.is_development)
            )
            (self._development_passed if self.is_development else self._passed).add(action.team_id)
            # 入札の巡で最後に残った球団が選択を終えたら、そろった入札をここで解く
            self._resolve_if_ready()
            self._normalize()
            return
        if (action.kind is DraftActionKind.BID) != turn.is_bid:
            raise InvalidDraftAction("この巡は入札です。" if turn.is_bid else "この巡は入札ではありません。")
        pool = self._pool_for(action)
        if action.prospect_key is None:
            raise InvalidDraftAction("指名する選手を選んでください。")
        prospect = pool.get(action.prospect_key)
        error = self._selection_error(action.team_id, prospect)
        if error is not None:
            raise InvalidDraftAction(error)
        if pool is not self.pool:
            self._add_to_rankings(prospect)
        self.pool = pool
        self._record(action)
        if action.kind is DraftActionKind.PICK:
            self._select(action.team_id, prospect, DraftActionKind.PICK)
        else:
            self._bids[action.team_id] = prospect.key
            self._resolve_if_ready()
        self._normalize()

    def _record(self, action: DraftAction) -> None:
        """当てた行動を覚える（本指名なら、行動のあった最後の巡も。育成の巡の順番の起点）。"""
        self.actions.append(action)
        if not self.is_development:
            self._main_last_round = self.round_number

    def _resolve_if_ready(self) -> None:
        """入札の巡で、まだ決まっていない全球団の入札がそろっていれば解く。"""
        if not self._bidding() or not self._bids:
            return
        if all(team_id in self._bids for team_id in self._participants()):
            self._resolve_bids()

    def _pool_for(self, action: DraftAction) -> ProspectPool:
        if action.extra_position is None:
            return self.pool
        if action.prospect_key != len(self.pool):
            raise InvalidDraftAction("足す候補の番号が正しくありません。")
        return self.pool.with_extra(action.extra_position, development=self.is_development)

    def _select(self, team_id: int, prospect: Prospect, kind: DraftActionKind, lottery: Lottery | None = None) -> None:
        """指名を確定する（`kind` は指名か入札の確定か。動きの記録に使う）。"""
        development = self.is_development
        self.selections.append(DraftSelection(team_id, prospect.key, self.round_number, is_development=development))
        self.moves.append(
            DraftMove(
                len(self.actions),
                team_id,
                kind,
                self.round_number,
                development,
                prospect_key=prospect.key,
                lottery=lottery,
            )
        )
        self._taken.add(prospect.key)
        picked, positions = (
            (self._development_picked, self._development_positions) if development else (self._picked, self._positions)
        )
        picked[team_id] += 1
        positions.setdefault(team_id, Counter())[prospect.position] += 1
        self._settled.add(team_id)

    def _resolve_bids(self) -> None:
        """入札がそろった回を解く。単独は確定、重複は抽選で1球団。外れた球団は次の回に再入札する。"""
        by_prospect: dict[int, list[int]] = {}
        for team_id, key in self._bids.items():
            by_prospect.setdefault(key, []).append(team_id)
        for key in sorted(by_prospect):
            bidders = tuple(sorted(by_prospect[key]))
            lottery: Lottery | None = None
            if len(bidders) == 1:
                winner = bidders[0]
            else:
                section = "development" if self.is_development else "main"
                rng = make_random(
                    game_seed(
                        self.pool.world_seed,
                        self.pool.year,
                        f"draft-lottery-{section}-{self.round_number}-{self.attempt}-{key}",
                    )
                )
                winner = bidders[min(len(bidders) - 1, int(rng.random() * len(bidders)))]
                lottery = Lottery(
                    self.round_number, self.attempt, key, bidders, winner, is_development=self.is_development
                )
                self.lotteries.append(lottery)
            self._select(winner, self.pool.get(key), DraftActionKind.BID, lottery)
        self._bids = {}
        self.attempt += 1

    def _normalize(self) -> None:
        """動ける球団が残っていない巡を進める。本指名が終わったら、育成の巡を開くなら育成の1巡目へ移る。"""
        while True:
            if self._phase_over():
                if self.is_development or not self._holds_development():
                    return
                self.is_development = True
                self.round_number = 0  # 下で1巡目にする
            elif self._participants():
                return
            self.round_number += 1
            self.attempt = 1
            self._bids = {}
            self._settled = set()

    # --- AI ---

    def taste(self, team_id: int) -> DraftTaste:
        """球団の好み（`draft_taste`。世界のシードと球団から導く）。"""
        taste = self._tastes.get(team_id)
        if taste is None:
            taste = self._tastes[team_id] = draft_taste(self.pool.world_seed, team_id)
        return taste

    def _tasted(self) -> bool:
        """いまの巡で AI の評価に球団の好みを入れるか（本指名の1巡目が入札のとき＝1位の入札だけ。
        外れ1位の再入札も含む）。

        2巡目以降と育成の巡は将来値だけで選ぶ。好みを全巡に入れると、指名された新人の経路の割合と平均が動き、
        候補の合わせ込み（`POOL_*`）をやり直すことになる（48球団の合成の会議で、大学の割合が 52% → 47%）。
        1巡目だけなら、外れた候補は2巡目以降に将来値で取られ、新人の分布は変わらない。
        1巡目が入札でない規則（完全ウェーバー・くじ順）は競合が起きないので入れない（前の球団ほど良い新人を取る形を保つ）。"""
        return not self.is_development and self.round_number == 1 and self._bidding()

    def report(self, team_id: int, prospect: Prospect) -> ScoutingReport:
        """球団 `team_id` のスカウトの見立て（`scouting_report`。会議の精度で。初めて要るときに作って覚える）。"""
        report = self._reports.get((team_id, prospect.key))
        if report is None:
            report = self._reports[(team_id, prospect.key)] = scouting_report(
                prospect, team_id, world_seed=self.pool.world_seed, year=self.pool.year, accuracy=self.accuracy
            )
        return report

    def evaluation(self, team_id: int, prospect: Prospect, *, tasted: bool = False) -> float:
        """球団 `team_id` から見た候補の評価（スカウトの見立てから）。`tasted` なら球団の好み（`DraftTaste`）と
        惚れ込み（`draft_liking`）を入れ、そうでなければ将来値（`future_value`）。"""
        if not tasted:
            future = self._futures.get((team_id, prospect.key))
            if future is None:
                future = future_value(prospect, self.report(team_id, prospect))
                self._futures[(team_id, prospect.key)] = future
            return future
        report = self.report(team_id, prospect)
        liking = draft_liking(self.pool.world_seed, self.pool.year, team_id, prospect.key)
        return self.taste(team_id).evaluate(prospect, report, liking=liking)

    def _ranking(self, team_id: int, position: Position, tasted: bool) -> list[tuple[float, int]]:
        """球団から見た、位置の候補の評価の高い順（同値は番号の順）。初めて要るときに作って覚える。"""
        ranking = self._rankings.get((team_id, position, tasted))
        if ranking is None:
            ranking = sorted(
                (
                    (-self.evaluation(team_id, prospect, tasted=tasted), prospect.key)
                    for prospect in self.pool.prospects
                    if prospect.position is position
                ),
            )
            self._rankings[(team_id, position, tasted)] = ranking
        return ranking

    def _add_to_rankings(self, prospect: Prospect) -> None:
        """足した候補を、作り済みの順位に差し込む（いつ作った順位でも、同じ候補の並びになる）。"""
        for (team_id, position, tasted), ranking in self._rankings.items():
            if position is prospect.position:
                bisect.insort(ranking, (-self.evaluation(team_id, prospect, tasted=tasted), prospect.key))

    def ai_action(self) -> DraftAction:
        """次の番の球団を AI として動かす行動。計画した位置の残りの中から、評価がいちばん高い候補を選ぶ。

        評価は `将来値 + NEED_WEIGHT × 位置の残り`（1位の入札は将来値の代わりに好みを入れた評価。`_tasted`）。
        計画した位置を指名し尽くしていれば（GM が別の位置を選んだ後のお任せ）、位置の最低人数を割らない候補から
        同じ評価で選ぶ。その位置の候補が尽きていれば、その場で1人足す。
        本指名では育成の層の候補（`Prospect.development_tier`。#362）を見ない。
        育成の巡は育成の位置の計画（`DraftSlot.development_planned`）で、育成で指名できる候補
        （`Prospect.allows_development_pick`）の中から同じように選ぶ。計画の人数に達したら選択を終える
        （GM の球団は育成の最多が計画より多い（`meeting_slots`）ので、AI が進めても計画の人数で止める）。
        """
        turn = self.next_turn()
        if turn is None:
            raise InvalidDraftAction("ドラフト会議は終わっています。")
        team_id = turn.team_id
        kind = DraftActionKind.BID if turn.is_bid else DraftActionKind.PICK
        slot = self.slots[team_id]
        if turn.is_development and self._development_picked[team_id] >= len(slot.development_planned):
            return DraftAction(DraftActionKind.PASS, team_id)
        if turn.is_development:
            needed = Counter(slot.development_planned)
            needed.subtract(self._development_positions.get(team_id, Counter()))
        else:
            needed = Counter(slot.planned)
            needed.subtract(self._positions.get(team_id, Counter()))
        order = list(generator.POSITION_RATIOS)
        any_needed = any(count > 0 for count in needed.values())
        allowed = [
            position
            for position in order
            if self.position_allowed(team_id, position) and (needed[position] > 0 or not any_needed)
        ]
        tasted = self._tasted()
        best: tuple[float, int] | None = None
        for position in allowed:
            top = next(
                (
                    item
                    for item in self._ranking(team_id, position, tasted)
                    if item[1] not in self._taken and self._ai_considers(self.pool.get(item[1]), turn.is_development)
                ),
                None,
            )
            if top is None:
                continue
            score = -top[0] + NEED_WEIGHT * max(0, needed[position])
            if best is None or (score, -top[1]) > (best[0], -best[1]):
                best = (score, top[1])
        if best is not None:
            return DraftAction(kind, team_id, best[1])
        position = max(allowed or order, key=lambda item: (needed[item], -order.index(item)))
        return DraftAction(kind, team_id, len(self.pool), extra_position=position)

    @staticmethod
    def _ai_considers(prospect: Prospect, development: bool) -> bool:
        """AI が指名を考える候補か。育成の巡は育成で指名できる候補（社会人でない。#307）、本指名は育成の層でない候補
        （#362。育成の層は今の能力が低く伸びしろで選ばれるので、本指名で見ると将来値の高い高校生に偏る）。"""
        return prospect.allows_development_pick if development else not prospect.development_tier

    def advance_ai(self, *, stop_at: int | None = None) -> list[DraftAction]:
        """`stop_at` の球団の番か、会議の終わりまで AI を進める。当てた行動を返す。"""
        applied: list[DraftAction] = []
        while (turn := self.next_turn()) is not None and turn.team_id != stop_at:
            action = self.ai_action()
            self.apply(action)
            applied.append(action)
        return applied

    def auto_complete(self) -> list[DraftAction]:
        """残りを全部 AI で進める（会議を始めなかった年の締める処理・管理コマンド・お任せ）。"""
        return self.advance_ai(stop_at=None)


@dataclass(frozen=True)
class DraftResult:
    """会議の結果（新人と、抽選の経過）。新人の能力は錨の前。"""

    draftees: tuple[Draftee, ...]
    lotteries: tuple[Lottery, ...]
    state: DraftState


@dataclass(frozen=True)
class MeetingSetup:
    """会議を開く設定（`open_meeting` の結果）。会議を始めるとき（D3）と締めるときが同じ関数で作る。

    - `teams`: 引退・戦力外・外国人の獲得の後の球団（球団 id の昇順）
    - `plans`: 球団ごとの新人の計画（`draft.plan_team_draft`。背番号と新人の作り方に使う）
    - `slots`: 人数の幅（`meeting_slots`）
    - `pool`: 候補（`meeting_pool`。足した候補の無いもの）
    - `strategy`: 指名順の戦略（世界の規則と最終順位から）
    """

    year: int
    world_seed: int
    teams: tuple[DraftTeam, ...]
    plans: Mapping[int, TeamDraftPlan]
    slots: tuple[DraftSlot, ...]
    pool: ProspectPool
    strategy: DraftOrderStrategy


def meeting_pool(
    slots: Sequence[DraftSlot], *, world_seed: int, year: int, used_names: Collection[str]
) -> ProspectPool:
    """人数の幅から会議の候補を作る（`draw_pool`。位置ごとの人数は全球団の `planned` の合計、
    育成の層は全球団の `development_planned` の合計）。

    **会議の候補を作る唯一の関数**。会議を開くとき（`open_meeting`）と、会議の途中に保存した人数の幅から候補を
    作り直すとき（D3 の1手ごと）が同じ関数を通るので、同じ幅と名前なら同じ候補（同じ指紋）になる。

    渡す名前は、会議を開くときは世界の全選手と獲得した外国人、作り直すときは世界の全選手だけ（外国人の獲得を
    計算し直さない）。獲得した外国人の名前（`generator.foreign_name` のカタカナの「名・姓」）は候補の名前
    （`generator.japanese_name` の漢字の氏名）と重ならないので、どちらでも同じ候補になる。前提が崩れても黙っては
    壊れない（指紋が違って `DraftChanged`）。
    """
    planned = Counter(position for slot in slots for position in slot.planned)
    development = Counter(position for slot in slots for position in slot.development_planned)
    return draw_pool(planned, world_seed=world_seed, year=year, used_names=used_names, development_planned=development)


def open_meeting(
    teams: Sequence[DraftTeam],
    context: DraftContext,
    *,
    year: int,
    world_seed: int,
    used_names: set[str],
    signed: Mapping[int, Sequence[Position]] | None = None,
) -> MeetingSetup:
    """締める年 `year` の会議の設定を作る（人数の幅・候補・指名順。行動はまだ当てない）。

    `teams` は引退・戦力外・外国人の獲得の後の球団（球団 id の昇順に処理する）。`signed` は球団ごとの、会議の前に
    獲得した外国人の位置（`foreign_market.OffseasonSignings.signed_positions`。日本人の人数と位置から除く）。
    `used_names` は世界の全選手の名前で、候補の名前を足していく。
    """
    ordered = tuple(sorted(teams, key=lambda team: team.team_id))
    missing = {team.team_id for team in ordered} - context.standings.team_ids
    if missing:
        raise ValueError(f"最終順位に無い球団があります（{sorted(missing)}）。")
    plans = {team.team_id: plan_team_draft(team, (signed or {}).get(team.team_id, ())) for team in ordered}
    slots = meeting_slots(ordered, plans, managed_team_id=context.managed_team_id)
    pool = meeting_pool(slots, world_seed=world_seed, year=year, used_names=used_names)
    used_names.update(pool.taken_names)
    strategy = strategy_for(context.rule, context.standings, world_seed=world_seed, year=year)
    return MeetingSetup(
        year=year, world_seed=world_seed, teams=ordered, plans=plans, slots=slots, pool=pool, strategy=strategy
    )


def finish_draft(setup: MeetingSetup, state: DraftState, *, used_names: set[str]) -> DraftResult:
    """会議の残りを AI で最後まで進め（終わっていれば何もしない）、新人を作る。

    新人の背番号は球団ごとの乱数（`draft-<球団 id>`、年は翌年）で引く。足した候補の名前を `used_names` に足す。
    """
    state.auto_complete()
    used_names.update(state.pool.taken_names)
    drafted = state.drafted()
    draftees: list[Draftee] = []
    for team in setup.teams:
        rng = make_random(game_seed(setup.world_seed, setup.year + 1, f"draft-{team.team_id}"))
        draftees.extend(club_draftees(rng, team, setup.plans[team.team_id], drafted=drafted[team.team_id]))
    return DraftResult(draftees=tuple(draftees), lotteries=tuple(state.lotteries), state=state)


def run_draft(
    teams: Sequence[DraftTeam],
    context: DraftContext,
    *,
    year: int,
    world_seed: int,
    used_names: set[str],
    actions: Sequence[DraftAction] = (),
    signed: Mapping[int, Sequence[Position]] | None = None,
) -> DraftResult:
    """締める年 `year` の会議を開き（`open_meeting`）、`actions` を当ててから残りを AI で進め、新人を作る
    （`finish_draft`）。引数は `open_meeting` と同じ。

    会議のエンジンを試すための入口（テストが使う）。締める処理は `offseason.plan_offseason` を通り、
    GM が開いた会議は人数の幅と候補の指紋を照合してから再生する（`offseason.replay_meeting`）。
    """
    setup = open_meeting(teams, context, year=year, world_seed=world_seed, used_names=used_names, signed=signed)
    return finish_draft(
        setup, DraftState.open(setup.strategy, setup.slots, setup.pool, actions), used_names=used_names
    )
