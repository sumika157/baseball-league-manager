"""シーズンを締めるときの材料（球団と選手）と、選手ひとりの値（総合値・年齢）。

**純粋な関数**で、DB にも Django にも触れない。

締める計算（`offseason.py`）と戦力外（`release.py`）が共有する。`offseason.py` が `release.py` を使うので、
両方が読むものをここに置く（循環させない）。呼び出し側は従来どおり `offseason.py` から読んでよい。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ..entities import Stint
from ..simulation.ratings import BatterRatings, PitcherRatings
from ..value_objects import (
    AcquisitionRoute,
    BattingLine,
    ContractStatus,
    ForeignPlayerRules,
    PitchingLine,
    Position,
    Profile,
)
from .fork import ESTIMATED_AGE_AT_START, with_birth_date
from .initial_ratings import age_at_season_start
from .ratings import PlayerRatings
from .retirement import PlayingTime

# 能力を持たない選手の総合値（引退の確率に効かない値）
_NEUTRAL_VALUE = 45.0

Ratings = BatterRatings | PitcherRatings


@dataclass(frozen=True)
class TrackRecord:
    """選手の実績（その球団での在籍年数・生え抜きと、世界の中の通算成績・タイトルの数（全球団））。戦力外の「実績の加点」（`release.track_record_credit`）の材料。

    在籍・世界の試合・主将・表彰から呼び出し側が導いて渡す値で、**これ自体は保存しない**。世界の中の事実だけを数える
    （分岐で写すのは現在の在籍だけなので、分岐した選手の分岐前の経歴と成績は数えない）。
    """

    # 生え抜き（その球団にドラフト・育成ドラフトで入り、ほかの球団に在籍したことがない）
    homegrown: bool = False
    # その球団に在籍した年数（締める年を含む）
    seasons: int = 0
    # 世界の通算（締める年まで）
    hits: int = 0
    home_runs: int = 0
    wins: int = 0
    saves: int = 0
    holds: int = 0
    # 世界の中で取ったタイトルの数（締めた年までの表彰と、いま締める年の受賞。同率の受賞も1つ。`awards.py`）
    titles: int = 0
    # いまの主将
    is_captain: bool = False

    @classmethod
    def of(
        cls,
        *,
        team_id: int,
        career: Sequence[Stint],
        year: int,
        batting: BattingLine,
        pitching: PitchingLine,
        is_captain: bool,
        titles: int,
    ) -> TrackRecord:
        """在籍（`career`。選手の全在籍）と世界の通算成績・タイトルの数から、実績を導く。

        成績は世界の中の通算（全球団の試合。いまは世界に移籍が無いので、その球団の分と同じ）、
        在籍年数と生え抜きは球団 `team_id` での事実。

        在籍年数は、その球団の在籍が覆う年（締める年 `year` まで）の数。生え抜きは、最初の在籍がその球団への
        ドラフト・育成ドラフトでの入団で、ほかの球団に在籍したことがない選手（入団の経路が不明な在籍は生え抜きと見なさない）。
        """
        own = [stint for stint in career if stint.team_id == team_id and stint.from_year <= year]
        seasons = sum(min(year, stint.to_year or year) - stint.from_year + 1 for stint in own)
        first = min(career, key=lambda stint: stint.from_year, default=None)
        homegrown = (
            first is not None
            and first.team_id == team_id
            and first.acquired_via in _HOMEGROWN_ROUTES
            and all(stint.team_id == team_id for stint in career)
        )
        return cls(
            homegrown=homegrown,
            seasons=seasons,
            hits=batting.hits,
            home_runs=batting.home_runs,
            wins=pitching.wins,
            saves=pitching.saves,
            holds=pitching.holds,
            is_captain=is_captain,
            titles=titles,
        )


_HOMEGROWN_ROUTES = frozenset({AcquisitionRoute.DRAFT, AcquisitionRoute.DEVELOPMENTAL_DRAFT})


@dataclass(frozen=True)
class OffseasonPlayer:
    """締める年 Y の現役の選手。"""

    player_id: int
    position: Position
    profile: Profile
    # 背番号の表記（「00」と「0」は別の番号）
    number: str
    # 現在の在籍の開始年（分岐した選手は一律に開幕年。プロ入りの年は entered_year）
    joined_year: int
    # Y の能力。無い選手は翌年の能力も作らない
    ratings: PlayerRatings | None
    # 今の契約区分（集約の `Team.contract_of`＝`Stint.contract_now` の値を渡す。ここで判定し直さない）。
    # 在籍が無ければ None。ドラフトの指名数と位置の補充は支配下だけで数える（育成は試合に出られない）
    contract: ContractStatus | None
    playing_time: PlayingTime = PlayingTime()
    # その球団での実績（戦力外で外されにくくするだけ。試合の成績には効かない）
    track_record: TrackRecord = TrackRecord()

    @property
    def is_registered(self) -> bool:
        return self.contract is ContractStatus.REGISTERED

    @property
    def is_development(self) -> bool:
        """今の区分が育成か（在籍の無い選手はどちらでもない）。錨の母集団から外し、育成ドラフトの人数に数える。"""
        return self.contract is ContractStatus.DEVELOPMENTAL

    @property
    def entered_year(self) -> int:
        """プロ入りの年。ドラフトがあればその翌年（`DraftPick.entry_year`）、無ければ現在の在籍の開始年。

        分岐した選手の在籍の開始年は一律に開幕年になる。若手の保護（入団2年以内）を在籍の開始年で数えると、
        実際の経歴にかかわらず最初の2シーズン保護されてしまうので、ドラフトを優先する。
        """
        draft = self.profile.draft
        return draft.entry_year if draft is not None else self.joined_year

    def seasons_as_pro(self, year: int) -> int:
        """`year` を含めてプロにいた年数（1以上）。引退の若手の保護（入団2年以内）の判定に使う。

        ドラフトが無ければ在籍の開始年で数える（分からない選手も保護の対象になりうる側に倒す）。
        """
        return max(1, year - self.entered_year + 1)

    def is_first_year(self, year: int, *, start_year: int) -> bool:
        """入団1年目（プロ入りの年が `year` 以降）か。戦力外（`release.py`）が使う。`start_year` は世界の開幕年。

        ドラフトがあればその翌年（`DraftPick.entry_year`）で見る。ドラフトの無い選手（外国人の新人など）は、
        在籍の開始年が開幕年より後（世界の中で迎えた選手）のときだけ在籍の開始年で見る。分岐した選手は一律に
        開幕年から在籍するので、在籍の開始年で代用すると、最初のオフで全員が1年目として戦力外から守られてしまう
        （`seasons_as_pro` と違い、分からない選手は1年目と見なさない側に倒す）。
        """
        draft = self.profile.draft
        if draft is not None:
            return draft.entry_year >= year
        return start_year < self.joined_year and self.joined_year >= year


@dataclass(frozen=True)
class OffseasonClub:
    """締める年 Y の球団。"""

    team_id: int
    # リーグの外国人枠（1軍登録。協約第82条の2）。ドラフトの外国人の目標人数に計（`active`）を使う
    foreign_rules: ForeignPlayerRules
    # リーグの支配下の上限（NPB は70人）。None は無制限。ドラフトはこれを超えない人数にする
    registered_player_limit: int | None
    players: tuple[OffseasonPlayer, ...]


def overall_value(ratings: Ratings) -> float:
    """能力の総合値。野手は打撃（`batting_value`）、投手は抑える力（`pitching_value`）。画面の「総合」の出典。"""
    if isinstance(ratings, PitcherRatings):
        return ratings.pitching_value
    return ratings.batting_value


def retirement_value(position: Position, ratings: Ratings | None) -> float:
    """引退の確率と戦力外の順に使う総合値。野手は打撃、投手は抑える力。捕手は守備力と打撃の大きい方。"""
    if ratings is None:
        return _NEUTRAL_VALUE
    if isinstance(ratings, BatterRatings) and position is Position.CATCHER:
        return max(float(ratings.fielding), ratings.batting_value)
    return overall_value(ratings)


def player_age(profile: Profile, year: int, *, start_year: int) -> int:
    """締める年 Y の4月1日時点の満年齢。生年月日が空なら推定で補って数える。"""
    age = age_at_season_start(with_birth_date(profile, start_year), year)
    return ESTIMATED_AGE_AT_START if age is None else age
