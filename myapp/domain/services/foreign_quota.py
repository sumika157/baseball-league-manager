"""FA 資格の取得で外国人枠の対象外になるか（日本プロフェッショナル野球協約 第82条 (4)）。Django には依存しない。

協約の原文（第82条 (4)。第11章「選手数の制限」の適用に限り、外国人選手でないものとみなす）:
「選手契約締結以後、この組織が定めるフリーエージェント資格を取得した者。当該選手はコミッショナー公示のあった年の
次の年度連盟選手権試合シーズンからこの適用を受ける。」

- **効くのは外国人枠（第82条の2）の数え方だけ。** 国籍・外国人選手フラグ・「外国人」の印・FA の数え方は変えない。
- 対象外になる年は、国内 FA の**最初の取得年の翌年**と、**最初の FA 宣言の翌年**の早いほう（宣言には資格が要るので、
  宣言は取得の証拠）。取得年は FA 取得タブと同じ推定（`free_agency.first_domestic_acquisition_year`）から導き、
  保存しない。
- **シーズン Y の扱いは Y−1 年までの記録だけで決める**（FA の公示はオフ）。Y 年の試合が Y 年の扱いを
  変えないので循環しない。
- 枠を**緩める向きにだけ**使う（数えない人を増やすだけで、フラグの無い選手を数えることはない）。
- 一度対象外になったら戻らない（取得と宣言は後の年の記録で消えない）。
"""

from __future__ import annotations

from collections.abc import Iterable


def foreign_quota_exempt_from(*, acquired_year: int | None, declared_years: Iterable[int]) -> int | None:
    """外国人枠の対象外になる最初の年。国内 FA の最初の取得年の翌年と、最初の FA 宣言の翌年の早いほう。

    どちらも無ければ None（対象外にならない）。`acquired_year` は `first_domestic_acquisition_year` の値。
    """
    candidates = [year + 1 for year in declared_years]
    if acquired_year is not None:
        candidates.append(acquired_year + 1)
    return min(candidates) if candidates else None


def counts_toward_foreign_quota(*, is_foreign_player: bool, exempt_from: int | None, year: int) -> bool:
    """その年の外国人枠（第82条の2）で数えるか。**誰を数えるかの唯一の判定。**

    外国人選手フラグの無い選手は数えない。フラグのある選手は、対象外の開始年（`foreign_quota_exempt_from`）に
    達した年からは数えない。`exempt_from` は `year − 1` 年までの記録から導いた値を渡す（協約は「公示の次の年度の
    シーズンから」。対象外を導かない場面は None を明示して、フラグどおりに数える）。
    """
    if not is_foreign_player:
        return False
    return exempt_from is None or year < exempt_from
