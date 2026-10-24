"""FA 資格の取得による外国人枠の対象外（協約 第82条 (4)）を、その年について導く。

**保存しない。** 毎回、FA 取得タブと同じ材料・同じ組み立て（`free_agency.fa_inputs`）から、年 Y の扱いを
Y−1 年までの記録で導く。判定（誰を数えるか）はドメインの `counts_toward_foreign_quota` が持ち、ここは材料を読んで
選手ごとの対象外の開始年を渡すだけ。

ふだんの読み込みを増やさないため、出場（`load_service_history`。重い）を読む前に、選手の属性と在籍の加入年
だけで「その年までに国内 FA を取得しえない」選手を外す（`earliest_domestic_acquisition_year`）。記録が浅い世界や
実データ（2026年から）では、ここで全員が外れて出場を読まない。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field

from ..domain import services as domain_services
from .dto import FaSubject
from .free_agency import fa_inputs
from .queries import ServiceHistoryQuery


@dataclass(frozen=True)
class QuotaExemptions:
    """ある年の、外国人選手ごとの外国人枠の対象外の開始年（対象外にならない選手は載らない）。"""

    year: int
    exempt_from: Mapping[int, int] = field(default_factory=dict)

    def counts(self, player_id: int, *, is_foreign_player: bool) -> bool:
        """その選手をこの年の外国人枠で数えるか（判定はドメインの `counts_toward_foreign_quota`）。"""
        return domain_services.counts_toward_foreign_quota(
            is_foreign_player=is_foreign_player, exempt_from=self.exempt_from.get(player_id), year=self.year
        )


class ForeignQuotaExemptions:
    """外国人枠の対象外を導く。参照クエリは使う側の範囲（実データ・世界）で組み立てて渡す。"""

    def __init__(self, query: ServiceHistoryQuery) -> None:
        self._query = query

    def for_year(self, foreign_player_ids: Collection[int], year: int) -> QuotaExemptions:
        """`foreign_player_ids`（外国人選手フラグの選手）の、`year` 年の外国人枠の対象外。

        読むのは `year − 1` 年までの記録（協約は「公示の次の年度のシーズンから」）。選手が空なら何も読まない。
        対象外になるのは、国内 FA の最初の取得年の翌年か、最初の FA 宣言の翌年（早いほう）に達した選手。
        """
        ids = sorted(set(foreign_player_ids))
        if not ids:
            return QuotaExemptions(year)
        through = year - 1
        material = self._query.load_fa_subjects(ids, through)
        declared: dict[int, list[int]] = defaultdict(list)
        for declaration in material.declarations:
            declared[declaration.player_id].append(declaration.year)

        exempt_from: dict[int, int] = {}
        candidates: list[FaSubject] = []
        for subject in material.subjects:
            if not subject.is_foreign_player:
                continue
            # 宣言だけで決まる分（宣言には資格が要る）。取得も見る選手は、下で取得年と合わせて決め直す
            by_declaration = domain_services.foreign_quota_exempt_from(
                acquired_year=None, declared_years=declared[subject.player_id]
            )
            if by_declaration is not None:
                exempt_from[subject.player_id] = by_declaration
            if self._may_have_acquired(subject, material.records_from_year, through):
                candidates.append(subject)
        if candidates:
            history = self._query.load_service_history([subject.player_id for subject in candidates], through)
            inputs = fa_inputs(candidates, history, through_year=through)
            for subject in candidates:
                found = inputs[subject.player_id].foreign_quota_exempt_from()
                if found is not None:
                    exempt_from[subject.player_id] = found
        return QuotaExemptions(year, exempt_from)

    @staticmethod
    def _may_have_acquired(subject: FaSubject, records_from_year: int | None, through: int) -> bool:
        """`through` 年までに国内 FA を取得しえたか（出場を読む前の絞り込み。取得しえない選手を外すだけ）。"""
        if records_from_year is None:
            return False
        entry = domain_services.ProEntry.of(
            draft=subject.draft,
            is_foreign_player=subject.is_foreign_player,
            first_stint_year=subject.first_stint_year,
        )
        if entry is None:
            return False
        return domain_services.earliest_domestic_acquisition_year(entry, records_from_year) <= through
