"""実データのリーグを、世界の初期状態として分岐させる。

分岐（スナップショット）で写すのは、リーグ・球団・選手のプロフィール・
**現在の在籍だけ**。試合と過去の在籍は写さない（分岐前の事実は実データの側に
1つだけある）。加入年は世界の開幕年にする。球場は共有で、本拠地は同じ球場を指す。

**分岐した後は同期しない。** 同じ事実を二重に持つのではなく、別の世界の初期状態として
独立させるため。

名簿は `Team.add_player()` で作る。背番号の重複は集約が自分で弾くので、
ここで検査を書き直さない。外国人枠は検査しない（分岐元がすでに超えていても、
それを写すのが「スナップショット」であって、枠の是正は別の仕事）。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

from ..entities import League, Player, Team
from ..value_objects import Profile

# 生年月日の推定（乱数なし）。入団年があれば入団年の22年前、無ければ開幕年の27年前の7月1日
ESTIMATED_DEBUT_AGE = 22
ESTIMATED_AGE_AT_START = 27


def estimated_birth_date(profile: Profile, start_year: int) -> date:
    """生年月日の無い選手の推定。**年齢の規則（成長・衰え・引退）が年齢を引けるようにするため**に補う。

    入団年があれば「入団年 − 22 年」、無ければ「開幕年 − 27 年」の7月1日。乱数を使わないので、
    同じプロフィールなら何度推定しても同じ。
    """
    if profile.debut_year is not None:
        return date(profile.debut_year - ESTIMATED_DEBUT_AGE, 7, 1)
    return date(start_year - ESTIMATED_AGE_AT_START, 7, 1)


def with_birth_date(profile: Profile, start_year: int) -> Profile:
    """生年月日が空なら推定で補ったプロフィール。あればそのまま。"""
    if profile.birth_date is not None:
        return profile
    return replace(profile, birth_date=estimated_birth_date(profile, start_year))


def fork_league(source: League, *, display_order: int) -> League:
    """リーグを写す。id は付けない（保存するときに世界の中で採番される）。"""
    return League(
        name=source.name,
        foreign_player_roster_limit=source.foreign_player_roster_limit,
        foreign_player_game_limit=source.foreign_player_game_limit,
        display_order=display_order,
    )


def fork_team(source: Team, *, league_id: int) -> Team:
    """名簿の空の球団を写す。選手は `fork_roster()` で足す。"""
    return Team(
        name=source.name,
        league_id=league_id,
        home_stadium_id=source.home_stadium_id,
        display_order=source.display_order,
    )


def fork_roster(source: Team, target: Team, *, start_year: int) -> list[tuple[Player, Player]]:
    """分岐元の現在の選手を、保存済みの空の球団へ加入させる。

    現在の在籍（退団年が空）のある選手だけを、開幕年に加入した形で写す。
    退団済みの選手・過去の在籍・主将の在任歴は写さない。生年月日が空の選手は推定で補う
    （`estimated_birth_date`。成長・衰え・引退が年齢を引けるように。分岐元は書き換えない）。

    (分岐元の選手, 写した選手) の組を返す。写した選手の id は保存で決まるので、
    能力の推定のように「元の成績」と「写した先」を結びつけたい呼び出し側がこの組を使う。
    """
    copies: list[tuple[Player, Player]] = []
    for player in source.active_players:
        stint = source.current_stint(player)
        if stint is None:
            continue
        copy = target.add_player(player.name, stint.number, player.position, from_year=start_year)
        copy.profile = with_birth_date(player.profile, start_year)
        copies.append((player, copy))
    return copies
