"""FA（フリーエージェント）権の取得の見込み。

NPB「フリーエージェントについて」（https://npb.jp/announcement/2019/fa_about.html）の規則を、
この1か所に定数として持つ。登録日数は保存も入力もせず、試合への出場（初出場〜最終出場）から
推定した値を受け取って数える。Django には依存しない。

- 出場選手登録が145日以上の年を1シーズンと数える。145日に満たない年は日数を合算し、
  145日に達するごとに1シーズン（合算した残りは次の年へ持ち越す）
- 国内FA 8シーズン（2007年以降のドラフトで入団した大卒・社会人は7シーズン。
  入団はドラフトの翌年なので入団年では2008年以降）、海外FA 9シーズン
- 権利を行使（宣言）した後は、宣言の翌年から数え直して4シーズンで再び取得する
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from enum import Enum

from ..value_objects import Profile

# 1シーズンと数える出場選手登録の日数
SERVICE_DAYS_PER_SEASON = 145
# 大卒・社会人の短縮（7シーズン）が適用される**ドラフトの年**の下限（原文は「2007年以降のドラフトで入団した」）。
# ドラフトはその年の秋で入団は翌年なので、入団年（`Profile.debut_year`）で比べるときは「入団年 - 1」を使う
# （入団年が2008年以降なら短縮）。入団年＝ドラフトの翌年とみなす（自由獲得枠などの例外は扱わない）
COLLEGE_RULE_FROM_DRAFT_YEAR = 2007
DOMESTIC_SEASONS = 8
DOMESTIC_SEASONS_COLLEGE = 7
OVERSEAS_SEASONS = 9
# 権利を行使した後に、再び取得するまでのシーズン数
REACQUIRE_SEASONS = 4


class EducationPath(Enum):
    """プロ入り前の学歴の区分。国内FAの必要シーズン数が変わる。"""

    HIGH_SCHOOL = "高卒"
    UNIVERSITY = "大卒"
    CORPORATE = "社会人"
    UNKNOWN = "不明"

    @classmethod
    def of(cls, profile: Profile) -> EducationPath:
        """社会人があれば社会人、なければ大学があれば大卒、高校だけなら高卒、どれも無ければ不明。"""
        if profile.corporate_team:
            return cls.CORPORATE
        if profile.university:
            return cls.UNIVERSITY
        if profile.high_school:
            return cls.HIGH_SCHOOL
        return cls.UNKNOWN


class FaStatusKind(Enum):
    ACQUIRED = "取得済み"
    REMAINING = "取得まで"
    UNKNOWN = "不明"


@dataclass(frozen=True)
class ServiceTime:
    """数えたシーズン数と、次のシーズンに持ち越す日数（145日未満）。"""

    seasons: int
    remainder_days: int


@dataclass(frozen=True)
class FaStatus:
    """国内または海外のFA権の状況。

    UNKNOWN でも remaining を持つことがある（記録より前の年が数えられず、数えた分だけで見た
    「あと最大 n シーズン」。数えられない年が足されるほど残りは減るので上限になる）。
    """

    kind: FaStatusKind
    required: int | None = None
    remaining: int | None = None
    acquired_year: int | None = None
    # 取得年が「遅くともこの年」の意味か（記録より前の年が数えられていないとき。実際はもっと早い可能性がある）
    acquired_year_is_latest: bool = False

    @property
    def label(self) -> str:
        if self.kind is FaStatusKind.ACQUIRED:
            prefix = "遅くとも" if self.acquired_year_is_latest else ""
            return f"取得済み（{prefix}{self.acquired_year}年）"
        if self.kind is FaStatusKind.REMAINING:
            return f"あと{self.remaining}シーズン"
        if self.remaining is not None:
            return f"不明（あと最大{self.remaining}シーズン）"
        return "不明"


@dataclass(frozen=True)
class FaOutlook:
    """選手1人のFA権の見込み。service は入団年が分からないときは None（数えられない）。"""

    education: EducationPath
    service: ServiceTime | None
    domestic: FaStatus
    overseas: FaStatus
    # 数えた日数は試合からの推定。数えた日数が1日でもあるときに True
    includes_estimate: bool


def estimated_service_days(first: date, last: date) -> int:
    """その年の初出場から最終出場までの日数（両端を含む）。登録日数の推定値。"""
    return (last - first).days + 1


def _running_seasons(days_by_year: Mapping[int, int], since: int, through: int) -> list[tuple[int, ServiceTime]]:
    """年ごとの累計（その年を数え終えた時点のシーズン数と持ち越しの日数）。"""
    seasons = 0
    carry = 0
    running: list[tuple[int, ServiceTime]] = []
    for year in sorted(y for y in days_by_year if since <= y <= through):
        days = days_by_year[year]
        if days >= SERVICE_DAYS_PER_SEASON:
            seasons += 1
        else:
            carry += max(days, 0)
            if carry >= SERVICE_DAYS_PER_SEASON:
                seasons += 1
                carry -= SERVICE_DAYS_PER_SEASON
        running.append((year, ServiceTime(seasons, carry)))
    return running


def service_seasons(days_by_year: Mapping[int, int], *, since: int = 0, through: int = 9999) -> ServiceTime:
    """年ごとの登録日数から、数えたシーズン数と持ち越しの日数を求める。since 以降・through 以前の年だけを数える。"""
    running = _running_seasons(days_by_year, since, through)
    return running[-1][1] if running else ServiceTime(0, 0)


def _status(
    running: list[tuple[int, ServiceTime]], required: int, counted: ServiceTime, *, complete: bool
) -> FaStatus:
    for year, time in running:
        if time.seasons >= required:
            # 記録より前が欠けていても、数えた分で足りていれば取得はしている。ただしその年が最初とは限らない
            return FaStatus(
                FaStatusKind.ACQUIRED, required=required, acquired_year=year, acquired_year_is_latest=not complete
            )
    remaining = required - counted.seasons
    if complete:
        return FaStatus(FaStatusKind.REMAINING, required=required, remaining=remaining)
    return FaStatus(FaStatusKind.UNKNOWN, required=required, remaining=remaining)


def fa_outlook(
    *,
    education: EducationPath,
    debut_year: int | None,
    days_by_year: Mapping[int, int],
    through_year: int,
    records_from_year: int,
    declared_years: Sequence[int] = (),
    developmental_years: Collection[int] = (),
) -> FaOutlook:
    """FA権の見込み。

    days_by_year は年ごとの登録日数（試合の無い年は0日）。developmental_years は育成だった年で、
    その年の日数は数えない（除外はここで行う）。
    records_from_year は試合の記録がある最初の年。入団がそれより前だと、記録より前の年は数えられないので、
    取得済みでも取得年は「遅くとも」になり、取得済みでなければ不明にする。
    declared_years は権利を行使した年（無ければ空）で、最後の宣言の翌年から数え直す。
    入団年が分からないときは数えられず不明。学歴が分からないときの国内FAは、数えたシーズンが8以上なら
    取得済み、ドラフトが2007年より前（入団が2007年以前）なら学歴によらず8シーズンで見て、それ以外は不明
    （海外FAは学歴によらないので数える）。入団年はドラフトの翌年とみなす。
    """
    unknown = FaStatus(FaStatusKind.UNKNOWN)
    if debut_year is None:
        return FaOutlook(education, None, unknown, unknown, includes_estimate=False)

    days_by_year = {year: (0 if year in developmental_years else days) for year, days in days_by_year.items()}
    reacquiring = bool(declared_years)
    since = max(debut_year, max(declared_years) + 1) if reacquiring else debut_year
    running = _running_seasons(days_by_year, since, through_year)
    counted = running[-1][1] if running else ServiceTime(0, 0)
    includes_estimate = any(days > 0 for year, days in days_by_year.items() if since <= year <= through_year)
    complete = since >= records_from_year

    # 海外FAは学歴に関係なく決まるので、学歴が分からなくても数える（国内FAだけが学歴で変わる）
    overseas_required = REACQUIRE_SEASONS if reacquiring else OVERSEAS_SEASONS
    overseas = _status(running, overseas_required, counted, complete=complete)
    drafted_before_rule = debut_year - 1 < COLLEGE_RULE_FROM_DRAFT_YEAR
    if reacquiring:
        domestic_required = REACQUIRE_SEASONS
    elif education is EducationPath.UNKNOWN and not drafted_before_rule:
        # 7か8か決まらない。8シーズン数えていれば学歴によらず取得済み、それ以外は不明
        domestic = _status(running, DOMESTIC_SEASONS, counted, complete=complete)
        if domestic.kind is not FaStatusKind.ACQUIRED:
            domestic = unknown
        return FaOutlook(education, counted, domestic, overseas, includes_estimate)
    else:
        is_college = education in (EducationPath.UNIVERSITY, EducationPath.CORPORATE)
        shortened = is_college and not drafted_before_rule
        domestic_required = DOMESTIC_SEASONS_COLLEGE if shortened else DOMESTIC_SEASONS
    return FaOutlook(
        education,
        counted,
        _status(running, domestic_required, counted, complete=complete),
        overseas,
        includes_estimate,
    )


def fa_order(outlook: FaOutlook) -> tuple[int, int]:
    """並びのキー。取得済み、取得が近い順、不明（数えた分だけの上限があるものを先）の順。国内FAで見る。"""
    status = outlook.domestic
    if status.kind is FaStatusKind.ACQUIRED:
        return (0, 0)
    if status.kind is FaStatusKind.REMAINING:
        return (1, status.remaining or 0)
    if status.remaining is not None:
        return (2, status.remaining)
    return (3, 0)
