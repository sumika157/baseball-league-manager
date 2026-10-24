"""FA 資格の推定の材料の組み立て。

参照クエリ（`ServiceHistoryQuery`）が読んだ選手の年ごとの出場・在籍の契約・FA 宣言を、ドメインの
`fa_outlook` に渡す形にする。推定の規則はドメイン（`domain/services/free_agency.py`）にあり、ここは
材料を選手ごとに振り分けるだけ。戦力分析の FA 取得タブと外国人枠の対象外（`foreign_quota.py`）は、
ここを通して見込みを出す（同じ組み立てを通るので、タブの取得年と枠の扱いがずれない）。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence, Set
from dataclasses import dataclass
from typing import Protocol

from ..domain import services as domain_services
from ..domain.value_objects import ContractStatus, DraftPick, Profile
from .dto import PlayerContractStint, ServiceHistory


class FaPlayer(Protocol):
    """FA の推定の対象の選手（ドラフト・外国人選手フラグ・学歴）。戦力分析の在籍の行などがこの形を持つ。"""

    @property
    def player_id(self) -> int: ...

    @property
    def draft(self) -> DraftPick | None: ...

    @property
    def is_foreign_player(self) -> bool: ...

    @property
    def high_school(self) -> str: ...

    @property
    def university(self) -> str: ...

    @property
    def corporate_team(self) -> str: ...


@dataclass(frozen=True)
class FaInputs:
    """選手1人の FA の推定の材料（`fa_outlook` の引数そのもの）。"""

    education: domain_services.EducationPath
    entry: domain_services.ProEntry | None
    days_by_year: Mapping[int, int]
    through_year: int
    records_from_year: int
    declared_years: Sequence[int]
    developmental_years: Set[int]

    def outlook(self) -> domain_services.FaOutlook:
        return domain_services.fa_outlook(
            education=self.education,
            entry=self.entry,
            days_by_year=self.days_by_year,
            through_year=self.through_year,
            records_from_year=self.records_from_year,
            declared_years=self.declared_years,
            developmental_years=self.developmental_years,
        )

    def first_domestic_acquisition_year(self) -> int | None:
        """国内 FA 権を最初に取得した年（FA 宣言で数え直す前。外国人枠の対象外の起点）。"""
        return domain_services.first_domestic_acquisition_year(
            education=self.education,
            entry=self.entry,
            days_by_year=self.days_by_year,
            through_year=self.through_year,
            records_from_year=self.records_from_year,
            developmental_years=self.developmental_years,
        )

    def foreign_quota_exempt_from(self) -> int | None:
        """外国人枠の対象外になる年（最初の取得の翌年と最初の宣言の翌年の早いほう）。外国人かどうかは見ない。"""
        return domain_services.foreign_quota_exempt_from(
            acquired_year=self.first_domestic_acquisition_year(), declared_years=self.declared_years
        )


def fa_inputs(players: Iterable[FaPlayer], history: ServiceHistory, *, through_year: int) -> dict[int, FaInputs]:
    """選手ごとの FA の推定の材料（キーは選手の id）。`history` は `through_year` 年までを読んだもの。

    登録日数は年ごとの初出場〜最終出場の日数で、育成だった年は数えない（除外はドメインが行う）。
    FA 宣言は `history` にある分（`through_year` 年まで）を渡し、最後の宣言の翌年から数え直す（再取得）。
    ドラフトを経ていない選手の数え始め（`ProEntry`）は、全球団の在籍のうち最初の加入年。
    """
    stints: dict[int, list[PlayerContractStint]] = defaultdict(list)
    for stint in history.stints:
        stints[stint.player_id].append(stint)
    first_stint_year = {player_id: min(s.from_year for s in owned) for player_id, owned in stints.items()}
    declared_years: dict[int, list[int]] = defaultdict(list)
    for declaration in history.declarations:
        declared_years[declaration.player_id].append(declaration.year)
    days: dict[int, dict[int, int]] = defaultdict(dict)
    for span in history.spans:
        days[span.player_id][span.year] = domain_services.estimated_service_days(span.first_on, span.last_on)
    # 記録が無ければ数えられる年が無いので、表示年を始まりとして「記録より前」を作らない
    records_from = history.records_from_year if history.records_from_year is not None else through_year

    inputs: dict[int, FaInputs] = {}
    for player in players:
        by_year = days[player.player_id]
        # 育成だった年かの判定だけをここで行い（在籍の契約から）、数えない扱いはドメインに任せる
        developmental_years = {y for y in by_year if _is_developmental_year(stints[player.player_id], y)}
        inputs[player.player_id] = FaInputs(
            education=domain_services.EducationPath.of(
                Profile(
                    high_school=player.high_school,
                    university=player.university,
                    corporate_team=player.corporate_team,
                )
            ),
            entry=domain_services.ProEntry.of(
                draft=player.draft,
                is_foreign_player=player.is_foreign_player,
                first_stint_year=first_stint_year.get(player.player_id),
            ),
            days_by_year=by_year,
            through_year=through_year,
            records_from_year=records_from,
            declared_years=declared_years[player.player_id],
            developmental_years=developmental_years,
        )
    return inputs


def _is_developmental_year(stints: list[PlayerContractStint], year: int) -> bool:
    """その年に在籍があり、そのすべてが育成か。在籍が無い年は育成とは言えないので False。"""
    covering = [s for s in stints if s.from_year <= year and (s.to_year is None or year <= s.to_year)]
    return bool(covering) and all(s.contract.contract_in(year) is ContractStatus.DEVELOPMENTAL for s in covering)
