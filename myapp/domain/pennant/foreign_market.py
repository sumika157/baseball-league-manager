"""外国人の獲得（#169）。**純粋な関数と値**で、DB にも Django にも触れない。

外国人は**球団ごとの候補**から獲得する（NPB の会議に外国人はいない）。オフは締める処理の中で、戦力外の後・
ドラフト会議の前に獲得する（`offseason.py` の順序は 引退 → 戦力外 → **外国人の獲得** → ドラフト会議 → 成長 → 錨）。

- **補う人数**は今までどおり `draft.foreign_count_to_add`（外国人の目標 `draft.foreign_target` までの不足。戦力外で
  外した外国人の入れ替え。#168）。獲得した外国人は**新しい支配下の選手の枠の内側**で数え、日本人の新人は
  「新人の人数 − 獲得した外国人」（`draft.plan_team_draft`。#169 Q2）
- **候補**は球団・年・局面（`SigningPhase`）から世界のシードで導き、保存しない（`draw_candidates`）。位置ごとの人数は
  `CANDIDATES_BY_POSITION`（捕手はいない）。乱数は候補ごとに別の種（`foreign-<局面>-<球団>-<何回目>-<添字>`）で、
  名前は能力と別の流れ（`…-name-…`）、伸びしろも別の流れ（`…-potential-…`）。名前を引き直しても能力・年齢・位置は動かず、
  他球団の獲得で候補は変わらない（同じ名前の外国人が既にいれば、名前だけ `…-rename-…` で引き直す）
- **前所属**（`ForeignBackground`。区分の出典は domain の値オブジェクト）ごとに年齢の範囲と能力のずらしを変える
  （`BACKGROUNDS`）。獲得した選手のプロフィールにも保存する（#169 Q5）
- 候補の本当の能力は見せない。球団は**スカウトの見立て**（`draft_meeting.scout_with_noise`。会議の候補と同じ誤差
  `SCOUT_SD`・同じ刻み）で見比べる
- **AI**（`choose_signings`）は、見立ての総合（`draft_meeting.scouting_value`）に、位置の必要度（その位置の
  **戦力の穴** × `HOLE_WEIGHT`。#169 F1b）を足した評価が大きい候補から選ぶ。戦力は支配下の今の能力の、1軍の単位
  （`SquadUnit`。内野・外野の打線と先発・救援）ごとの上位の平均（`squad_strength`）、穴は世界の全球団の平均との差
  （`squad_holes`。+ が弱い）。1人獲得するごとに、その候補の見立てを戦力に入れて穴を数え直す。位置の最低人数
  （`draft.MIN_BY_POSITION`）は会議と同じ硬い制約（`draft.keeps_position_minimum`。残る新人の枠で最低人数に届く間だけ、
  足りている位置を選べる）
- **ゲームの上限**（#169 Q4。年俸・契約年数が無い間の代わり）: 外国人の保有は `MAX_FOREIGN_HELD` 人まで、
  シーズン中の獲得は1年 `MAX_MIDSEASON_SIGNINGS` 人まで（`ensure_signable`。AI も通る）
- **合わせ込み**: 候補の能力の平均は `SIGNED_RATING_MEAN + POOL_RATING_SHIFT`（+ 前所属のずらし）。AI は候補の上から
  取るので、**獲得した外国人**の錨の前の平均が今までの外国人の新人と同じ50になるよう、`POOL_RATING_SHIFT` を
  定常状態の実測で決めた（`test_offseason_population.py` が見張る）。戦力外が新しく迎える外国人と比べる見込み
  （`release.foreign_prospect_value`）の出典は `expected_signing_ratings` と `expected_signing_ages`

GM の指名（F2）とシーズン中の獲得（F3）は、ここに後から足す（局面 `SigningPhase.MIDSEASON` と `ensure_signable` の
1年の上限は、その土台）。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Mapping, MutableSet, Sequence
from dataclasses import dataclass, replace
from datetime import date
from enum import Enum
from statistics import fmean

from ..exceptions import InvalidSigning
from ..simulation.estimate import draw_growth_type, draw_potential, with_growth, with_potential
from ..simulation.manager import ROTATION_SIZE
from ..simulation.randomness import game_seed, make_random, weighted_index
from ..simulation.ratings import AVERAGE_RATING, PitcherRatings
from ..value_objects import AcquisitionRoute, ContractStatus, ForeignBackground, Position, Profile
from ..virtual_players import generator
from .draft import (
    DraftRoute,
    DraftTeam,
    draw_number,
    foreign_count_to_add,
    keeps_position_minimum,
    newcomer_plan,
)
from .draft_meeting import ScoutingAccuracy, ScoutingReport, scout_noise, scout_with_noise, scouting_value
from .initial_ratings import SEASON_START_MONTH, STRENGTH_UNIT
from .offseason_player import Ratings
from .prospects import draw_prospect_ratings, expected_prospect_ratings

# 1回の候補の、位置ごとの人数（計8人。捕手はいない＝外国人の捕手は NPB でもまれ）
CANDIDATES_BY_POSITION: Mapping[Position, int] = {
    Position.PITCHER: 4,
    Position.INFIELDER: 2,
    Position.OUTFIELDER: 2,
}
# 外国人の保有の上限（ゲームの上限。協約は保有の人数を縛らない。#169 Q4）
MAX_FOREIGN_HELD = 6
# シーズン中の獲得の、1球団1年の上限（#169 Q4。F3 が使う）
MAX_MIDSEASON_SIGNINGS = 1
# 獲得した外国人の能力の平均（錨の前）。今までの外国人の新人と同じ（`release.foreign_prospect_value` の出典）
SIGNED_RATING_MEAN = 50.0
# 候補の能力の平均のずらし（点）と標準偏差。AI は候補の上から取るので、**獲得した外国人**の錨の前の平均が
# `SIGNED_RATING_MEAN`・標準偏差が今までの外国人の新人と同じ6に近くなるよう、定常状態の実測で決めた値
# （Wiki「ペナントモードの仕組み」の「実測値」）
POOL_RATING_SHIFT = -3.8
# 投手の候補だけに足すずらし（投手は見比べる項目が3つで野手の5つより総合が散らばり、上から取ると高く出るため。
# ドラフト会議の `draft_meeting.POOL_PITCHER_SHIFT` と同じ考え）
POOL_PITCHER_SHIFT = -1.3
POOL_RATING_SD = 7.0
# AI の評価: 見立ての総合 + HOLE_WEIGHT × その位置の戦力の穴（点。世界の平均との差）。実測で、外国人が投手か
# 野手の一方に寄らず、穴の大きい側を多く取る値に決めた（Wiki「ペナントモードの仕組み」の「実測値」）
HOLE_WEIGHT = 1.0
# 空いた枠の能力（単位の人数に満たない球団。2軍の層の水準）
VACANT_RATING = 40.0


class SquadUnit(Enum):
    """戦力の単位（1軍の打線と投手陣）。値は画面に出す名前。"""

    INFIELD = "内野の打線"
    OUTFIELD = "外野の打線"
    ROTATION = "先発"
    BULLPEN = "救援"


# 単位ごとの、上位を数える人数（内野4・外野3の打線、先発6人・救援6人）。捕手は外国人を獲得しないので数えない
SQUAD_SIZES: Mapping[SquadUnit, int] = {
    SquadUnit.INFIELD: 4,
    SquadUnit.OUTFIELD: 3,
    SquadUnit.ROTATION: ROTATION_SIZE,
    SquadUnit.BULLPEN: 6,
}
# 位置ごとに見る単位（投手は先発と救援の平均）
UNITS_BY_POSITION: Mapping[Position, tuple[SquadUnit, ...]] = {
    Position.INFIELDER: (SquadUnit.INFIELD,),
    Position.OUTFIELDER: (SquadUnit.OUTFIELD,),
    Position.PITCHER: (SquadUnit.ROTATION, SquadUnit.BULLPEN),
}

Squad = Sequence[tuple[Position, Ratings]]


class SigningPhase(Enum):
    """獲得の局面。値は画面に出す名前。候補の種に入る（オフと期中で別の候補）。"""

    OFFSEASON = "オフ"
    MIDSEASON = "シーズン中"

    @property
    def label(self) -> str:
        return self.value


@dataclass(frozen=True)
class BackgroundDistribution:
    """前所属の区分ごとの候補の分布。`weight` は候補に占める割合（相対）、`age_range` は入団の年の4月1日時点の
    満年齢の範囲（両端を含む）、`rating_shift` は能力の平均のずらし（点）。"""

    weight: float
    age_range: tuple[int, int]
    rating_shift: float


# 区分ごとの分布。混ぜた全体が今までの外国人の新人（平均50・25〜31歳・平均28歳）に近くなる値
# （割合で重みを付けたずらしの平均は −0.15点、年齢の平均は 27.9歳）。
# メジャー経験は年上で能力が高く、マイナーは若く能力が低い。アジア（韓国・台湾）のリーグはその間
BACKGROUNDS: Mapping[ForeignBackground, BackgroundDistribution] = {
    ForeignBackground.MAJOR_LEAGUE: BackgroundDistribution(weight=0.30, age_range=(28, 31), rating_shift=2.0),
    ForeignBackground.MINOR_LEAGUE: BackgroundDistribution(weight=0.50, age_range=(25, 28), rating_shift=-1.5),
    ForeignBackground.ASIAN_LEAGUE: BackgroundDistribution(weight=0.20, age_range=(27, 31), rating_shift=0.0),
}


@dataclass(frozen=True)
class CandidateKey:
    """候補の識別。締める年（オフ）か、その年（期中）・局面・その年の何回目か（期中の2人目以降。オフは0）・球団の中の添字。"""

    year: int
    phase: SigningPhase
    nth: int
    index: int

    def seed_key(self, team_id: int, stream: str = "") -> str:
        """乱数の用途（`game_seed` の鍵）。`stream` は流れの名前（空は能力の流れ）。"""
        suffix = f"-{stream}" if stream else ""
        return f"foreign-{self.phase.name.lower()}{suffix}-{team_id}-{self.nth}-{self.index}"


@dataclass(frozen=True)
class ForeignCandidate:
    """候補ひとり。能力は錨の前で、本当の値（球団にはスカウトの見立て `scouting_report` しか見せない）。

    `age` は入団する年の4月1日時点の満年齢。`surname_romaji`・`given_romaji` は背ネームの材料。
    """

    team_id: int
    key: CandidateKey
    position: Position
    background: ForeignBackground
    name: str
    surname_romaji: str
    given_romaji: str
    profile: Profile
    ratings: Ratings
    age: int


@dataclass(frozen=True)
class ForeignSigning:
    """獲得した外国人。支配下で入り、入団の経路は新外国人。能力は錨の前（`offseason.py` が錨をかけて差し替える）。"""

    team_id: int
    candidate: ForeignCandidate
    number: str
    name: str
    profile: Profile
    ratings: Ratings

    @property
    def position(self) -> Position:
        return self.candidate.position

    @property
    def contract(self) -> ContractStatus:
        return ContractStatus.REGISTERED

    @property
    def acquisition_route(self) -> AcquisitionRoute:
        return AcquisitionRoute.NEW_FOREIGN


@dataclass(frozen=True)
class OffseasonSignings:
    """オフの獲得の結果。`teams` は獲得した外国人の背番号・苗字・人数を足した球団（ドラフト会議に渡す）。"""

    signings: tuple[ForeignSigning, ...]
    teams: tuple[DraftTeam, ...]

    @property
    def signed_positions(self) -> dict[int, tuple[Position, ...]]:
        """球団ごとの、獲得した外国人の位置（獲得した順。`draft.plan_team_draft` が日本人の位置から除く）。"""
        result: dict[int, tuple[Position, ...]] = {}
        for signing in self.signings:
            result[signing.team_id] = (*result.get(signing.team_id, ()), signing.position)
        return result


def _season_year(year: int, phase: SigningPhase) -> int:
    """候補が入団する年（オフは翌年、期中はその年）。"""
    return year + 1 if phase is SigningPhase.OFFSEASON else year


def draw_candidates(
    team_id: int,
    *,
    year: int,
    phase: SigningPhase,
    world_seed: int,
    used_names: Collection[str],
    nth: int = 0,
) -> tuple[ForeignCandidate, ...]:
    """球団 `team_id` の候補（`CANDIDATES_BY_POSITION` の位置の順）。`year` はオフなら締める年、期中はその年。

    `used_names` は世界の全選手の名前（候補の名前が重ならないように。同じ球団の候補どうしも重ならない）。
    同じ引数なら何度引いても同じ候補になる（保存しない）。
    """
    names = set(used_names)
    positions = [position for position, count in CANDIDATES_BY_POSITION.items() for _ in range(count)]
    return tuple(
        _draw_candidate(team_id, CandidateKey(year, phase, nth, index), position, world_seed=world_seed, names=names)
        for index, position in enumerate(positions)
    )


def _draw_candidate(
    team_id: int, key: CandidateKey, position: Position, *, world_seed: int, names: MutableSet[str]
) -> ForeignCandidate:
    """候補ひとり。能力の流れは 前所属 → 年齢 → 生年月日 → 投打 → 体格 → 能力 → 成長型 の順。"""
    rng = make_random(game_seed(world_seed, key.year, key.seed_key(team_id)))
    name_rng = make_random(game_seed(world_seed, key.year, key.seed_key(team_id, "name")))
    overseas = generator.foreign_name(name_rng, names)
    backgrounds = list(BACKGROUNDS)
    background = backgrounds[weighted_index(rng, [BACKGROUNDS[item].weight for item in backgrounds])]
    distribution = BACKGROUNDS[background]
    age = generator.randint(rng, *distribution.age_range)
    season_start = date(_season_year(key.year, key.phase), SEASON_START_MONTH, 1)
    birth_date = generator.birth_date_for(rng, age=age, as_of=season_start)
    throws, bats = generator.handedness(rng, position)
    height_cm, weight_kg = generator.physique(rng, position)
    ratings = draw_prospect_ratings(
        rng,
        mean=SIGNED_RATING_MEAN
        + POOL_RATING_SHIFT
        + distribution.rating_shift
        + (POOL_PITCHER_SHIFT if position.is_pitcher else 0.0),
        position=position,
        sd=POOL_RATING_SD,
    )
    value = ratings.pitching_value if isinstance(ratings, PitcherRatings) else ratings.batting_value
    growth = draw_growth_type(rng, age=age, strength=(value - AVERAGE_RATING) / STRENGTH_UNIT)
    potential = draw_potential(make_random(game_seed(world_seed, key.year, key.seed_key(team_id, "potential"))))
    profile = Profile(
        birth_date=birth_date,
        throws=throws,
        bats=bats,
        height_cm=height_cm,
        weight_kg=weight_kg,
        birthplace=overseas.country,
        nationality=overseas.country,
        name_kana=overseas.kana,
        is_foreign_player=True,
        foreign_background=background,
    )
    return ForeignCandidate(
        team_id=team_id,
        key=key,
        position=position,
        background=background,
        name=overseas.name,
        surname_romaji=overseas.surname_romaji,
        given_romaji=overseas.given_romaji,
        profile=profile,
        ratings=with_potential(with_growth(ratings, growth), potential),
        age=age,
    )


def scouting_report(
    candidate: ForeignCandidate,
    *,
    world_seed: int,
    accuracy: ScoutingAccuracy = ScoutingAccuracy(),  # noqa: B008  不変の値（frozen）なので既定値に置いてよい
) -> ScoutingReport:
    """候補の球団のスカウトが見た候補（会議の候補と同じ誤差と刻み。種は `foreign-…-scout-…`）。"""
    key = candidate.key
    text = f"{world_seed}:{key.year}:{key.seed_key(candidate.team_id, 'scout')}"
    return scout_with_noise(candidate.ratings, scout_noise(text, candidate.ratings), accuracy=accuracy)


def _top_mean(values: Sequence[float], size: int) -> float:
    """上位 `size` 個の平均。足りない分は `VACANT_RATING`。"""
    top = sorted(values, reverse=True)[:size]
    return (sum(top) + VACANT_RATING * (size - len(top))) / size


def squad_strength(squad: Squad) -> dict[SquadUnit, float]:
    """球団の戦力（単位ごとの上位の平均）。`squad` は支配下の選手の位置と今の能力。

    打線は位置ごとの打撃（`batting_value`）の上位、先発は投手の `starter_value` の上位、救援は先発の上位を除いた
    投手の抑える力（`pitching_value`）の上位。同じ値の投手は並びの前を先発にする。
    """
    batters: dict[Position, list[float]] = {Position.INFIELDER: [], Position.OUTFIELDER: []}
    pitchers: list[PitcherRatings] = []
    for position, ratings in squad:
        if isinstance(ratings, PitcherRatings):
            pitchers.append(ratings)
        elif position in batters:
            batters[position].append(ratings.batting_value)
    rotation_size = SQUAD_SIZES[SquadUnit.ROTATION]
    order = sorted(range(len(pitchers)), key=lambda index: -pitchers[index].starter_value)
    return {
        SquadUnit.INFIELD: _top_mean(batters[Position.INFIELDER], SQUAD_SIZES[SquadUnit.INFIELD]),
        SquadUnit.OUTFIELD: _top_mean(batters[Position.OUTFIELDER], SQUAD_SIZES[SquadUnit.OUTFIELD]),
        SquadUnit.ROTATION: _top_mean(
            [pitchers[index].starter_value for index in order[:rotation_size]], rotation_size
        ),
        SquadUnit.BULLPEN: _top_mean(
            [pitchers[index].pitching_value for index in order[rotation_size:]], SQUAD_SIZES[SquadUnit.BULLPEN]
        ),
    }


def world_strength(squads: Collection[Squad]) -> dict[SquadUnit, float]:
    """世界の戦力の平均（単位ごと）。穴を測る基準。能力の錨（50 = 1軍の平均）は世界全体で決めるので、比べる相手も
    世界の全球団（リーグが複数の世界でも）。球団が無ければ全単位0。"""
    strengths = [squad_strength(squad) for squad in squads]
    return {unit: fmean(strength[unit] for strength in strengths) if strengths else 0.0 for unit in SquadUnit}


def squad_holes(squad: Squad, reference: Mapping[SquadUnit, float]) -> dict[SquadUnit, float]:
    """戦力の穴（単位ごと。基準 − 球団の戦力。+ が弱い）。"""
    strength = squad_strength(squad)
    return {unit: reference[unit] - strength[unit] for unit in SquadUnit}


def position_hole(holes: Mapping[SquadUnit, float], position: Position) -> float:
    """位置の穴（`UNITS_BY_POSITION` の単位の穴の平均。投手は先発と救援）。外国人を獲得しない位置は0。"""
    units = UNITS_BY_POSITION.get(position, ())
    return fmean(holes[unit] for unit in units) if units else 0.0


def choose_signings(
    candidates: Sequence[ForeignCandidate],
    count: int,
    team: DraftTeam,
    *,
    reference: Mapping[SquadUnit, float],
    world_seed: int,
) -> tuple[ForeignCandidate, ...]:
    """AI が球団 `team` のために獲得する候補（獲得する順。ふつうは `count` 人）。

    評価は見立ての総合（`scouting_value`）+ `HOLE_WEIGHT` × その位置の穴（`position_hole`。球団の戦力
    `team.squad` を基準 `reference`＝`world_strength` と比べる）。1人選ぶごとに、その候補の見立ての能力を戦力に
    入れて穴を数え直す（基準は動かさない）。同じ評価は候補の添字の小さい方。候補の並びには依存しない。

    位置の最低人数は硬い制約: 足りない位置はいつでも選べ、ほかの位置は、獲得の後に残る新しい支配下の選手の枠
    （`draft.newcomer_plan` の人数 − 獲得した人数）で最低人数までの不足を埋められる間だけ選べる（会議の指名と同じ
    `draft.keeps_position_minimum`）。選べる候補が尽きたら `count` 人より少なく獲得する（捕手だけが足りないなど）。
    """
    if count > len(candidates):
        raise ValueError("候補より多くは獲得できません。")
    reports = {candidate.key: scouting_report(candidate, world_seed=world_seed).ratings for candidate in candidates}
    values = {key: scouting_value(ratings) for key, ratings in reports.items()}
    slots, _ = newcomer_plan(team)
    counts: Counter[Position] = Counter(team.position_counts)
    squad = list(team.squad)
    remaining = sorted(candidates, key=lambda candidate: candidate.key.index)
    chosen: list[ForeignCandidate] = []
    for _ in range(count):
        open_slots = slots - len(chosen) - 1
        allowed = [
            candidate for candidate in remaining if keeps_position_minimum(counts, candidate.position, open_slots)
        ]
        if not allowed:
            break
        holes = squad_holes(squad, reference)
        best = max(
            allowed,
            key=lambda candidate: (
                values[candidate.key] + HOLE_WEIGHT * position_hole(holes, candidate.position),
                -candidate.key.index,
            ),
        )
        remaining.remove(best)
        counts[best.position] += 1
        squad.append((best.position, reports[best.key]))
        chosen.append(best)
    return tuple(chosen)


def ensure_signable(
    chosen: Sequence[CandidateKey],
    candidates: Sequence[ForeignCandidate],
    *,
    held_foreign: int,
    phase: SigningPhase,
    signed_this_season: int = 0,
) -> None:
    """獲得として成立するか。成立しなければ `InvalidSigning`。

    - `chosen`: 獲得する候補。候補（`candidates`）の中から、重複なく
    - `held_foreign`: 獲得の前に球団が保有する外国人の人数（育成も数える）。獲得の後が `MAX_FOREIGN_HELD` 人まで
    - `signed_this_season`: シーズン中の局面で、その年に既に獲得した人数。合わせて `MAX_MIDSEASON_SIGNINGS` 人まで
    """
    if len(set(chosen)) != len(chosen):
        raise InvalidSigning("同じ候補が重複しています。")
    known = {candidate.key for candidate in candidates}
    if any(key not in known for key in chosen):
        raise InvalidSigning("その選手は候補にいません。")
    if held_foreign + len(chosen) > MAX_FOREIGN_HELD:
        raise InvalidSigning(
            f"外国人選手の保有は{MAX_FOREIGN_HELD}人までです（獲得すると{held_foreign + len(chosen)}人）。"
        )
    if phase is SigningPhase.MIDSEASON and signed_this_season + len(chosen) > MAX_MIDSEASON_SIGNINGS:
        raise InvalidSigning(f"シーズン中の外国人選手の獲得は1年に{MAX_MIDSEASON_SIGNINGS}人までです。")


def offseason_signings(
    teams: Sequence[DraftTeam], *, year: int, world_seed: int, used_names: MutableSet[str]
) -> OffseasonSignings:
    """締める年 `year` のオフに、AI が球団ごとに外国人を獲得する（球団 id の昇順）。入団は翌年。

    `teams` は引退・戦力外の後の**世界の全球団**（穴の基準 `world_strength` を全球団の戦力から獲得の前に決める）。
    補う人数は `foreign_count_to_add`（新しい支配下の選手の枠の内側）、位置の必要度は戦力の穴（`choose_signings`）。
    候補は世界の全選手の名前（`used_names` の呼び出し時点の値）と重ならないように引き、獲得した外国人の名前を
    `used_names` に足していく。背番号は球団ごとの乱数（`foreign-number-<球団 id>`、年は翌年）で、獲得した順に引く。
    """
    world_names = frozenset(used_names)
    reference = world_strength([team.squad for team in teams])
    signings: list[ForeignSigning] = []
    after: list[DraftTeam] = []
    for team in sorted(teams, key=lambda item: item.team_id):
        count, _ = newcomer_plan(team)
        wanted = foreign_count_to_add(team, count)
        if wanted <= 0:
            after.append(team)
            continue
        candidates = draw_candidates(
            team.team_id, year=year, phase=SigningPhase.OFFSEASON, world_seed=world_seed, used_names=world_names
        )
        chosen = choose_signings(candidates, wanted, team, reference=reference, world_seed=world_seed)
        if not chosen:
            after.append(team)
            continue
        ensure_signable(
            [candidate.key for candidate in chosen],
            candidates,
            held_foreign=team.foreign_count,
            phase=SigningPhase.OFFSEASON,
        )
        signed = _sign(team, chosen, year=year, world_seed=world_seed, used_names=used_names)
        signings.extend(signed)
        after.append(
            replace(
                team,
                foreign_count=team.foreign_count + len(signed),
                used_numbers=team.used_numbers | {signing.number for signing in signed},
                surname_counts=dict(
                    Counter(team.surname_counts)
                    + Counter(signing.profile.back_name.split(".")[-1] for signing in signed)
                ),
            )
        )
    return OffseasonSignings(signings=tuple(signings), teams=tuple(after))


def _sign(
    team: DraftTeam,
    chosen: Sequence[ForeignCandidate],
    *,
    year: int,
    world_seed: int,
    used_names: MutableSet[str],
) -> list[ForeignSigning]:
    """候補を獲得した外国人にする（名前の重複の解消・背番号・背ネーム）。"""
    names = [
        _resolved_name(candidate, team.team_id, world_seed=world_seed, used_names=used_names) for candidate in chosen
    ]
    rng = make_random(game_seed(world_seed, year + 1, f"foreign-number-{team.team_id}"))
    used_numbers = set(team.used_numbers)
    surnames = Counter(team.surname_counts) + Counter(name.surname_romaji for name in names)
    result = []
    for candidate, name in zip(chosen, names, strict=True):
        back_name = generator.back_name(
            name.surname_romaji, name.given_romaji, shares_surname=surnames[name.surname_romaji] > 1
        )
        result.append(
            ForeignSigning(
                team_id=team.team_id,
                candidate=candidate,
                number=draw_number(rng, used_numbers),
                name=name.name,
                profile=replace(
                    candidate.profile,
                    name_kana=name.kana,
                    birthplace=name.country,
                    nationality=name.country,
                    back_name=back_name,
                ),
                ratings=candidate.ratings,
            )
        )
    return result


def _resolved_name(
    candidate: ForeignCandidate, team_id: int, *, world_seed: int, used_names: MutableSet[str]
) -> generator.ForeignName:
    """獲得した外国人の名前（`used_names` に足す）。他球団が同じ名前の外国人を先に獲得していたら、
    名前（と国・背ネームの材料）だけを専用の流れ（`…-rename-…`）で引き直す。能力・年齢・位置・前所属は動かない。"""
    if candidate.name not in used_names:
        used_names.add(candidate.name)
        return generator.ForeignName(
            candidate.name,
            candidate.profile.name_kana,
            candidate.profile.nationality,
            candidate.surname_romaji,
            candidate.given_romaji,
        )
    key = candidate.key
    return generator.foreign_name(
        make_random(game_seed(world_seed, key.year, key.seed_key(team_id, "rename"))), used_names
    )


def newcomer_route_label(profile: Profile) -> str:
    """オフに加入した選手（新人と獲得した外国人）の経路の表記。**オフの結果の「経路」の出典**。

    ドラフトを経ていない外国人は獲得した外国人で「新外国人（前所属）」（前所属が無ければ「新外国人」）。
    ほかはドラフトの新人で、経歴から導く経路（`DraftRoute.of_profile`。高校・大学・社会人）。
    """
    if profile.is_foreign_player and profile.draft is None:
        label = AcquisitionRoute.NEW_FOREIGN.label
        background = profile.foreign_background
        return label if background is None else f"{label}（{background.label}）"
    return DraftRoute.of_profile(profile).label


def expected_signing_ratings(position: Position) -> Ratings:
    """獲得した外国人に見込める能力（ぶれを除いた平均。錨の前の尺度。平均は `SIGNED_RATING_MEAN`）。

    戦力外が、今の外国人と新しく迎える外国人を比べるのに使う（`release.foreign_prospect_value`）。
    外国人は捕手を獲得しないので、捕手を渡さない。
    """
    if position is Position.CATCHER:
        raise ValueError("外国人の捕手は獲得しません。")
    return expected_prospect_ratings(mean=SIGNED_RATING_MEAN, position=position)


def expected_signing_ages() -> dict[int, float]:
    """候補の年齢の分布（年齢 → 割合）。前所属の割合と、区分ごとの年齢の範囲（一様）から。

    獲得した外国人は能力の高いメジャー経験に寄るぶん候補より年上（実測で平均 28.4歳。候補は 27.9歳）だが、
    見込みの伸び（`outlook.projected_ratings`）は26歳以下にしか付かず小さいので、候補の分布のままにした
    （#308 P3b の前の若さの上乗せで測って、見込みの差は約0.1点）。
    """
    total = sum(item.weight for item in BACKGROUNDS.values())
    result: dict[int, float] = {}
    for item in BACKGROUNDS.values():
        low, high = item.age_range
        for age in range(low, high + 1):
            result[age] = result.get(age, 0.0) + item.weight / total / (high - low + 1)
    return result
