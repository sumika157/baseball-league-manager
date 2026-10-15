"""リーグの基準値（FIP 定数・OPS+/ERA+ の平均）と、その覚え。

複数の画面のユースケースが同じ基準値を引くため、サービスのクラスから出してある。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..domain import services as domain_services
from .dto import ActivePlayerStats
from .player_stats_view import stats_player
from .queries import PlayerStatsQuery


@dataclass(frozen=True)
class LeagueContext:
    """リーグ全体の成績から決まる、個々の成績を評価するための基準値。

    FIP の定数と、OPS+・ERA+ の基準になるリーグ平均。いずれもリーグ全体を
    見ないと決まらない値で、1回の要求の中で何度も参照されるため
    リーグ単位でまとめて求めて覚えておく。
    """

    fip_constant: float
    average_ops: float
    average_era: float


EMPTY_LEAGUE_CONTEXT = LeagueContext(fip_constant=0.0, average_ops=0.0, average_era=0.0)


class LeagueContexts:
    """リーグの基準値を求め、インスタンスごとに覚えておく。

    覚えは1つのサービスのインスタンスの寿命（通常は1回の要求）の中だけで効く。
    成績は試合の保存で変わるので、インスタンスをまたいで持ち越さない。
    """

    def __init__(self, player_stats: PlayerStatsQuery) -> None:
        self._player_stats = player_stats
        self._memo: dict[tuple[int, int | None], LeagueContext] = {}

    def get(
        self, league_id: int | None, year: int | None = None, *, rows: list[ActivePlayerStats] | None = None
    ) -> LeagueContext:
        """そのリーグの基準値。リーグ全体の成績から決まるので、リーグを
        知らなければ求められない（未登板と同じ、全て0の基準値を返す）。

        `year` を渡すと、その年の成績から求める（ペナントの「今季」の表の基準）。省くと在籍中の選手の通算。
        リーグの選手の行を既に読んでいれば `rows` で渡す（読み直さない）。
        """
        if league_id is None:
            return EMPTY_LEAGUE_CONTEXT
        key = (league_id, year)
        if key not in self._memo:
            # 基準はそのリーグの中だけで決まる。他リーグのチームは読まない。集約は組み立てず、SQL の集計で読む
            if rows is None:
                rows = self._player_stats.list_roster(year=year, league_id=league_id)
            members = [stats_player(row) for row in rows]
            batting = domain_services.team_batting(members)
            pitching = domain_services.team_pitching(members)
            self._memo[key] = LeagueContext(
                fip_constant=domain_services.fip_constant(pitching),
                average_ops=batting.ops,
                average_era=pitching.earned_run_average,
            )
        return self._memo[key]
