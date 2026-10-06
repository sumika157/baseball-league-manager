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
from ..domain.services import ColorAxis, ColorCategory, FielderGroup, MoveJudgement, MoveKind, PitcherRole, StintSpan
from ..domain.value_objects import (
    ContractStatus,
    FieldingPosition,
    Handedness,
    Position,
    Profile,
    Season,
    StintPeriod,
    fa_destinations,
    fa_origin,
)
from .dto import (
    AgeBandRow,
    AnalysisRosterRow,
    AnalysisTab,
    AnalysisTeamOption,
    ColorLegendItem,
    ColorOption,
    DeclarationFact,
    DepthCell,
    DepthPlayer,
    DepthRow,
    DepthTable,
    FaRow,
    FielderUsage,
    MoveRow,
    MoveStintRow,
    PitcherUsage,
    PlayerContractStint,
    ServiceHistory,
    TeamAnalysis,
    TeamAnalysisFacts,
    UsageBox,
    UsagePlayer,
)
from .queries import TeamAnalysisQuery, TeamListQuery

DEPTH_TAB = "depth"
USAGE_TAB = "usage"
FA_TAB = "fa"
TABS = [
    AnalysisTab(key=DEPTH_TAB, label="デプス表"),
    AnalysisTab(key=USAGE_TAB, label="起用マップ"),
    AnalysisTab(key=FA_TAB, label="FA取得"),
]

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

    def get_analysis(
        self, team_id: int, *, year: int | None = None, tab: str | None = None, color: str | None = None
    ) -> TeamAnalysis:
        """球団×年度の戦力分析。

        年が不正（試合の無い年）なら最新の年に落とす。試合が1つも無いチームは今日の年。
        タブが不正ならデプス表、色分けの軸が不正なら左右。チームが無ければ `TeamNotFound`。
        """
        summaries = self._team_list_query.list_summaries()
        team = next((summary for summary in summaries if summary.id == team_id), None)
        if team is None:
            raise TeamNotFound("チームが見つかりません。")

        years = self._analysis_query.list_years(team_id)
        chosen = year if year in years else (years[0] if years else date.today().year)
        facts = self._analysis_query.load(team_id, chosen)

        axis = ColorAxis.parse(color)
        chosen_tab = tab if tab in {t.key for t in TABS} else DEPTH_TAB
        pitchers, fielders, pitcher_ages, fielder_ages = self._depth(facts, Season(chosen), axis)
        joiners, leavers = self._moves(facts, chosen, axis)
        usage_boxes = self._usage_boxes(facts, chosen, axis)
        # FA 取得タブのときだけ、キャリア通算の出場を読む（ほかのタブのクエリ数を増やさない）
        history = (
            self._analysis_query.load_service_history([row.player_id for row in facts.roster], chosen)
            if chosen_tab == FA_TAB and facts.roster
            else None
        )
        fa_rows = self._fa_rows(facts, history, chosen, axis) if history is not None else []
        # 凡例の人数は、いまのタブで色が付いて並んでいる選手。デプス表のタブは入退団の表も同じページに出るので含める
        if chosen_tab == FA_TAB:
            shown = [row.tone for row in fa_rows]
        elif chosen_tab == USAGE_TAB:
            shown = [p.tone for box in usage_boxes for p in box.players]
        else:
            shown = [
                *(
                    p.tone
                    for table in (pitchers, fielders)
                    for row in table.rows
                    for cell in row.cells
                    for p in cell.players
                ),
                *(row.tone for row in (*joiners, *leavers)),
            ]
        return TeamAnalysis(
            team_id=team.id,
            team_name=team.name,
            year=chosen,
            years=years,
            tab=chosen_tab,
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
            usage_boxes=usage_boxes,
            color=axis.value,
            color_options=[ColorOption(key=a.value, label=a.label) for a in ColorAxis],
            legend=_legend(axis, shown),
            joiners=joiners,
            leavers=leavers,
            registered_limit=facts.registered_limit,
            fa_rows=fa_rows,
            records_from_year=history.records_from_year if history is not None else None,
        )

    @staticmethod
    def _fa_rows(facts: TeamAnalysisFacts, history: ServiceHistory | None, year: int, axis: ColorAxis) -> list[FaRow]:
        """FA 取得タブの行。表示年の在籍選手ごとに、年ごとの登録日数（推定）からFA権の見込みを出す。

        登録日数は年ごとの初出場〜最終出場の日数で、育成だった年と試合の無い年は0日。
        FA 宣言（F1）は表示年までのものを渡し、最後の宣言の翌年から数え直す（再取得）。
        """
        if history is None:
            return []
        stints: dict[int, list[PlayerContractStint]] = defaultdict(list)
        for stint in history.stints:
            stints[stint.player_id].append(stint)
        declared_years: dict[int, list[int]] = defaultdict(list)
        for declaration in history.declarations:
            declared_years[declaration.player_id].append(declaration.year)
        days: dict[int, dict[int, int]] = defaultdict(dict)
        for span in history.spans:
            days[span.player_id][span.year] = domain_services.estimated_service_days(span.first_on, span.last_on)
        # 記録が無ければ数えられる年が無いので、表示年を始まりとして「記録より前」を作らない
        records_from = history.records_from_year if history.records_from_year is not None else year

        rows: list[tuple[tuple[int, int, int], FaRow]] = []
        for player in facts.roster:
            by_year = days[player.player_id]
            # 育成だった年かの判定だけをここで行い（在籍の契約から）、数えない扱いはドメインに任せる
            developmental_years = {y for y in by_year if _is_developmental_year(stints[player.player_id], y)}
            education = domain_services.EducationPath.of(
                Profile(
                    high_school=player.high_school,
                    university=player.university,
                    corporate_team=player.corporate_team,
                )
            )
            outlook = domain_services.fa_outlook(
                education=education,
                debut_year=player.debut_year,
                days_by_year=by_year,
                through_year=year,
                records_from_year=records_from,
                declared_years=declared_years[player.player_id],
                developmental_years=developmental_years,
            )
            number = player.contract.number_in(year)
            row = FaRow(
                player_id=player.player_id,
                name=player.name,
                number=number,
                education_label=education.value,
                debut_year=player.debut_year,
                seasons=outlook.service.seasons if outlook.service else None,
                remainder_days=outlook.service.remainder_days if outlook.service else 0,
                includes_estimate=outlook.includes_estimate,
                domestic_label=outlook.domestic.label,
                overseas_label=outlook.overseas.label,
                is_domestic_acquired=outlook.domestic.kind is domain_services.FaStatusKind.ACQUIRED,
                is_overseas_acquired=outlook.overseas.kind is domain_services.FaStatusKind.ACQUIRED,
                # FA 取得の表には守備位置が無いので、本職の軸では対象外になる
                tone=_tone(
                    axis,
                    registered=player.position,
                    throws=player.throws,
                    bats=player.bats,
                    fielding_position=None,
                ),
                is_developmental=player.contract.contract_in(year) is ContractStatus.DEVELOPMENTAL,
            )
            rows.append(((*domain_services.fa_order(outlook), number), row))
        rows.sort(key=lambda entry: entry[0])
        return [row for _, row in rows]

    @staticmethod
    def _moves(facts: TeamAnalysisFacts, year: int, axis: ColorAxis) -> tuple[list[MoveRow], list[MoveRow]]:
        """その年の加入と退団。在籍から区分を導き、区分→背番号の順に並べる。"""
        spans: dict[int, list[StintSpan]] = defaultdict(list)
        team_names: dict[int, str] = {}
        for related in facts.related_stints:
            spans[related.player_id].append(
                StintSpan(related.stint_id, related.team_id, related.from_year, related.to_year)
            )
            team_names[related.team_id] = related.team_name
        # 選手ごとの在籍の期間（経路つき）と FA 宣言。結果（残留・移籍）は保存せず、ドメインの規則で導く
        periods: dict[int, list[StintPeriod]] = defaultdict(list)
        for related in facts.related_stints:
            periods[related.player_id].append(
                StintPeriod(related.team_id, related.from_year, related.to_year, related.acquired_via)
            )
        declared: dict[int, list[DeclarationFact]] = defaultdict(list)
        for declaration in facts.declarations:
            declared[declaration.player_id].append(declaration)

        def fa_label(move: MoveStintRow) -> str:
            """この退団が、FA を宣言した球団の在籍（起点）の終わりで、移籍先があれば「国内FA」「海外FA」。

            宣言の年は退団年とは限らない（翌年に移籍先の在籍が始まる）ので、宣言ごとに起点を見る。
            判定はドメインの `fa_origin`・`fa_destinations` で、入退団の区分とは別に宣言から導く。
            """
            own = StintPeriod(move.team_id, move.from_year, move.to_year, move.acquired_via)
            player_periods = periods.get(move.player_id, [])
            for declaration in sorted(declared.get(move.player_id, []), key=lambda d: -d.year):
                if fa_origin(declaration.year, player_periods) == own and fa_destinations(
                    declaration.year, player_periods
                ):
                    return f"{declaration.kind.label}FA"
            return ""

        def row(move: MoveStintRow, judgement: MoveJudgement, *, leaving: bool) -> MoveRow:
            other = team_names.get(judgement.other_team_id, "") if judgement.other_team_id is not None else ""
            return MoveRow(
                player_id=move.player_id,
                name=move.name,
                number=move.contract.number_in(year),
                position_label=move.position.label,
                kind_label=judgement.kind.value,
                is_developmental=move.contract.contract_in(year) is ContractStatus.DEVELOPMENTAL,
                other_team_name=other,
                acquired_via_label=move.acquired_via.label if move.acquired_via is not None and not leaving else "",
                fa_label=fa_label(move) if leaving else "",
                # 入退団の表には守備位置が無いので、本職の軸では対象外になる
                tone=_tone(
                    axis,
                    registered=move.position,
                    throws=move.throws,
                    bats=move.bats,
                    fielding_position=None,
                ),
            )

        joined: list[tuple[MoveKind, MoveRow]] = []
        left: list[tuple[MoveKind, MoveRow]] = []
        for move in facts.moves:
            own = StintSpan(move.stint_id, move.team_id, move.from_year, move.to_year)
            stints = spans.get(move.player_id, [])
            if move.from_year == year:
                judgement = domain_services.judge_join(own, stints)
                joined.append((judgement.kind, row(move, judgement, leaving=False)))
            if move.to_year == year:
                judgement = domain_services.judge_leave(own, stints)
                left.append((judgement.kind, row(move, judgement, leaving=True)))

        def ordered(entries: list[tuple[MoveKind, MoveRow]]) -> list[MoveRow]:
            entries.sort(key=lambda e: domain_services.move_order(e[0], e[1].number))
            return [moved for _, moved in entries]

        return ordered(joined), ordered(left)

    @staticmethod
    def _usage_boxes(facts: TeamAnalysisFacts, year: int, axis: ColorAxis) -> list[UsageBox]:
        """守備位置ごとの起用マップ。箱の中は先発数・出場数・背番号の順。

        投手の箱は投球明細の先発登板から数える（打撃明細に「投」が出るのは指名打者制を使わない試合だけ。
        そこから数えると二重になるので、打撃明細の「投」は使わない）。
        """
        roster = {row.player_id: row for row in facts.roster}
        counts: dict[FieldingPosition, list[UsagePlayer]] = defaultdict(list)

        def add(position: FieldingPosition, player_id: int, starts: int, games: int) -> None:
            row = roster.get(player_id)
            if row is not None and games > 0:
                tone = _tone(
                    axis,
                    registered=row.position,
                    throws=row.throws,
                    bats=row.bats,
                    fielding_position=position,
                )
                counts[position].append(
                    UsagePlayer(
                        player_id=player_id,
                        name=row.name,
                        number=row.contract.number_in(year),
                        starts=starts,
                        games=games,
                        tone=tone,
                    )
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
        facts: TeamAnalysisFacts, season: Season, axis: ColorAxis
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
            classify=lambda row: _pitcher_entry(row, pitcher_usage.get(row.player_id), age_of(row), season.year, axis),
        )

        by_player: dict[int, list[FielderUsage]] = defaultdict(list)
        for usage in facts.fielder_usage:
            by_player[usage.player_id].append(usage)
        fielder_table = _table(
            rows=fielder_rows,
            is_pitcher=False,
            labels=[group.value for group in FielderGroup],
            classify=lambda row: _fielder_entry(row, by_player.get(row.player_id, []), age_of(row), season.year, axis),
        )
        return (
            pitcher_table,
            fielder_table,
            [age_of(row) for row in pitcher_rows],
            [age_of(row) for row in fielder_rows],
        )


def _tone(
    axis: ColorAxis,
    *,
    registered: Position,
    throws: Handedness | None,
    bats: Handedness | None,
    fielding_position: FieldingPosition | None,
) -> ColorCategory:
    """選手の色分けの区分。振り分けの規則はドメインの `color_category` 1つで、ここは材料を渡すだけ。"""
    return domain_services.color_category(
        axis,
        registered=registered,
        profile=Profile(throws=throws, bats=bats),
        fielding_position=fielding_position,
    )


def _is_developmental_year(stints: list[PlayerContractStint], year: int) -> bool:
    """その年に在籍があり、そのすべてが育成か。在籍が無い年は育成とは言えないので False。"""
    covering = [s for s in stints if s.from_year <= year and (s.to_year is None or year <= s.to_year)]
    return bool(covering) and all(s.contract.contract_in(year) is ContractStatus.DEVELOPMENTAL for s in covering)


def _legend(axis: ColorAxis, tones: list[ColorCategory]) -> list[ColorLegendItem]:
    """凡例。軸の区分を並べ、画面に出ている選手の人数を添える。対象外は該当者がいるときだけ。"""
    items = [
        ColorLegendItem(category=c, count=sum(1 for t in tones if t.key == c.key))
        for c in domain_services.color_categories(axis)
    ]
    neutral = domain_services.NEUTRAL_CATEGORY
    count = sum(1 for t in tones if t.key == neutral.key)
    if count:
        items.append(ColorLegendItem(category=neutral, count=count))
    return items


def _pitcher_entry(
    row: AnalysisRosterRow, usage: PitcherUsage | None, age: int | None, year: int, axis: ColorAxis
) -> tuple[str, DepthPlayer]:
    games, starts = (usage.games, usage.starts) if usage else (0, 0)
    player = DepthPlayer(
        player_id=row.player_id,
        name=row.name,
        number=row.contract.number_in(year),
        age=age,
        is_foreign_player=row.is_foreign_player,
        is_developmental=row.contract.contract_in(year) is ContractStatus.DEVELOPMENTAL,
        games=games,
        starts=starts,
        tone=_tone(
            axis,
            registered=row.position,
            throws=row.throws,
            bats=row.bats,
            fielding_position=FieldingPosition.PITCHER if games > 0 else None,
        ),
    )
    return PitcherRole.of(games, starts).value, player


def _fielder_entry(
    row: AnalysisRosterRow, usages: list[FielderUsage], age: int | None, year: int, axis: ColorAxis
) -> tuple[str, DepthPlayer]:
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
        number=row.contract.number_in(year),
        age=age,
        is_foreign_player=row.is_foreign_player,
        is_developmental=row.contract.contract_in(year) is ContractStatus.DEVELOPMENTAL,
        games=games,
        starts=starts,
        position_label=label,
        tone=_tone(
            axis,
            registered=row.position,
            throws=row.throws,
            bats=row.bats,
            fielding_position=position,
        ),
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
