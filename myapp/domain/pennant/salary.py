"""ペナントの選手の年俸の相場と契約更改（#40 K1a）。**純粋な関数**で、DB にも Django にも触れない。
**年俸に関わる数はここ1か所**に置く（表記と契約の型は `contract.py`）。

- 相場（`market_salary`）: 能力の総合値と、その年の出場機会・タイトルから決める。**相場の唯一の出典**。
  決定的で、能力と出場に対して単調（下がらない）。前の年俸は材料にしない（前の年俸からの動きは更改の規則が決める）。
  相場は保存しない
- 減額の下限（`salary_floor`）: 野球協約 第92条の減額制限と最低年俸。**減額の下限の唯一の出典**
- AI の更改（`ai_renewal_offer`）: 前の年俸から相場へ一定の割合だけ寄せ、減額の下限で切る。受け持つ球団もいまは
  同じ規則（GM の提示は #40 K1c）。上げ幅は球団ごとの予算（翌年の年俸の上限 − 新人の取り置き。#40 K1b）に収まるよう
  `payroll_cap.room_factor` で0〜1倍に縮める（`decide_renewals`）
- 補い（`initial_salary`）: 契約の行の無い選手（世界の作成・この機能より前の世界）の年俸。出場の実績を使わず、能力から
  見込んだ出場で相場を作り、プロでの年数が浅いほど最低年俸に寄せる。**補いの唯一の出典**
- 新人（`rookie_salary`）と獲得した外国人（`foreign_salary`）の年俸。新人の取り置き（`rookie_reserve`）
- 人件費（`payroll`）: 年俸の合計。球団の収支（`finance.py`）の人件費はこれ

NPB の水準（実測は Wiki「ペナントモードの仕組み」の「年俸（K1a）」の「実測」）: 選手会の 2026年度の年俸調査で、
支配下公示の日本人選手 713名の平均 5,216万円・中央値 2,000万円、球団ごとの合計 22.9〜49.6億円
（https://jpbpa.net/2026rr/ 。外国人を含まない）。報道の推定の最高は日本人で6億円、外国人で12億円前後。
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from ..simulation.ratings import BatterRatings, PitcherRatings
from ..value_objects import ContractStatus, DraftPick, Position
from .contract import MAN, OKU, Salary
from .draft import Draftee
from .offseason_player import OffseasonPlayer, retirement_value
from .payroll_cap import room_factor

# --- 協約・規約の数（円） ---
# 支配下の最低年俸 420万円（日本プロフェッショナル野球協約 2025 第89条。税別。
# https://jpbpa.net/wp-content/uploads/jpbpa-pdf/ag2025.pdf 。2026年度版の協約は公開されておらず確かめられなかった）
MIN_REGISTERED_SALARY = 420 * MAN
# 育成選手の最低年俸 230万円（日本プロ野球育成選手に関する規約 2014年度版 第4条②。税別。
# https://jpbpa.net/wp-content/uploads/2021/12/tp2014.pdf 。公開されている最新の版。240万円への改定は
# 確かめられなかった）
MIN_DEVELOPMENTAL_SALARY = 230 * MAN
# 減額制限（協約 2025 第92条）: その年度の年俸が1億円を超えるなら40%、1億円以下なら25%までしか減らせない
# （選手の同意があれば超えられるが、ゲームでは同意しない＝常に制限どおり）
REDUCTION_THRESHOLD = OKU
HIGH_REDUCTION_LIMIT = 0.40
LOW_REDUCTION_LIMIT = 0.25
# 新人の初年度の年俸の上限 1,600万円（12球団の申し合わせ。2019年11月から。報道による二次資料で、原文は
# 確かめられなかった）
ROOKIE_SALARY_CAP = 1_600 * MAN

# --- 相場の式（実測は Wiki「ペナントモードの仕組み」の「年俸（K1a）」の「実測」） ---
# 支配下: 最低年俸 ＋ 幅 × exp(傾き × (総合値 − 中心)) × 出場の効き × タイトルの効き
MARKET_SPAN = 10_000 * MAN
MARKET_CENTER = 50.0
MARKET_SLOPE = 0.12
# 出場の効き = 出場なしの割合 ＋ (1 − 出場なしの割合) × 出場の度合い（0〜1）
IDLE_SHARE = 0.25
# 出場の度合いが1になる打席数（野手）とアウト数（投手。140回）
FULL_PLATE_APPEARANCES = 500
FULL_OUTS = 420
# タイトル1つあたりの上乗せ（世界の通算。数えるのは3つまで）
TITLE_BONUS = 0.10
MAX_COUNTED_TITLES = 3
# 相場の上限（支配下）
MAX_MARKET_SALARY = 8 * OKU
# 育成: 最低年俸 ＋ 幅 × exp(傾き × (総合値 − 中心))。上限つき（育成の選手は1軍に出ない）
DEVELOPMENTAL_SPAN = 100 * MAN
DEVELOPMENTAL_MAX = 600 * MAN
# 外国人: 日本人の相場の倍率と、下限（新外国人は数千万円から）
FOREIGN_FACTOR = 1.5
FOREIGN_MIN_SALARY = 3_000 * MAN

# --- 補い（出場の実績を使わない） ---
# 能力から見込む出場の度合い: 総合値がこの下限で0、上限で1（間は一次）
EXPECTED_PLAY_LOW = 42.0
EXPECTED_PLAY_HIGH = 56.0
# プロでの年数がこれに満たない日本人は、最低年俸からの上乗せを年数の割合だけにする（若手はまだ相場に届いていない）
RAMP_SEASONS = 6

# --- AI の更改 ---
# 前の年俸と相場の差のうち、1年で寄せる割合（上げるとき・下げるとき）。下げは減額の下限で切る
RAISE_RATE = 0.5
CUT_RATE = 0.5

# --- 新人（ドラフトの巡ごと。二次資料の相場の目安。上限は `ROOKIE_SALARY_CAP`） ---
ROOKIE_SALARIES: tuple[int, ...] = (1_600 * MAN, 1_300 * MAN, 1_100 * MAN, 900 * MAN, 800 * MAN)
ROOKIE_LATER_SALARY = 700 * MAN
# 育成ドラフトの新人（巡によらない。最低年俸の上）
DEVELOPMENTAL_ROOKIE_SALARY = 300 * MAN


@dataclass(frozen=True)
class SalaryBasis:
    """年俸を決める、選手のその年の事実（出場の実績を除く）。

    - `contract`: その年の契約区分（支配下・育成）
    - `value`: 能力の総合値（`offseason_player.retirement_value`。能力の無い選手は中立の値）
    - `seasons_as_pro`: その年を含めてプロにいた年数（1以上。ドラフトの翌年から、無ければ在籍の開始年から）
    - `is_foreign`: 外国人選手
    """

    contract: ContractStatus
    value: float
    seasons_as_pro: int
    is_foreign: bool

    @property
    def is_development(self) -> bool:
        return self.contract is ContractStatus.DEVELOPMENTAL


@dataclass(frozen=True)
class MarketFacts:
    """相場の材料。`basis` と、その年の1軍の出場機会（打席数・アウト数）、世界の通算のタイトルの数。"""

    basis: SalaryBasis
    plate_appearances: int = 0
    outs: int = 0
    titles: int = 0


@dataclass(frozen=True)
class Renewal:
    """契約更改ひとつ（選手ひとり）。`previous` は締める年の年俸、`salary` は翌年の年俸。"""

    player_id: int
    previous: Salary
    salary: Salary

    @property
    def change(self) -> int:
        return self.salary.yen - self.previous.yen


def minimum_salary(contract: ContractStatus) -> int:
    """契約区分の最低年俸（円）。"""
    return MIN_DEVELOPMENTAL_SALARY if contract is ContractStatus.DEVELOPMENTAL else MIN_REGISTERED_SALARY


def salary_basis(
    *,
    position: Position,
    ratings: BatterRatings | PitcherRatings | None,
    contract: ContractStatus | None,
    is_foreign: bool,
    entered_year: int,
    year: int,
) -> SalaryBasis:
    """選手のその年の事実から `SalaryBasis` を作る。**締める処理と、行の無い選手を補う読み口が同じ作り方をする**
    （同じ選手・同じ年なら、締める前に画面で補った年俸と、締めるときに書く補った年俸が一致する）。

    在籍の無い選手（`contract` が None）は支配下として扱う。
    """
    return SalaryBasis(
        contract=ContractStatus.REGISTERED if contract is None else contract,
        value=retirement_value(position, ratings),
        seasons_as_pro=max(1, year - entered_year + 1),
        is_foreign=is_foreign,
    )


def entered_year_of(*, draft_year: int | None, joined_year: int) -> int:
    """プロ入りの年。ドラフトがあればその翌年（`DraftPick.entry_year`）、無ければ在籍の開始年
    （`OffseasonPlayer.entered_year` と同じ規則。読み口が参照クエリの年から補いの材料を作るときに使う）。"""
    return joined_year if draft_year is None else DraftPick(year=draft_year).entry_year


def basis_of(player: OffseasonPlayer, year: int) -> SalaryBasis:
    """締める年 `year` の選手の `SalaryBasis`（`salary_basis` と同じ作り方）。"""
    return salary_basis(
        position=player.position,
        ratings=None if player.ratings is None else player.ratings.ratings,
        contract=player.contract,
        is_foreign=player.profile.is_foreign_player,
        entered_year=player.entered_year,
        year=year,
    )


def market_facts(player: OffseasonPlayer, year: int) -> MarketFacts:
    """締める年 `year` の選手の相場の材料（締めた後の区分。昇格した選手は支配下で渡ってくる）。"""
    return MarketFacts(
        basis=basis_of(player, year),
        plate_appearances=player.playing_time.plate_appearances,
        outs=player.playing_time.outs,
        titles=player.track_record.titles,
    )


def _playing_share(plate_appearances: int, outs: int) -> float:
    """出場の度合い（0〜1）。野手は打席数、投手はアウト数の大きい方。"""
    return min(1.0, max(plate_appearances / FULL_PLATE_APPEARANCES, outs / FULL_OUTS))


def _market_yen(basis: SalaryBasis, *, playing: float, titles: int) -> float:
    """相場（円。丸める前）。出場の度合い `playing`（0〜1）とタイトルの数 `titles`。"""
    strength = math.exp(MARKET_SLOPE * (basis.value - MARKET_CENTER))
    if basis.is_development:
        return min(DEVELOPMENTAL_MAX, MIN_DEVELOPMENTAL_SALARY + DEVELOPMENTAL_SPAN * strength)
    title_factor = 1.0 + TITLE_BONUS * min(max(titles, 0), MAX_COUNTED_TITLES)
    yen = min(
        MAX_MARKET_SALARY,
        MIN_REGISTERED_SALARY + MARKET_SPAN * strength * (IDLE_SHARE + (1.0 - IDLE_SHARE) * playing) * title_factor,
    )
    if basis.is_foreign:
        yen = max(FOREIGN_MIN_SALARY, min(MAX_MARKET_SALARY, yen * FOREIGN_FACTOR))
    return yen


def round_salary(yen: float) -> Salary:
    """円の額を年俸に丸める（1億円未満は10万円、1億円以上は100万円の単位で四捨五入。最小は1万円）。"""
    step = 100 * MAN if yen >= OKU else 10 * MAN
    return Salary(max(MAN, round(yen / step) * step))


def market_salary(facts: MarketFacts) -> Salary:
    """相場（年額）。**相場の唯一の出典**。決定的で、総合値・出場機会・タイトルの数に対して下がらない。

    前の年俸は材料にしない。最低年俸を下回らない。
    """
    basis = facts.basis
    yen = _market_yen(basis, playing=_playing_share(facts.plate_appearances, facts.outs), titles=facts.titles)
    return round_salary(max(yen, minimum_salary(basis.contract)))


def initial_salary(basis: SalaryBasis) -> Salary:
    """契約の行の無い選手の年俸（世界の作成・この機能より前の世界）。**補いの唯一の出典**。

    出場の実績を使わず、総合値から見込んだ出場で相場を作る（同じ年なら、出場が進んでも値が変わらない）。
    日本人の支配下は、プロでの年数が `RAMP_SEASONS` に満たなければ最低年俸からの上乗せを年数の割合だけにする。
    """
    expected = min(1.0, max(0.0, (basis.value - EXPECTED_PLAY_LOW) / (EXPECTED_PLAY_HIGH - EXPECTED_PLAY_LOW)))
    yen = _market_yen(basis, playing=expected, titles=0)
    minimum = minimum_salary(basis.contract)
    if not basis.is_foreign and not basis.is_development:
        yen = minimum + (yen - minimum) * min(1.0, basis.seasons_as_pro / RAMP_SEASONS)
    return round_salary(max(yen, minimum))


def salary_floor(previous: Salary, *, contract: ContractStatus) -> Salary:
    """翌年の年俸の下限（野球協約 第92条の減額制限と、翌年の区分の最低年俸の大きい方）。**減額の下限の唯一の出典**。

    減額制限の基準は前の年俸（1億円を超えれば40%、1億円ちょうど以下は25%まで減らせる）。万円未満は切り上げる。
    """
    limit = HIGH_REDUCTION_LIMIT if previous.yen > REDUCTION_THRESHOLD else LOW_REDUCTION_LIMIT
    floor = math.ceil(previous.yen * (1.0 - limit) / MAN) * MAN
    return Salary(max(floor, minimum_salary(contract)))


def ai_renewal_offer(
    previous: Salary, market: Salary, *, contract: ContractStatus, room_factor: float = 1.0
) -> Salary:
    """AI の更改の額。前の年俸から相場へ、上げるなら差の `RAISE_RATE` × `room_factor`、下げるなら差の `CUT_RATE` だけ
    寄せ、減額の下限（`salary_floor`）で切る。`contract` は翌年の区分。

    `room_factor` は総額の上限の余裕で上げ幅を変える口（#40 K1b。K1a は 1.0）。0 以上。
    """
    if room_factor < 0:
        raise ValueError("余裕の倍率は0以上にしてください。")
    gap = market.yen - previous.yen
    target = previous.yen + (gap * RAISE_RATE * room_factor if gap > 0 else gap * CUT_RATE)
    return max(round_salary(target), salary_floor(previous, contract=contract))


def decide_renewals(players: Sequence[OffseasonPlayer], *, year: int, budget: int | None) -> tuple[Renewal, ...]:
    """締める年 `year` に残る、ひとつの球団の選手の契約更改（翌年の年俸）。選手 id の順で、並びによらない。

    `players` は引退・戦力外・育成契約の満了の後に残る選手（昇格した選手は支配下で渡ってくる。`plan_departures` の
    `survivors`）。年俸（`OffseasonPlayer.salary`）の無い選手は更改しない（読み口が必ず補って渡す）。

    `budget` は更改に使える額（翌年の年俸の上限 − 新人の取り置き。#40 K1b）。上げ幅を `payroll_cap.room_factor` で
    縮め、合計がおおむね予算に収まるようにする（下げは減額制限のとおりで、予算を超えても更改は通す）。None は上限を
    見ない（倍率1。K1a と同じ）。
    """
    paid = [player for player in sorted(players, key=lambda item: item.player_id) if player.salary is not None]
    facts = {player.player_id: market_facts(player, year) for player in paid}
    markets = {player_id: market_salary(item) for player_id, item in facts.items()}

    def offers(factor: float) -> list[Renewal]:
        result = []
        for player in paid:
            assert player.salary is not None
            offer = ai_renewal_offer(
                player.salary,
                markets[player.player_id],
                contract=facts[player.player_id].basis.contract,
                room_factor=factor,
            )
            result.append(Renewal(player_id=player.player_id, previous=player.salary, salary=offer))
        return result

    full = offers(1.0)
    if budget is None:
        return tuple(full)
    factor = room_factor(
        budget=budget,
        at_zero=payroll(renewal.salary for renewal in offers(0.0)),
        at_full=payroll(renewal.salary for renewal in full),
    )
    return tuple(full if factor == 1.0 else offers(factor))


def rookie_reserve(slots: int) -> int:
    """新人 `slots` 人（本指名の1巡から）の初年度の年俸の和（円）。**新人の取り置きの唯一の出典**（#40 K1b）。

    年俸の上限の検査は会議の前に行うので、会議で指名する新人のぶんを先に取り置く。育成ドラフトの新人は数えない
    （人数が会議まで決まらず、1人300万円と小さい）。
    """
    return sum(rookie_round_salary(number).yen for number in range(1, max(0, slots) + 1))


def rookie_round_salary(round_number: int | None) -> Salary:
    """本指名の巡 `round_number`（1から。分からなければ None）の新人の初年度の年俸（上限 `ROOKIE_SALARY_CAP`）。
    **巡ごとの新人の年俸の唯一の出典**（`rookie_salary` と `rookie_reserve` が通る）。"""
    if round_number is None or round_number > len(ROOKIE_SALARIES):
        return Salary(ROOKIE_LATER_SALARY)
    return Salary(min(ROOKIE_SALARY_CAP, ROOKIE_SALARIES[round_number - 1]))


def rookie_salary(draftee: Draftee) -> Salary:
    """ドラフトの新人の初年度の年俸。本指名は巡ごと（上限 `ROOKIE_SALARY_CAP`）、育成ドラフトは巡によらない。"""
    if draftee.is_development:
        return Salary(DEVELOPMENTAL_ROOKIE_SALARY)
    pick = draftee.profile.draft
    return rookie_round_salary(None if pick is None else pick.round)


def foreign_salary(position: Position, ratings: BatterRatings | PitcherRatings, *, year: int) -> Salary:
    """獲得した外国人（オフ・期中）のその年の年俸。補い（`initial_salary`）と同じ式で、外国人・支配下として作る。

    期中に獲得した外国人も年額で数える（按分しない。#40 K1a の簡略化）。
    """
    return initial_salary(
        salary_basis(
            position=position,
            ratings=ratings,
            contract=ContractStatus.REGISTERED,
            is_foreign=True,
            entered_year=year,
            year=year,
        )
    )


def payroll(salaries: Iterable[Salary]) -> int:
    """人件費（年俸の合計。円）。**球団の人件費の唯一の出典**（`finance.FinanceInputs.payroll` に渡す）。"""
    return sum(salary.yen for salary in salaries)
