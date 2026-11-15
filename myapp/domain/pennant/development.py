"""育成選手のオフの扱い: 支配下への昇格（AI）と、育成契約の満了（再契約か自由契約か）。#170 P1。

**純粋な関数**で、DB にも Django にも触れない。締める処理（`offseason.plan_departures`）の中で、引退の後に使う:

    引退 → **昇格の希望**（`decide_promotions`）→ 戦力外（昇格の希望の分も追加で空ける＝上乗せ）→
    **昇格の確定**（`confirm_promotions`。空きが足りなければ昇格を削り、新人は削らない）→
    **満了**（`decide_expirations`）→ 外国人の獲得 → ドラフト会議（育成の巡が満了で空いた分を補う）→ 成長 → 錨

規則（2026-10-24・11-12 ユーザー決定。Issue #170）:

- **昇格は新人の枠に上乗せ**（Q2）。戦力外は「ふつうの新人＋昇格の希望の人数」を迎える空きまで外す
  （`release.release_counts(expected=)`）。新人の人数はふだんどおり（`draft.USUAL_DRAFTEES`）。
- **昇格の基準は入れ替わる選手と比べる**（Q1 A）。k 人目の昇格の候補（育成の中で見込みの価値の高い順）は、
  その見込みの価値（`release.ability_value`＝所属球団の見立て）が、上乗せで k 人目に追加で外れる支配下の選手の
  残す価値（`release.keep_value`）＋ `PROMOTION_MARGIN` を上回るときだけ上げる。1球団1年 `MAX_PROMOTIONS` 人まで。
- **満了**: 育成の契約は `DEVELOPMENT_CONTRACT_YEARS` 年（NPB の育成選手に関する規約。3年を過ぎて翌年に支配下で
  契約されなければ自由契約。再契約は1シーズンごと）。満了の年に昇格しなかった育成は、見込みの価値が新しい育成の
  新人の見込み（`development_prospect_value`）− `RESIGN_MARGIN` に届かなければ自由契約（退団の経路は戦力外＝
  `RELEASED`。Q3）、届けば再契約（何も書かない。再契約の記録は保存しない）。

昇格の事実は在籍の `promoted_year`・`number_before_promotion` だけ、育成契約の起点・満了・再契約は在籍とドラフトの
指名から導く（保存しない）。

シーズン中は、GM が育成タブで上げ（#170 P3a）、AI は判定日（6月1日・7月1日）に今の力で上げる
（`decide_midseason_promotions`。#170 P3b・Q2 A）。どちらも補強の窓（`reinforcement.ReinforcementWindow`）の中だけ。

オフは、GM が受け持つ球団の育成選手を行ごとに1つの扱い（`DevelopmentDecision`）で決められる（`DevelopmentPlan`。
#170 P2。検査は `ensure_development_plan`）。手動の球団は指名どおりに上げ・自由契約にし（`decisions_as_chosen`・
`confirm_chosen_promotions`）、上の規則（自動）を使わない。AI 球団は常に自動。
"""

from __future__ import annotations

import datetime
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from functools import cache

from ..exceptions import InvalidDevelopmentPlan
from ..simulation.manager import ClubRoster, choose_active_roster
from ..simulation.randomness import game_seed, make_random
from ..simulation.ratings import POTENTIAL_AVERAGE, Potential
from ..value_objects import DraftPick, ForeignPlayerRules, Position
from .draft import NUMBER_POOLS, DraftRoute, draftee_count, draw_number
from .offseason_player import OffseasonClub, OffseasonPlayer, Ratings, retirement_value
from .outlook import projected_ratings
from .prospects import expected_prospect_ratings
from .reinforcement import MidseasonRegistration, RegistrationKind, ReinforcementWindow
from .release import ability_value, keep_value, release_order
from .roster import OPENING_REGISTERED_BAND

# 育成選手の契約の年数（NPB の育成選手に関する規約: 契約期間は3年。3年を過ぎて翌年に支配下で契約されなければ
# 11月30日に自由契約。その後の再契約は1シーズンごと）。**満了の年数の出典はここだけ**
DEVELOPMENT_CONTRACT_YEARS = 3
# 1球団が1年のオフに上げる育成の上限（案3）。NPB の育成から支配下への移行は 2023年 30人・2024年 50人
# ＝1球団1年 約2.5〜4.2人（npb.jp の「新規支配下選手登録」の公示。2月〜7月の分だけ）
MAX_PROMOTIONS = 3
# 昇格の基準の余裕（点）。育成の見込みの価値が、入れ替わる支配下の選手の残す価値をこれより上回るときだけ上げる。
# #362（育成の新人に高校生を増やした）で 3 → 6: 高校で入った育成は伸びの見込みが大きく、3点では昇格が増えて
# 入れ替わりが上がった（2026-11-13 ユーザー決定 案1。昇格の人数は前と同じくらいに保ち、上がる選手が若くなる）
PROMOTION_MARGIN = 6.0
# 満了の余裕（点）。満了の育成の見込みの価値が、新しい育成の新人の見込みからこれを引いた値に届かなければ自由契約
RESIGN_MARGIN = 5.0
# AI の期中の昇格（#170 P3b。Q2 A）: 1球団1年の上限（判定日ごとに1人まで）と、支配下の上限のうち空けておく人数
# （#169 F3 の期中の外国人の獲得の分）
MIDSEASON_MAX_PROMOTIONS = 2
MIDSEASON_RESERVED_SLOTS = 1
# 新しい育成の新人の見込みに使う年齢と経路。#362 で育成の新人の約半分が高校から入るようになり、大学（22歳）のままでは
# 高校で入った若い育成の見込みがいつも上回って満了の自由契約がほとんど出なくなったので、高校（18歳）にした
# （前は大学22歳。育成の新人がほとんど大学だった #198 D2 の実測から）
DEVELOPMENT_PROSPECT_AGE = 18
DEVELOPMENT_PROSPECT_ROUTE = DraftRoute.HIGH_SCHOOL


@dataclass(frozen=True)
class Promotion:
    """昇格ひとり。背番号は `number`（支配下の番号）に変わる。

    オフの昇格（`confirm_promotions`）は翌年（締める年 + 1）から、AI の期中の昇格（`decide_midseason_promotions`）は
    登録日から支配下。"""

    player_id: int
    team_id: int
    number: str


def development_start_year(player: OffseasonPlayer) -> int:
    """育成契約の起点の年（1年目のシーズン）。**起点の出典はここだけ**。

    育成ドラフトで入った選手は入団の年（`DraftPick.entry_year`）、それ以外（自由契約から育成で再び入った選手など）は
    今の在籍の開始年。分岐した選手は在籍の開始年が一律に開幕年になるので、ドラフトの年を優先する
    （開幕3年目に一斉に満了させないため）。育成ドラフトの指名の無い分岐した育成（外国人の育成など）は、開幕3年目の
    オフにそろって満了の判定を受ける（実データの日本人の育成は投入で指名を持つので当たらない）。
    """
    return contract_start_year(player.profile.draft, joined_year=player.joined_year)


def contract_start_year(draft: DraftPick | None, *, joined_year: int) -> int:
    """`development_start_year` の材料版（ドラフトの指名と今の在籍の開始年から）。

    編成の「育成」タブの「何年目」も使う。"""
    if draft is not None and draft.is_development:
        return draft.entry_year
    return joined_year


def contract_season(start_year: int, year: int) -> int:
    """`year` 年が育成契約の何年目か（起点の年が1年目）。編成の「育成」タブに出す。"""
    return year - start_year + 1


def is_contract_expiring(start_year: int, year: int) -> bool:
    """起点 `start_year` の育成契約が、`year` 年のオフに満了するか。**満了の判定の出典はここだけ**。

    `DEVELOPMENT_CONTRACT_YEARS` 年目（`year - start_year + 1`）のオフに満了し、再契約した後は毎年のオフに満了する
    （NPB の再契約は1シーズンごと。再契約の記録は保存しないので、3年を過ぎた育成は毎年が満了の年になる）。
    """
    return contract_season(start_year, year) >= DEVELOPMENT_CONTRACT_YEARS


@cache
def development_prospect_value(position: Position) -> float:
    """新しい育成の新人に見込める価値（`release.ability_value` と同じ物差し）。満了の再契約の基準。

    能力は `DEVELOPMENT_PROSPECT_ROUTE`（高校）の新人の平均（`DraftRoute.rating_mean_at`。錨の前の尺度）、年齢は
    `DEVELOPMENT_PROSPECT_AGE`（高校から入る年齢）、伸びしろは普通で見た
    見込みの能力（`outlook.projected_ratings`）の総合値（`retirement_value`）。材料は定数だけなので位置ごとに覚える。
    """
    mean = DEVELOPMENT_PROSPECT_ROUTE.rating_mean_at(DEVELOPMENT_PROSPECT_AGE)
    ratings = expected_prospect_ratings(mean=mean, position=position)
    projected = projected_ratings(
        ratings, age=DEVELOPMENT_PROSPECT_AGE, position=position, potential_estimate=Potential(POTENTIAL_AVERAGE)
    )
    return retirement_value(position, projected)


def decide_promotions(
    club: OffseasonClub, remaining: Sequence[OffseasonPlayer], *, year: int, start_year: int, world_seed: int
) -> tuple[int, ...]:
    """昇格の希望（育成の選手の id。上げたい順）。`remaining` は引退しなかった選手。

    育成を見込みの価値（`ability_value`）の高い順に、k 人目を「上乗せで k 人目に追加で外れる支配下の選手」
    （戦力外の順 `release.release_order` の、ふつうの戦力外の人数の後ろ k 人目）の残す価値＋ `PROMOTION_MARGIN`
    と比べ、上回る間だけ取る（`MAX_PROMOTIONS` 人まで）。追加で外せる選手がいなければ（位置の最低人数）そこで止める。
    人数が足りるか（新人を削らないか）は戦力外の後に `confirm_promotions` が決める。
    天井まで空きのある球団・入れ替える外国人で空きができる球団では、昇格しても誰も追加で外れないが、それでも同じ並びの
    選手（いちばん外れやすい控え）と比べる（空きがあるだけで控えより弱い育成を上げない。安全側）。
    """

    def value(player: OffseasonPlayer) -> float:
        return ability_value(player, team_id=club.team_id, year=year, start_year=start_year, world_seed=world_seed)

    candidates = sorted(
        (player for player in remaining if player.is_development),
        key=lambda player: (-value(player), player.player_id),
    )
    if not candidates:
        return ()
    order, usual = release_order(club, remaining, year=year, start_year=start_year, world_seed=world_seed)
    wished: list[int] = []
    for candidate, pushed in zip(candidates[:MAX_PROMOTIONS], order[usual:], strict=False):
        bar = keep_value(pushed, team_id=club.team_id, year=year, start_year=start_year, world_seed=world_seed)
        if value(candidate) <= bar + PROMOTION_MARGIN:
            break
        wished.append(candidate.player_id)
    return tuple(wished)


def confirm_promotions(
    club: OffseasonClub, remaining: Sequence[OffseasonPlayer], wished: Sequence[int], *, year: int, world_seed: int
) -> tuple[Promotion, ...]:
    """戦力外の後の球団で、昇格を確定する（希望の前から）。`remaining` は引退・戦力外の後の選手。

    新人の人数（`draft.draftee_count`）を昇格の前と同じに保てる人数まで（**昇格を削り、新人は削らない**。
    2026-11-12 の指示）。帯の下端に新人だけで届かない球団（戦力外の後に 55人以下）は、1人上げると新人が減るので
    0人になる（新人が `MAX_DRAFTEES` で頭打ちの球団だけは上げられる）。
    背番号は空いている支配下の番号から引く（`draft.draw_number`。新人・獲得した外国人と同じ。乱数は
    `promotion-number-<球団 id>`）。
    """
    registered = sum(1 for player in remaining if player.is_registered)
    limit = club.registered_player_limit
    rookies = draftee_count(registered, registered_limit=limit)
    ceiling = OPENING_REGISTERED_BAND.ceiling(limit)
    count = len(wished)
    while count > 0 and (
        registered + count + rookies > ceiling or draftee_count(registered + count, registered_limit=limit) < rookies
    ):
        count -= 1
    return _numbered(club, remaining, wished[:count], year=year, world_seed=world_seed)


def _numbered(
    club: OffseasonClub, remaining: Sequence[OffseasonPlayer], player_ids: Sequence[int], *, year: int, world_seed: int
) -> tuple[Promotion, ...]:
    """オフの昇格に背番号を付ける（空いている支配下の番号から順に引く。乱数は `promotion-number-<球団 id>`）。

    AI の昇格（`confirm_promotions`）と GM の指名（`confirm_chosen_promotions`）が共有する。"""
    if not player_ids:
        return ()
    rng = make_random(game_seed(world_seed, year, f"promotion-number-{club.team_id}"))
    used = {player.number for player in remaining}
    return tuple(
        Promotion(player_id=player_id, team_id=club.team_id, number=draw_number(rng, used, NUMBER_POOLS))
        for player_id in player_ids
    )


def midseason_promotion_number(used: set[str], *, world_seed: int, year: int, team_id: int, player_id: int) -> str:
    """期中の昇格（#170 P3a）の背番号。空いている支配下の番号から引く（オフの昇格と同じ `draft.draw_number`）。

    GM は背番号を選ばない（自動）。乱数は世界のシード・登録の年・球団・選手から作るので、同じ状態なら同じ番号になる。
    `used` はいま球団で使われている番号（引いた番号を足す）。
    """
    rng = make_random(game_seed(world_seed, year, f"midseason-promotion-number-{team_id}-{player_id}"))
    return draw_number(rng, used, NUMBER_POOLS)


@dataclass(frozen=True)
class MidseasonProspect:
    """期中の昇格の候補の育成選手ひとり（登録位置と、判定日の年の能力。能力が無い選手は上げない）。"""

    player_id: int
    position: Position
    ratings: Ratings | None


@dataclass(frozen=True)
class MidseasonClub:
    """AI の期中の昇格（`decide_midseason_promotions`）の材料。判定日の時点の球団ひとつ。"""

    team_id: int
    # 受け持つ球団（GM が決める。AI は触れない）
    is_managed: bool
    # 支配下の登録候補（能力のある在籍選手。試合の進行の `pool_of` と同じもの）と、所属リーグの1軍の外国人枠
    pool: ClubRoster
    foreign_rules: ForeignPlayerRules
    development: tuple[MidseasonProspect, ...]
    # その年の支配下の人数とリーグの上限（None は無制限）
    registered_count: int
    registered_limit: int | None
    # 球団でいま使われている背番号（支配下・育成とも）
    used_numbers: frozenset[str]
    # 期中の登録（GM・AI とも。昇格だけを数える＝外国人の獲得（#169 F3）の登録は昇格の上限に数えない）
    registrations: tuple[MidseasonRegistration, ...]


def decide_midseason_promotions(
    clubs: Sequence[MidseasonClub], *, on: datetime.date, world_seed: int
) -> tuple[Promotion, ...]:
    """判定日に AI が支配下に上げる育成選手（#170 P3b。2026-11-12 ユーザー決定 Q2 A）。`on` は登録日（判定日以降の
    最初の公式戦の日。上げた選手はこの日の試合から出られる）。

    - **受け持つ球団は対象にしない**（GM が育成タブで決める）。補強の窓（`ReinforcementWindow.is_open`）の外なら
      誰も上げない
    - 上げるのは、今の能力の総合値（`retirement_value`）がその球団の1軍の同じ登録位置の基準（自動の1軍
      `choose_active_roster` に入る、その位置で最も低い選手の総合値）以上の育成。1軍にその位置の選手がいなければ
      上げない。
      候補が複数なら基準を上回る幅の大きい選手（同じなら id の小さい選手）
    - 1判定日1球団1人・1年 `MIDSEASON_MAX_PROMOTIONS` 人まで（その年の期中の**昇格の**登録で数える。外国人の獲得の
      登録は数えない）。支配下の上限のうち `MIDSEASON_RESERVED_SLOTS` 人は空けておく（#169 F3 の期中の外国人の
      獲得の分）
    - その判定日の昇格の登録（登録日が `on` 以降）がもうある球団は上げない（書いた後に試合を作る前で止まった
      「進める」をやり直しても、2人目を上げない）

    背番号は `midseason_promotion_number`（GM の期中の昇格と同じ）。判定日の材料だけで決まる。
    """
    if not ReinforcementWindow.is_open(next_regular_game_on=on):
        return ()
    promotions: list[Promotion] = []
    for club in clubs:
        if club.is_managed:
            continue
        this_year = [
            registration.registered_on
            for registration in club.registrations
            if registration.kind is RegistrationKind.PROMOTION and registration.registered_on.year == on.year
        ]
        if any(registered >= on for registered in this_year) or len(this_year) >= MIDSEASON_MAX_PROMOTIONS:
            continue
        if (
            club.registered_limit is not None
            and club.registered_count + 1 > club.registered_limit - MIDSEASON_RESERVED_SLOTS
        ):
            continue
        chosen = _midseason_candidate(club)
        if chosen is None:
            continue
        number = midseason_promotion_number(
            set(club.used_numbers), world_seed=world_seed, year=on.year, team_id=club.team_id, player_id=chosen
        )
        promotions.append(Promotion(player_id=chosen, team_id=club.team_id, number=number))
    return tuple(promotions)


def _midseason_candidate(club: MidseasonClub) -> int | None:
    """1軍の同じ登録位置の基準以上の育成のうち、基準を上回る幅の最も大きい選手の id（いなければ None）。"""
    active = choose_active_roster(club.pool, rules=club.foreign_rules)
    bars: dict[Position, float] = {}
    members: list[tuple[Position, Ratings]] = [(batter.position, batter.ratings) for batter in active.batters]
    members.extend((Position.PITCHER, pitcher.ratings) for pitcher in active.pitchers)
    for position, ratings in members:
        value = retirement_value(position, ratings)
        bars[position] = min(value, bars.get(position, value))
    best: tuple[float, int] | None = None
    for prospect in club.development:
        bar = bars.get(prospect.position)
        if prospect.ratings is None or bar is None:
            continue
        margin = retirement_value(prospect.position, prospect.ratings) - bar
        if margin < 0:
            continue
        key = (-margin, prospect.player_id)
        if best is None or key < best:
            best = key
    return None if best is None else best[1]


def decide_expirations(
    club: OffseasonClub,
    remaining: Sequence[OffseasonPlayer],
    *,
    promoted: Sequence[int],
    year: int,
    start_year: int,
    world_seed: int,
) -> tuple[int, ...]:
    """育成契約の満了で自由契約にする選手の id（id の昇順）。`remaining` は引退・戦力外の後の選手。

    満了の年（`is_contract_expiring`）の育成で、昇格しなかった選手のうち、見込みの価値（`ability_value`）が
    新しい育成の新人の見込み（`development_prospect_value`）− `RESIGN_MARGIN` に届かない選手。それ以外は再契約。
    """
    skipped = set(promoted)
    return tuple(
        sorted(
            player.player_id
            for player in remaining
            if player.is_development
            and player.player_id not in skipped
            and is_contract_expiring(development_start_year(player), year)
            and ability_value(player, team_id=club.team_id, year=year, start_year=start_year, world_seed=world_seed)
            < development_prospect_value(player.position) - RESIGN_MARGIN
        )
    )


# --- GM の指名（受け持つ球団だけ。#170 P2） ---


class DevelopmentDecision(Enum):
    """育成選手ひとりのオフの扱い（GM が行ごとに1つ選ぶ。2026-10-24 ユーザー決定 Q4 A）。
    **選択肢（永続化の choices）の出典はこの Enum だけ**。

    満了の年（`is_contract_expiring`）の選手は「支配下に登録」「再契約」「自由契約」、それ以外は「育成のまま」
    「支配下に登録」から選ぶ（`choices_for`）。満了の自由契約の退団の経路は戦力外（`RELEASED`。Q3）。
    """

    KEEP = "育成のまま"
    PROMOTE = "支配下に登録"
    RESIGN = "再契約"
    RELEASE = "自由契約"

    @property
    def label(self) -> str:
        return self.value

    @classmethod
    def choices_for(cls, *, is_expiring: bool) -> tuple[DevelopmentDecision, ...]:
        """その選手に選べる扱い（画面の並び順）。"""
        if is_expiring:
            return (cls.PROMOTE, cls.RESIGN, cls.RELEASE)
        return (cls.KEEP, cls.PROMOTE)

    @classmethod
    def unchanged(cls, *, is_expiring: bool) -> DevelopmentDecision:
        """何も変えない扱い（満了なら再契約、それ以外は育成のまま）。手動にしたときの初期値。"""
        return cls.RESIGN if is_expiring else cls.KEEP

    @classmethod
    def from_label(cls, label: str) -> DevelopmentDecision:
        for decision in cls:
            if decision.value == label:
                return decision
        raise InvalidDevelopmentPlan(f"「{label}」は育成選手の扱いとして認識できません。")


@dataclass(frozen=True)
class DevelopmentPlan:
    """GM の育成選手のオフの指名。球団ごとに1つで、締める年 `year` のオフに使う。

    `decisions`（選手の id → 扱い）が None なら自動（AI と同じ規則）。戦力外の指名（`release.ReleasePlan`）と同じく
    GM の上書き（入力）だけを持ち、締めた時点で消す（昇格の事実は在籍の `promoted_year`、自由契約は退団の経路に残る）。
    コンストラクタは保存した値をそのまま写すので検査しない（検査は決めるときの `ensure_development_plan`）。
    """

    team_id: int
    year: int
    # 比べるのは値（`==`）だけ。辞書は hash できないので、hash の材料から外す
    decisions: Mapping[int, DevelopmentDecision] | None = field(default=None, hash=False)

    @property
    def is_manual(self) -> bool:
        return self.decisions is not None

    def count(self, decision: DevelopmentDecision) -> int:
        """その扱いを選んだ人数（自動なら 0）。"""
        return sum(1 for chosen in (self.decisions or {}).values() if chosen is decision)


@dataclass(frozen=True)
class DevelopmentMember:
    """指名の検査に使う、球団のいま在籍している育成選手ひとり（`is_expiring` は締める年に満了するか）。"""

    player_id: int
    name: str
    is_expiring: bool


def ensure_development_plan(
    decisions: Mapping[int, DevelopmentDecision], members: Sequence[DevelopmentMember]
) -> None:
    """育成選手のオフの指名として成立するか。成立しなければ `InvalidDevelopmentPlan`。

    - 指名できるのは球団のいま在籍している育成選手だけで、**全員に1つずつ選ぶ**（選んでいない選手を「何もしない」と
      見なさない。画面のキーの欠落と同じく、黙って埋めない）
    - 扱いはその選手に選べるもの（`DevelopmentDecision.choices_for`。再契約・自由契約は満了の選手だけ、
      育成のままは満了でない選手だけ）

    支配下に上げる人数は縛らない（AI が上げる人数の上限 `MAX_PROMOTIONS` は自動の規則で、GM の判断は縛らない）。
    支配下の上限を超える分は締めるときに上げない（`confirm_chosen_promotions`）。
    """
    by_id = {member.player_id: member for member in members}
    unknown = sorted(player_id for player_id in decisions if player_id not in by_id)
    if unknown:
        raise InvalidDevelopmentPlan(
            f"育成の指名にできるのは、この球団に在籍している育成選手だけです（id={unknown[0]}）。"
        )
    for member in members:
        decision = decisions.get(member.player_id)
        if decision is None:
            raise InvalidDevelopmentPlan(f"{member.name}の扱いを選んでください。")
        if decision not in DevelopmentDecision.choices_for(is_expiring=member.is_expiring):
            reason = "今季で育成契約が満了する" if member.is_expiring else "今季で育成契約が満了しない"
            raise InvalidDevelopmentPlan(f"{member.name}は{reason}ので、「{decision.label}」は選べません。")


@dataclass(frozen=True)
class ChosenDevelopment:
    """手動の指名から、締めるときに使う昇格の希望と満了の自由契約（どちらも id の昇順）。"""

    promotions: tuple[int, ...]
    releases: tuple[int, ...]


def decisions_as_chosen(
    decisions: Mapping[int, DevelopmentDecision], remaining: Sequence[OffseasonPlayer], *, year: int
) -> ChosenDevelopment:
    """手動の指名から、締めるときに上げる希望と自由契約にする選手。`remaining` は引退しなかった選手。

    指名した後に引退した選手（引退として記録する）・育成でなくなった選手は黙って除く（`release.releases_as_chosen`
    と同じ）。自由契約は、締める年に満了する選手（`is_contract_expiring`）だけ。
    """
    development = [player for player in remaining if player.is_development]
    return ChosenDevelopment(
        promotions=tuple(
            sorted(
                player.player_id
                for player in development
                if decisions.get(player.player_id) is DevelopmentDecision.PROMOTE
            )
        ),
        releases=tuple(
            sorted(
                player.player_id
                for player in development
                if decisions.get(player.player_id) is DevelopmentDecision.RELEASE
                and is_contract_expiring(development_start_year(player), year)
            )
        ),
    )


def confirm_chosen_promotions(
    club: OffseasonClub,
    remaining: Sequence[OffseasonPlayer],
    chosen: Sequence[int],
    *,
    year: int,
    start_year: int,
    world_seed: int,
) -> tuple[Promotion, ...]:
    """GM が指名した昇格を、戦力外の後の球団で確定する。`remaining` は引退・戦力外の後の選手。

    AI の `confirm_promotions` と違い、新人の人数を守るために削らない（GM の判断を優先し、新人は昇格の後の空きに
    合わせて `draft.draftee_count` が減らす）。削るのは**支配下の上限**を超える分だけで、見込みの価値
    （`ability_value`。AI の昇格と同じ物差し）の低い選手から削る。背番号は AI の昇格と同じく、空いている支配下の
    番号から。
    """
    candidates = {player.player_id: player for player in remaining if player.is_development}
    ranked = sorted(
        (candidates[player_id] for player_id in chosen if player_id in candidates),
        key=lambda player: (
            -ability_value(player, team_id=club.team_id, year=year, start_year=start_year, world_seed=world_seed),
            player.player_id,
        ),
    )
    count = len(ranked)
    if club.registered_player_limit is not None:
        registered = sum(1 for player in remaining if player.is_registered)
        count = min(count, max(0, club.registered_player_limit - registered))
    return _numbered(
        club, remaining, [player.player_id for player in ranked[:count]], year=year, world_seed=world_seed
    )
