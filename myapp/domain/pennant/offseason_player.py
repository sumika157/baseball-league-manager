"""シーズンを締めるときの材料（球団と選手）と、選手ひとりの値（総合値・年齢）。

**純粋な関数**で、DB にも Django にも触れない。

締める計算（`offseason.py`）と戦力外（`release.py`）が共有する。`offseason.py` が `release.py` を使うので、
両方が読むものをここに置く（循環させない）。呼び出し側は従来どおり `offseason.py` から読んでよい。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..simulation.ratings import BatterRatings, PitcherRatings
from ..value_objects import ContractStatus, Position, Profile
from .fork import ESTIMATED_AGE_AT_START, with_birth_date
from .initial_ratings import age_at_season_start
from .ratings import PlayerRatings
from .retirement import PlayingTime

# 能力を持たない選手の総合値（引退の確率に効かない値）
_NEUTRAL_VALUE = 45.0

Ratings = BatterRatings | PitcherRatings


@dataclass(frozen=True)
class OffseasonPlayer:
    """締める年 Y の現役の選手。"""

    player_id: int
    position: Position
    profile: Profile
    # 背番号の表記（「00」と「0」は別の番号）
    number: str
    # 現在の在籍の開始年（分岐した選手は一律に開幕年。入団年は entered_year）
    joined_year: int
    # Y の能力。無い選手は翌年の能力も作らない
    ratings: PlayerRatings | None
    # 今の契約区分（集約の `Team.contract_of`＝`Stint.contract_now` の値を渡す。ここで判定し直さない）。
    # 在籍が無ければ None。ドラフトの指名数と位置の補充は支配下だけで数える（育成は試合に出られない）
    contract: ContractStatus | None
    playing_time: PlayingTime = PlayingTime()

    @property
    def is_registered(self) -> bool:
        return self.contract is ContractStatus.REGISTERED

    @property
    def entered_year(self) -> int:
        """プロ入りの年。入団年（`debut_year`）があればそれ、無ければ現在の在籍の開始年。

        分岐した選手の在籍の開始年は一律に開幕年になる。若手の保護（入団2年以内）を在籍の開始年で数えると、
        実際の経歴にかかわらず最初の2シーズン保護されてしまうので、入団年を優先する。
        """
        return self.profile.debut_year if self.profile.debut_year is not None else self.joined_year

    def seasons_as_pro(self, year: int) -> int:
        """`year` を含めてプロにいた年数（1以上）。引退の若手の保護（入団2年以内）の判定に使う。

        入団年が空なら在籍の開始年で数える（分からない選手も保護の対象になりうる側に倒す）。
        """
        return max(1, year - self.entered_year + 1)

    def is_first_year(self, year: int) -> bool:
        """入団1年目（入団年 `debut_year` が `year` 以降）か。戦力外（`release.py`）が使う。

        `seasons_as_pro` と違い、入団年が空なら1年目と見なさない。在籍の開始年で代用すると、分岐した選手は
        一律に開幕年から在籍するので、最初のオフで全員が1年目として戦力外から守られてしまう。新人は必ず入団年を持つ。
        """
        return self.profile.debut_year is not None and self.profile.debut_year >= year


@dataclass(frozen=True)
class OffseasonClub:
    """締める年 Y の球団。"""

    team_id: int
    # リーグの外国人の登録枠。None は無制限
    foreign_roster_limit: int | None
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
