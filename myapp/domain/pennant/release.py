"""AI 球団の戦力外。締める処理の中で、引退の後・ドラフトの前に、球団ごとに外す選手を決める。
**純粋な関数**で、乱数も使わない（同じ材料なら必ず同じ選手を外す）。DB にも Django にも触れない。

    引退 → **戦力外** → ドラフト → 成長と衰え → 水準の錨

規則は2つ（2026-10-11・10-12 ユーザー決定。Issue #168）:

- **支配下の空き**（`release_counts`）: ふだんの新人（`draft.USUAL_DRAFTEES`）を迎えても天井（帯の上端か支配下の上限の
  低い方）を超えない人数まで外す。式は `OPENING_REGISTERED_BAND.excess` が出典（ドラフトの `gap` と同じ天井）。
  外すのは、その年の主力（`PlayingTime.is_regular`）と入団1年目を除いた支配下から、残す価値（`keep_value`）の低い順
- **外国人の入れ替え**（`is_replaceable_foreigner`）: 外国人は、残す価値が**新しく迎える外国人に見込める価値**
  （`foreign_prospect_value`。ドラフトの外国人の新人の分布から導く）を下回れば、主力でも外す
  （入団1年目は `FIRST_YEAR_FOREIGN_MARGIN` 点の余裕を持たせる）。
  全員が下回れば全員を入れ替える。外した外国人はドラフトが同じオフに目標（`draft.foreign_target`）まで補う

どちらも育成は外さない（#170・#174）。位置の最低人数（`draft.MIN_BY_POSITION`）を割らず、候補が足りなければ
外せる分だけ（締める処理を失敗させない）。外国人を外した分も支配下の空きに数える。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import fmean

from ..value_objects import Position
from .draft import FOREIGN_AGE_RANGE, MIN_BY_POSITION, USUAL_DRAFTEES, DraftRoute, expected_rookie_ratings
from .offseason_player import OffseasonClub, OffseasonPlayer, player_age, retirement_value
from .roster import OPENING_REGISTERED_BAND

# 残す価値の年齢の補正。この年齢より若い選手は、1歳につき `YOUTH_CREDIT` 点だけ総合値に上乗せして残しやすくする
# （伸びしろのある若手を、今の能力だけで外さない）
PRIME_AGE = 27
YOUTH_CREDIT = 1.0
# 入団1年目の外国人の余裕（点）。1年目だけ、新しく迎える外国人の見込みからこの値を引いて比べ、大きく下回るときだけ外す
# （2026-10-12 ユーザー決定。0 だと、見込みの平均の近くで入った1年目の外国人が成績と関係なく半分近く外れる。
# 値は実測で決めた。Wiki「ペナントモードの仕組み」の「戦力外」）
FIRST_YEAR_FOREIGN_MARGIN = 5.0


@dataclass(frozen=True)
class ReleaseCounts:
    """球団が支配下の空きを作るために外したい人数（国籍を問わない）。"""

    registered: int


def release_counts(*, registered: int, registered_limit: int | None) -> ReleaseCounts:
    """支配下の空きを作るために外したい人数。**人数の式の出典はここだけ**。

    `registered` は引退の後の支配下の人数（育成は数えない）。`excess(registered, expected=USUAL_DRAFTEES)` 人。
    """
    return ReleaseCounts(
        registered=OPENING_REGISTERED_BAND.excess(registered, limit=registered_limit, expected=USUAL_DRAFTEES)
    )


def youth_credit(age: int) -> float:
    """若さの上乗せ。`PRIME_AGE` より若い1歳ごとに `YOUTH_CREDIT` 点。"""
    return YOUTH_CREDIT * max(0, PRIME_AGE - age)


def keep_value(player: OffseasonPlayer, *, year: int, start_year: int) -> float:
    """残す価値。総合値（`retirement_value`。捕手は守備力と打撃の大きい方）に、若さの上乗せを足す。低いほど外される。"""
    ratings = None if player.ratings is None else player.ratings.ratings
    return retirement_value(player.position, ratings) + youth_credit(
        player_age(player.profile, year, start_year=start_year)
    )


def foreign_prospect_value(position: Position) -> float:
    """新しく迎える外国人に見込める残す価値（`keep_value` と同じ物差し）。

    能力はドラフトの外国人の新人の平均（`draft.expected_rookie_ratings`。錨の前の尺度で、今の選手の能力と同じ尺度）、
    若さの上乗せは外国人の新人の年齢（`draft.FOREIGN_AGE_RANGE`。範囲の整数から一様に引く）の平均。
    外国人の新人は捕手にしないので、捕手は内野手の見込みで比べる。
    """
    compared = Position.INFIELDER if position is Position.CATCHER else position
    ratings = expected_rookie_ratings(DraftRoute.FOREIGN, compared)
    low, high = FOREIGN_AGE_RANGE
    return retirement_value(compared, ratings) + fmean(youth_credit(age) for age in range(low, high + 1))


def is_release_candidate(player: OffseasonPlayer, *, year: int) -> bool:
    """支配下の空きのために AI が外しうる選手か。支配下で、その年の主力でなく、入団1年目（`is_first_year`）でない。"""
    return player.is_registered and not player.playing_time.is_regular and not player.is_first_year(year)


def is_replaceable_foreigner(player: OffseasonPlayer, *, year: int, start_year: int) -> bool:
    """新しい外国人と入れ替える外国人か。支配下の外国人で、残す価値が新しく迎える外国人の見込みを下回る（主力も）。

    入団1年目の外国人は、見込みから `FIRST_YEAR_FOREIGN_MARGIN` を引いた値と比べる（大きく下回るときだけ外す）。
    """
    if not (player.is_registered and player.profile.is_foreign_player):
        return False
    threshold = foreign_prospect_value(player.position)
    if player.is_first_year(year):
        threshold -= FIRST_YEAR_FOREIGN_MARGIN
    return keep_value(player, year=year, start_year=start_year) < threshold


def decide_releases(
    club: OffseasonClub, remaining: Sequence[OffseasonPlayer], *, year: int, start_year: int
) -> tuple[int, ...]:
    """球団 `club` の戦力外の選手の id（外す順）。`remaining` は引退しなかった選手。

    先に入れ替える外国人、続けて支配下の候補から `ReleaseCounts.registered` 人に届くまで。
    どちらも残す価値の昇順（同値は選手 id の昇順）で、その位置の支配下が `MIN_BY_POSITION` 以下なら飛ばす。
    """
    counts = release_counts(
        registered=sum(1 for player in remaining if player.is_registered),
        registered_limit=club.registered_player_limit,
    )
    by_position = Counter(player.position for player in remaining if player.is_registered)

    def order(players: Sequence[OffseasonPlayer]) -> list[OffseasonPlayer]:
        return sorted(
            players, key=lambda player: (keep_value(player, year=year, start_year=start_year), player.player_id)
        )

    chosen: list[int] = []

    def take(pool: Sequence[OffseasonPlayer], until: int) -> None:
        for player in pool:
            if len(chosen) >= until:
                return
            if player.player_id in chosen or by_position[player.position] <= MIN_BY_POSITION.get(player.position, 0):
                continue
            by_position[player.position] -= 1
            chosen.append(player.player_id)

    foreigners = order([p for p in remaining if is_replaceable_foreigner(p, year=year, start_year=start_year)])
    take(foreigners, len(foreigners))
    if counts.registered > len(chosen):
        take(order([p for p in remaining if is_release_candidate(p, year=year)]), counts.registered)
    return tuple(chosen)
