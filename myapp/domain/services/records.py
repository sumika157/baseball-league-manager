"""試合からの集計。通算成績・順位表・対戦成績・期間別成績。

チームの勝敗も選手の通算成績もテーブルには持たず、試合（Game）を
唯一の出典としてここで集計する。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..entities import Game, Player, Team
from ..value_objects import BattingLine, PitchingLine, Season, TeamRecord


@dataclass(frozen=True)
class StandingRow:
    """順位表の1行。"""

    rank: int
    team_id: int
    team_name: str
    record: TeamRecord
    games_behind: float

    @property
    def is_leader(self) -> bool:
        return self.rank == 1


def team_record(games: list[Game], team_id: int) -> TeamRecord:
    """試合の一覧からチームの勝敗を集計する。

    勝敗は試合が唯一の出典であり、手入力の値は持たない。
    渡す games は対象シーズンに絞り込んだものを想定する。
    """
    wins = losses = ties = 0
    for game in games:
        if not game.involves(team_id):
            continue
        result = game.result_for(team_id)
        if result == "win":
            wins += 1
        elif result == "loss":
            losses += 1
        else:
            ties += 1
    return TeamRecord(wins=wins, losses=losses, ties=ties)


def player_batting_total(games: list[Game], player_id: int) -> BattingLine:
    """試合の一覧から選手の通算打撃成績を集計する。"""
    return BattingLine.total(entry.line for game in games for entry in game.batting if entry.player_id == player_id)


def player_pitching_total(games: list[Game], player_id: int) -> PitchingLine:
    """試合の一覧から選手の通算投球成績を集計する。"""
    return PitchingLine.total(entry.line for game in games for entry in game.pitching if entry.player_id == player_id)


def appeared_in(game: Game, player_id: int) -> bool:
    """その試合に出場したか。打撃・投球のどちらかに記録があれば出場とみなす。

    「出場した」の判定は期間別成績（月別・年度別）の行を作るかどうかを決めるため、
    束ね方ごとに書き直さず、ここを唯一の出典にする。
    """
    return any(e.player_id == player_id for e in game.batting) or any(e.player_id == player_id for e in game.pitching)


def team_batting(players: list[Player]) -> BattingLine:
    """チームの打撃成績。所属選手の成績を合算する。

    率は合算した実数から計算し直す（選手ごとの率を平均しても正しくない）。
    投手の打席も含める。打線として何を積み上げたかを見るため。
    """
    return BattingLine.total(p.batting for p in players)


def team_pitching(players: list[Player]) -> PitchingLine:
    """チームの投球成績。登板した投手の成績を合算する。"""
    return PitchingLine.total(p.pitching for p in players)


def fip_constant(league_pitching: PitchingLine) -> float:
    """FIP をリーグの得点環境に合わせるための定数。

    リーグ全体の防御率と、リーグ全体の FIP 素点との差を取る。これを個々の
    投手の素点に足すと、リーグ平均が防御率と同じ水準に揃い、防御率と
    見比べられる値になる。投球回が無い（＝比べる相手がいない）なら 0。

    定数がリーグごとに決まるのは、本塁打の出やすさや球場が違えば同じ内容の
    投球でも失点が変わるため。固定値を使うとリーグ間の比較が歪む。
    """
    if league_pitching.innings.outs == 0:
        return 0.0
    return league_pitching.earned_run_average - league_pitching.fip_base


def seasons_of(games: list[Game]) -> list[Season]:
    """試合が登録されているシーズンを新しい順に返す。"""
    return sorted({game.season for game in games}, key=lambda s: s.year, reverse=True)


def standings(teams: list[Team], games: list[Game]) -> list[StandingRow]:
    """順位表を作る。games は対象シーズンに絞り込んだものを渡す。

    順位は勝率の高い順で決まる。勝率が同じなら同順位として扱う。
    そのシーズンに1試合も無いチームは順位表に載せない
    （0勝0敗として並べると、未実施なのか全敗なのか区別できなくなる）。
    """
    entries = []
    for team in teams:
        # 未保存（id が無い）のチームは試合を持てないので載らない
        if team.id is None or not any(g.involves(team.id) for g in games):
            continue
        entries.append((team.id, team.name, team_record(games, team.id)))

    entries.sort(key=lambda e: (-e[2].winning_percentage, -e[2].wins, e[1]))

    if not entries:
        return []

    leader_record = entries[0][2]

    rows: list[StandingRow] = []
    previous_percentage = None
    for index, (team_id, team_name, record) in enumerate(entries, start=1):
        if previous_percentage is not None and record.winning_percentage == previous_percentage:
            rank = rows[-1].rank  # 同率
        else:
            rank = index
        rows.append(
            StandingRow(
                rank=rank,
                team_id=team_id,
                team_name=team_name,
                record=record,
                games_behind=record.games_behind(leader_record),
            )
        )
        previous_percentage = record.winning_percentage

    return rows


@dataclass(frozen=True)
class MatchupRow:
    """あるチームから見た、相手ごとの対戦成績。"""

    team_id: int
    team_name: str
    # 相手チームid → そのチームとの成績
    against: dict[int, TeamRecord]
    total: TeamRecord

    def record_against(self, opponent_id: int) -> TeamRecord | None:
        """相手との成績。自分自身なら None（対戦は存在しない）。"""
        return self.against.get(opponent_id)


def head_to_head(games: list[Game], team_id: int, opponent_id: int) -> TeamRecord:
    """2チーム間の対戦成績。team_id から見た勝敗を返す。

    games は対象シーズンに絞り込んだものを渡す。
    """
    between = [g for g in games if g.involves(team_id) and g.involves(opponent_id)]
    return team_record(between, team_id)


def matchups(teams: list[Team], games: list[Game]) -> list[MatchupRow]:
    """チーム間の対戦成績の表を作る。games は対象シーズンに絞って渡す。

    並びは順位表と揃える。対戦成績は「順位がなぜその形になったか」を
    読むための表なので、順位表と別の並びにすると突き合わせられない。
    そのシーズンに1試合も無いチームは載せない（順位表と同じ規則）。
    """
    order = standings(teams, games)
    name_of = {team.id: team.name for team in teams}

    rows: list[MatchupRow] = []
    for row in order:
        against = {
            other.team_id: head_to_head(games, row.team_id, other.team_id)
            for other in order
            if other.team_id != row.team_id
        }
        rows.append(
            MatchupRow(
                team_id=row.team_id,
                team_name=name_of.get(row.team_id, row.team_name),
                against=against,
                total=row.record,
            )
        )
    return rows


@dataclass(frozen=True)
class YearlySplit:
    """ある選手の、ひとシーズンぶんの成績。

    束ね方は MonthlySplit と同じで、単位が年になるだけ。年度別成績（年ごとの
    働き）とキャリア通算（選手の積み上げ全体）を分けて見るために使う。
    """

    year: int
    appearances: int
    batting: BattingLine
    pitching: PitchingLine

    @property
    def label(self) -> str:
        return f"{self.year}年"


@dataclass(frozen=True)
class MonthlySplit:
    """ある選手の、ひと月ぶんの成績。"""

    year: int
    month: int
    appearances: int
    batting: BattingLine
    pitching: PitchingLine

    @property
    def label(self) -> str:
        return f"{self.year}年{self.month}月"


@dataclass(frozen=True)
class TeamMonthlySplit:
    """あるチームの、ひと月ぶんの成績。

    選手の MonthlySplit と同じ「年と月で束ねる」規則に従うが、束ねる対象が
    チームなので勝敗も持つ。打撃・投球はその月に出場した所属選手の合計。
    """

    year: int
    month: int
    record: TeamRecord
    batting: BattingLine
    pitching: PitchingLine

    @property
    def label(self) -> str:
        return f"{self.year}年{self.month}月"

    @property
    def games_played(self) -> int:
        return self.record.games_played


def team_monthly_splits(games: list[Game], team_id: int, member_ids: set[int]) -> list[TeamMonthlySplit]:
    """チームの成績を月ごとにまとめる。古い順。

    member_ids はそのチームの選手。試合の明細は両チームの選手が混ざって
    入っているため、自チームの選手だけに絞らないと相手の成績まで足してしまう。
    試合のあった月だけを返す（選手の月別成績と同じ規則）。
    """
    buckets: dict[tuple[int, int], list[Game]] = {}
    for game in games:
        if not game.involves(team_id):
            continue
        key = (game.played_on.year, game.played_on.month)
        buckets.setdefault(key, []).append(game)

    return [
        TeamMonthlySplit(
            year=year,
            month=month,
            record=team_record(month_games, team_id),
            batting=BattingLine.total(
                entry.line for game in month_games for entry in game.batting if entry.player_id in member_ids
            ),
            pitching=PitchingLine.total(
                entry.line for game in month_games for entry in game.pitching if entry.player_id in member_ids
            ),
        )
        for (year, month), month_games in sorted(buckets.items())
    ]


def monthly_splits(games: list[Game], player_id: int) -> list[MonthlySplit]:
    """選手の成績を月ごとにまとめる。古い順。

    束ねる単位は「年と月」の組。月だけで束ねると、別のシーズンの同じ月が
    混ざってしまう。出場した月だけを返す（記録の無い月は行を作らない。
    0 の行を並べると、休んだのか登録漏れなのか区別できなくなる）。
    """
    buckets: dict[tuple[int, int], list[Game]] = {}
    for game in games:
        if not appeared_in(game, player_id):
            continue
        key = (game.played_on.year, game.played_on.month)
        buckets.setdefault(key, []).append(game)

    return [
        MonthlySplit(
            year=year,
            month=month,
            appearances=len(month_games),
            batting=player_batting_total(month_games, player_id),
            pitching=player_pitching_total(month_games, player_id),
        )
        for (year, month), month_games in sorted(buckets.items())
    ]


def yearly_splits(games: list[Game], player_id: int) -> list[YearlySplit]:
    """選手の成績を年ごとにまとめる。古い順。

    月別（monthly_splits）と同じ規則で、束ねる単位が年になるだけ。出場した年
    だけを返す（記録の無い年は行を作らない）。デビューからの推移を追う並びなので
    古い順に返す。
    """
    buckets: dict[int, list[Game]] = {}
    for game in games:
        if not appeared_in(game, player_id):
            continue
        buckets.setdefault(game.played_on.year, []).append(game)

    return [
        YearlySplit(
            year=year,
            appearances=len(year_games),
            batting=player_batting_total(year_games, player_id),
            pitching=player_pitching_total(year_games, player_id),
        )
        for year, year_games in sorted(buckets.items())
    ]
