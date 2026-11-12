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
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache

from ..simulation.randomness import game_seed, make_random
from ..simulation.ratings import POTENTIAL_AVERAGE, Potential
from ..value_objects import Position
from .draft import NUMBER_POOLS, DraftRoute, draftee_count, draw_number
from .offseason_player import OffseasonClub, OffseasonPlayer, retirement_value
from .outlook import projected_ratings
from .prospects import expected_prospect_ratings
from .release import ability_value, keep_value, release_order
from .roster import OPENING_REGISTERED_BAND

# 育成選手の契約の年数（NPB の育成選手に関する規約: 契約期間は3年。3年を過ぎて翌年に支配下で契約されなければ
# 11月30日に自由契約。その後の再契約は1シーズンごと）。**満了の年数の出典はここだけ**
DEVELOPMENT_CONTRACT_YEARS = 3
# 1球団が1年のオフに上げる育成の上限（案3）。NPB の育成から支配下への移行は 2023年 30人・2024年 50人
# ＝1球団1年 約2.5〜4.2人（npb.jp の「新規支配下選手登録」の公示。2月〜7月の分だけ）
MAX_PROMOTIONS = 3
# 昇格の基準の余裕（点）。育成の見込みの価値が、入れ替わる支配下の選手の残す価値をこれより上回るときだけ上げる
PROMOTION_MARGIN = 3.0
# 満了の余裕（点）。満了の育成の見込みの価値が、新しい育成の新人の見込みからこれを引いた値に届かなければ自由契約
RESIGN_MARGIN = 5.0
# 新しい育成の新人の見込みに使う年齢と経路（育成の新人はほとんどが大学から入る。#198 D2 の実測）
DEVELOPMENT_PROSPECT_AGE = 22
DEVELOPMENT_PROSPECT_ROUTE = DraftRoute.UNIVERSITY


@dataclass(frozen=True)
class Promotion:
    """オフの昇格ひとり。翌年（締める年 + 1）から支配下で、背番号は `number`（支配下の番号）に変わる。"""

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
    draft = player.profile.draft
    if draft is not None and draft.is_development:
        return draft.entry_year
    return player.joined_year


def is_contract_expiring(start_year: int, year: int) -> bool:
    """起点 `start_year` の育成契約が、`year` 年のオフに満了するか。**満了の判定の出典はここだけ**。

    `DEVELOPMENT_CONTRACT_YEARS` 年目（`year - start_year + 1`）のオフに満了し、再契約した後は毎年のオフに満了する
    （NPB の再契約は1シーズンごと。再契約の記録は保存しないので、3年を過ぎた育成は毎年が満了の年になる）。
    """
    return year - start_year + 1 >= DEVELOPMENT_CONTRACT_YEARS


@cache
def development_prospect_value(position: Position) -> float:
    """新しい育成の新人に見込める価値（`release.ability_value` と同じ物差し）。満了の再契約の基準。

    能力は大学の新人の平均（`DraftRoute.rating_mean_at`。錨の前の尺度）、年齢は大学から入る年齢、伸びしろは普通で見た
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
    if count == 0:
        return ()
    rng = make_random(game_seed(world_seed, year, f"promotion-number-{club.team_id}"))
    used = {player.number for player in remaining}
    return tuple(
        Promotion(player_id=player_id, team_id=club.team_id, number=draw_number(rng, used, NUMBER_POOLS))
        for player_id in wished[:count]
    )


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
