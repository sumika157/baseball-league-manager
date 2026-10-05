"""戦力分析（球団×年度の編成）のアプリケーションサービス。

`TeamApplicationService` は既に約50メソッドを抱えているため、対象ごとに別のサービスにした。
集計結果は保存しない。参照クエリが在籍と試合の明細から事実を集め、ここで
ドメインの規則（`domain/services/roster_analysis.py`）に当てて区分けする。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import date

from ..domain import services as domain_services
from ..domain.exceptions import TeamNotFound
from ..domain.services import FielderGroup, PitcherRole
from ..domain.value_objects import FieldingPosition, Handedness, Profile, Season
from .dto import (
    AgeBandRow,
    AnalysisRosterRow,
    AnalysisTab,
    AnalysisTeamOption,
    DepthCell,
    DepthPlayer,
    DepthRow,
    DepthTable,
    FielderUsage,
    PitcherUsage,
    TeamAnalysis,
    TeamAnalysisFacts,
    UsageBox,
    UsagePlayer,
)
from .queries import TeamAnalysisQuery, TeamListQuery

DEPTH_TAB = "depth"
USAGE_TAB = "usage"
# 成績一覧・FA を足すときはここに並べる
TABS = [AnalysisTab(key=DEPTH_TAB, label="デプス表"), AnalysisTab(key=USAGE_TAB, label="起用マップ")]

# 起用マップの箱をダイヤモンドのどこに置くか（CSS の `usage-box-<キー>`）。表示の関心なのでここに持つ。
# 守備位置の一覧そのものはドメイン（`usage_map_positions`）が出典で、足りない位置があればテストが落ちる
USAGE_AREAS: dict[FieldingPosition, str] = {
    FieldingPosition.PITCHER: "pitcher",
    FieldingPosition.CATCHER: "catcher",
    FieldingPosition.FIRST_BASE: "first",
    FieldingPosition.SECOND_BASE: "second",
    FieldingPosition.THIRD_BASE: "third",
    FieldingPosition.SHORTSTOP: "short",
    FieldingPosition.LEFT_FIELD: "left",
    FieldingPosition.CENTER_FIELD: "center",
    FieldingPosition.RIGHT_FIELD: "right",
    FieldingPosition.DESIGNATED_HITTER: "dh",
}


class TeamAnalysisService:
    def __init__(self, team_list_query: TeamListQuery, analysis_query: TeamAnalysisQuery) -> None:
        self._team_list_query = team_list_query
        self._analysis_query = analysis_query

    def first_team_id(self) -> int | None:
        """チームの表示順で先頭のチーム。1つも無ければ None。"""
        summaries = self._team_list_query.list_summaries()
        return summaries[0].id if summaries else None

    def has_team(self, team_id: int) -> bool:
        return any(summary.id == team_id for summary in self._team_list_query.list_summaries())

    def get_analysis(self, team_id: int, *, year: int | None = None, tab: str | None = None) -> TeamAnalysis:
        """球団×年度の戦力分析。

        年が不正（試合の無い年）なら最新の年に落とす。試合が1つも無いチームは今日の年。
        タブが不正ならデプス表。チームが無ければ `TeamNotFound`。
        """
        summaries = self._team_list_query.list_summaries()
        team = next((summary for summary in summaries if summary.id == team_id), None)
        if team is None:
            raise TeamNotFound("チームが見つかりません。")

        years = self._analysis_query.list_years(team_id)
        chosen = year if year in years else (years[0] if years else date.today().year)
        facts = self._analysis_query.load(team_id, chosen)

        pitchers, fielders, pitcher_ages, fielder_ages = self._depth(facts, Season(chosen))
        return TeamAnalysis(
            team_id=team.id,
            team_name=team.name,
            year=chosen,
            years=years,
            tab=tab if tab in {t.key for t in TABS} else DEPTH_TAB,
            tabs=TABS,
            teams=[AnalysisTeamOption(id=s.id, name=s.name, league_name=s.league_name) for s in summaries],
            pitchers=pitchers,
            fielders=fielders,
            age_rows=[
                AgeBandRow(band=row.band, pitchers=row.pitchers, fielders=row.fielders, total=row.total)
                for row in domain_services.age_distribution(pitcher_ages, fielder_ages)
            ],
            average_age_pitchers=domain_services.average_age(pitcher_ages),
            average_age_fielders=domain_services.average_age(fielder_ages),
            average_age_all=domain_services.average_age([*pitcher_ages, *fielder_ages]),
            usage_boxes=self._usage_boxes(facts),
        )

    @staticmethod
    def _usage_boxes(facts: TeamAnalysisFacts) -> list[UsageBox]:
        """守備位置ごとの起用マップ。箱の中は先発数・出場数・背番号の順。

        投手の箱は投球明細の先発登板から数える（打撃明細に「投」が出るのは指名打者制を使わない試合だけ。
        そこから数えると二重になるので、打撃明細の「投」は使わない）。
        """
        roster = {row.player_id: row for row in facts.roster}
        counts: dict[FieldingPosition, list[UsagePlayer]] = defaultdict(list)

        def add(position: FieldingPosition, player_id: int, starts: int, games: int) -> None:
            row = roster.get(player_id)
            if row is not None and games > 0:
                counts[position].append(
                    UsagePlayer(player_id=player_id, name=row.name, number=row.number, starts=starts, games=games)
                )

        for pitching in facts.pitcher_usage:
            add(FieldingPosition.PITCHER, pitching.player_id, pitching.starts, pitching.games)
        for usage in facts.fielder_usage:
            if usage.position is not None and usage.position is not FieldingPosition.PITCHER:
                add(usage.position, usage.player_id, usage.starts, usage.games)

        boxes: list[UsageBox] = []
        for position in domain_services.usage_map_positions():
            players = sorted(
                counts.get(position, []), key=lambda p: domain_services.depth_order(p.starts, p.games, p.number)
            )
            shown = domain_services.usage_visible_count(len(players))
            boxes.append(
                UsageBox(
                    label=position.label,
                    area=USAGE_AREAS[position],
                    players=players[:shown],
                    hidden_count=len(players) - shown,
                )
            )
        return boxes

    @staticmethod
    def _depth(
        facts: TeamAnalysisFacts, season: Season
    ) -> tuple[DepthTable, DepthTable, list[int | None], list[int | None]]:
        """投手側・野手側のデプス表と、それぞれの年齢の並び。"""
        # 投手かどうかは登録位置で決める。登録が投手でない選手が投げても野手側に置く
        roster = {row.player_id: row for row in facts.roster}
        pitcher_rows = [row for row in roster.values() if row.position.is_pitcher]
        fielder_rows = [row for row in roster.values() if not row.position.is_pitcher]

        def age_of(row: AnalysisRosterRow) -> int | None:
            return Profile(birth_date=row.birth_date).age_in(season)

        pitcher_usage = {usage.player_id: usage for usage in facts.pitcher_usage}
        pitcher_table = _table(
            rows=pitcher_rows,
            is_pitcher=True,
            labels=[role.value for role in PitcherRole],
            classify=lambda row: _pitcher_entry(row, pitcher_usage.get(row.player_id), age_of(row)),
        )

        by_player: dict[int, list[FielderUsage]] = defaultdict(list)
        for usage in facts.fielder_usage:
            by_player[usage.player_id].append(usage)
        fielder_table = _table(
            rows=fielder_rows,
            is_pitcher=False,
            labels=[group.value for group in FielderGroup],
            classify=lambda row: _fielder_entry(row, by_player.get(row.player_id, []), age_of(row)),
        )
        return (
            pitcher_table,
            fielder_table,
            [age_of(row) for row in pitcher_rows],
            [age_of(row) for row in fielder_rows],
        )


def _pitcher_entry(row: AnalysisRosterRow, usage: PitcherUsage | None, age: int | None) -> tuple[str, DepthPlayer]:
    games, starts = (usage.games, usage.starts) if usage else (0, 0)
    player = DepthPlayer(
        player_id=row.player_id,
        name=row.name,
        number=row.number,
        age=age,
        is_foreign_player=row.is_foreign_player,
        games=games,
        starts=starts,
    )
    return PitcherRole.of(games, starts).value, player


def _fielder_entry(row: AnalysisRosterRow, usages: list[FielderUsage], age: int | None) -> tuple[str, DepthPlayer]:
    starts_by: dict[FieldingPosition, int] = {}
    games_by: dict[FieldingPosition, int] = {}
    for usage in usages:
        if usage.position is not None:
            starts_by[usage.position] = starts_by.get(usage.position, 0) + usage.starts
            games_by[usage.position] = games_by.get(usage.position, 0) + usage.games
    position = domain_services.primary_position(starts_by, games_by)

    if position is not None:
        games, starts, label = games_by[position], starts_by.get(position, 0), position.label
    else:
        # 守備に就いていない。代打・代走などで出ていれば、その出場数だけを見せる
        games, starts, label = sum(u.games for u in usages), sum(u.starts for u in usages), ""
    player = DepthPlayer(
        player_id=row.player_id,
        name=row.name,
        number=row.number,
        age=age,
        is_foreign_player=row.is_foreign_player,
        games=games,
        starts=starts,
        position_label=label,
    )
    return FielderGroup.of(position).value, player


def _table(
    *,
    rows: list[AnalysisRosterRow],
    is_pitcher: bool,
    labels: list[str],
    classify: Callable[[AnalysisRosterRow], tuple[str, DepthPlayer]],
) -> DepthTable:
    """区分（行）×左右（列）に選手を振り分ける。区分の中は先発数・出場数・背番号の順。"""
    hands: dict[int, Handedness | None] = {
        row.player_id: domain_services.hand_of(Profile(throws=row.throws, bats=row.bats), is_pitcher=is_pitcher)
        for row in rows
    }
    columns = domain_services.hand_columns(hands.values(), is_pitcher=is_pitcher)

    grid: dict[tuple[str, Handedness | None], list[DepthPlayer]] = defaultdict(list)
    for row in rows:
        label, player = classify(row)
        grid[(label, hands[row.player_id])].append(player)

    return DepthTable(
        columns=[domain_services.hand_label(hand, is_pitcher=is_pitcher) for hand in columns],
        rows=[
            DepthRow(
                label=label,
                cells=[
                    DepthCell(
                        hand_label=domain_services.hand_label(hand, is_pitcher=is_pitcher),
                        players=sorted(
                            grid.get((label, hand), []),
                            key=lambda p: domain_services.depth_order(p.starts, p.games, p.number),
                        ),
                    )
                    for hand in columns
                ],
            )
            for label in labels
        ],
    )
