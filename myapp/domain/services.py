"""ドメインサービス。

単一のエンティティには属さないが、業務ルールであるものを置く。

ランキングは「誰を対象に含めるか（規定に達しているか）」「何を良しとするか」という
野球のルールそのものなので、表示の都合ではなくドメインの関心事として扱う。
ここに置くことで、画面を持たなくても順位づけを単体テストできる。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .entities import Game, Player, Team
from .value_objects import BattingLine, PitchingLine, Season, TeamRecord


@dataclass(frozen=True)
class RankedPlayer:
    """順位づけされた選手と、その順位の根拠となった値。"""

    rank: int
    player: Player
    value: float


def _rank(entries: list[tuple[Player, float]], limit: int | None) -> list[RankedPlayer]:
    """値の並び順が確定した一覧に順位を振る。同値は同順位とする。"""
    ranked: list[RankedPlayer] = []
    previous_value = None
    for index, (player, value) in enumerate(entries, start=1):
        if previous_value is not None and value == previous_value:
            rank = ranked[-1].rank  # 同率
        else:
            rank = index
        ranked.append(RankedPlayer(rank=rank, player=player, value=value))
        previous_value = value

    return ranked[:limit] if limit is not None else ranked


# 規定打席・規定投球回。日本プロ野球の規則にならう。
#   打者: チームの試合数 × 3.1 打席
#   投手: チームの試合数 × 1.0 投球回
QUALIFYING_PLATE_APPEARANCES_PER_GAME = 3.1
QUALIFYING_INNINGS_PER_GAME = 1.0


def required_plate_appearances(team_games: int) -> int:
    """規定打席。端数は切り上げる。"""
    return math.ceil(team_games * QUALIFYING_PLATE_APPEARANCES_PER_GAME)


def required_outs(team_games: int) -> int:
    """規定投球回をアウト数で表したもの。"""
    return math.ceil(team_games * QUALIFYING_INNINGS_PER_GAME * 3)


def qualified_batters(
    players: list[Player],
    *,
    team_games: dict[int, int] | None = None,
    minimum_at_bats: int = 1,
) -> list[Player]:
    """打撃タイトルの対象となる選手。

    team_games（選手id → その選手のチームの試合数）を渡すと規定打席で絞る。
    渡さない場合は最低打数だけで絞る。規定で絞らないと、1打数1安打の選手が
    打率10割で首位に立ってしまう。
    """
    batters = [p for p in players if not p.is_pitcher]

    if team_games is None:
        return [p for p in batters if p.batting.at_bats >= minimum_at_bats]

    return [
        p
        for p in batters
        if p.batting.plate_appearances >= required_plate_appearances(team_games.get(p.id or 0, 0))
        and p.batting.at_bats > 0
    ]


def qualified_pitchers(players: list[Player], *, team_games: dict[int, int] | None = None) -> list[Player]:
    """投球タイトルの対象となる投手。

    team_games を渡すと規定投球回で絞る。渡さない場合は未登板だけを除く。
    """
    pitchers = [p for p in players if p.is_pitcher and p.pitching.innings.outs > 0]

    if team_games is None:
        return pitchers

    return [p for p in pitchers if p.pitching.innings.outs >= required_outs(team_games.get(p.id or 0, 0))]


def leaders_by_ops(
    players: list[Player],
    *,
    limit: int | None = 5,
    team_games: dict[int, int] | None = None,
    minimum_at_bats: int = 1,
) -> list[RankedPlayer]:
    """OPS の高い順。規定打席に達した選手のみ。"""
    entries = [
        (p, p.batting.ops) for p in qualified_batters(players, team_games=team_games, minimum_at_bats=minimum_at_bats)
    ]
    entries.sort(key=lambda e: (-e[1], e[0].name))
    return _rank(entries, limit)


def leaders_by_batting_average(
    players: list[Player],
    *,
    limit: int | None = 5,
    team_games: dict[int, int] | None = None,
    minimum_at_bats: int = 1,
) -> list[RankedPlayer]:
    """打率の高い順。規定打席に達した選手のみ。"""
    entries = [
        (p, p.batting.batting_average)
        for p in qualified_batters(players, team_games=team_games, minimum_at_bats=minimum_at_bats)
    ]
    entries.sort(key=lambda e: (-e[1], e[0].name))
    return _rank(entries, limit)


def leaders_by_home_runs(players: list[Player], *, limit: int | None = 5) -> list[RankedPlayer]:
    """本塁打の多い順。本数そのものが記録なので打数の規定は設けない。"""
    entries = [(p, float(p.batting.home_runs)) for p in players if not p.is_pitcher and p.batting.home_runs > 0]
    entries.sort(key=lambda e: (-e[1], e[0].name))
    return _rank(entries, limit)


def leaders_by_runs_batted_in(players: list[Player], *, limit: int | None = 5) -> list[RankedPlayer]:
    """打点の多い順。本塁打と同じく本数そのものが記録なので規定を設けない。"""
    entries = [
        (p, float(p.batting.runs_batted_in)) for p in players if not p.is_pitcher and p.batting.runs_batted_in > 0
    ]
    entries.sort(key=lambda e: (-e[1], e[0].name))
    return _rank(entries, limit)


def leaders_by_era(
    players: list[Player],
    *,
    limit: int | None = 5,
    team_games: dict[int, int] | None = None,
) -> list[RankedPlayer]:
    """防御率の低い順。規定投球回に達した投手のみ。"""
    entries = [(p, p.pitching.earned_run_average) for p in qualified_pitchers(players, team_games=team_games)]
    entries.sort(key=lambda e: (e[1], e[0].name))
    return _rank(entries, limit)


def leaders_by_strikeouts(players: list[Player], *, limit: int | None = 5) -> list[RankedPlayer]:
    """奪三振の多い順。"""
    entries = [(p, float(p.pitching.strikeouts)) for p in players if p.is_pitcher and p.pitching.strikeouts > 0]
    entries.sort(key=lambda e: (-e[1], e[0].name))
    return _rank(entries, limit)


# ------------------------------------------------------------------
# 並べ替え
# ------------------------------------------------------------------

# 「何を基準に並べ替えられるか」は野球の指標そのものなので、
# 画面ではなくここに置く。既定の向き（True = 大きい順）も併せて持たせる。
# 打率や本塁打は多いほど良く、防御率や敗戦は少ないほど良い、という違いを
# 画面側で覚えずに済ませるため。
BATTER_SORT_KEYS = {
    "number": (lambda p: p.number.value, False),
    "name": (lambda p: p.name, False),
    "average": (lambda p: p.batting.batting_average, True),
    "hits": (lambda p: p.batting.hits, True),
    "doubles": (lambda p: p.batting.doubles, True),
    "triples": (lambda p: p.batting.triples, True),
    "home_runs": (lambda p: p.batting.home_runs, True),
    "rbi": (lambda p: p.batting.runs_batted_in, True),
    "walks": (lambda p: p.batting.walks, True),
    "sacrifice_flies": (lambda p: p.batting.sacrifice_flies, True),
    "obp": (lambda p: p.batting.on_base_percentage, True),
    "slg": (lambda p: p.batting.slugging_percentage, True),
    "ops": (lambda p: p.batting.ops, True),
    "iso": (lambda p: p.batting.isolated_power, True),
}

PITCHER_SORT_KEYS = {
    "number": (lambda p: p.number.value, False),
    "name": (lambda p: p.name, False),
    "innings": (lambda p: p.pitching.innings.outs, True),
    "era": (lambda p: p.pitching.earned_run_average, False),
    "wins": (lambda p: p.pitching.wins, True),
    "losses": (lambda p: p.pitching.losses, True),
    "saves": (lambda p: p.pitching.saves, True),
    "holds": (lambda p: p.pitching.holds, True),
    "hold_points": (lambda p: p.pitching.hold_points, True),
    "starts": (lambda p: p.pitching.starts, True),
    "strikeouts": (lambda p: p.pitching.strikeouts, True),
    "whip": (lambda p: p.pitching.whip, False),
    "k9": (lambda p: p.pitching.strikeouts_per_nine, True),
    "bb9": (lambda p: p.pitching.walks_per_nine, False),
    # FIP はリーグ共通の定数を足して仕上げる指標なので、素点で並べても
    # 順序は変わらない（全員に同じ値が足されるだけ）。定数を持たない
    # 並べ替えの場面でも、素点をそのまま比較に使える
    "fip": (lambda p: p.pitching.fip_base, False),
    "home_runs_allowed": (lambda p: p.pitching.home_runs_allowed, True),
    "hit_by_pitch_allowed": (lambda p: p.pitching.hit_by_pitch_allowed, True),
}

DEFAULT_BATTER_SORT = "ops"
DEFAULT_PITCHER_SORT = "era"


# 率で並べる指標。未登板だと 0 になり、実力と無関係に上位や下位へ寄るため、
# これらで並べるときは未登板を常に末尾へ回す。
_RATE_PITCHER_KEYS = {"era", "whip", "k9", "bb9", "fip"}


def _resolve(keys, key, descending, default_key):
    """URL 由来のキーを検証する。不正なら既定に落とす（エラーにしない）。"""
    if key not in keys:
        key = default_key
    if descending is None:
        descending = keys[key][1]
    return key, bool(descending)


def _ordered(players, getter, descending):
    """指定のキーで並べ替え、同値は背番号の小さい順で安定させる。

    sorted(..., key=(指標, 背番号), reverse=True) と書くと同値のときの
    背番号まで逆順になってしまうため、背番号で並べてから指標で並べ直す。
    Python のソートは安定なので、この順序なら背番号は常に昇順に保たれる。
    """
    ordered = sorted(players, key=lambda p: p.number.value)
    ordered.sort(key=getter, reverse=descending)
    return ordered


def sort_batters(players: list[Player], key: str | None = None, descending: bool | None = None):
    """野手を並べ替える。key が未指定・不正なら OPS の高い順。

    戻り値は (並べ替え後, 実際に使ったキー, 向き)。画面側で見出しの表示を
    合わせるため、採用されたキーと向きも返す。
    """
    key, descending = _resolve(BATTER_SORT_KEYS, key, descending, DEFAULT_BATTER_SORT)
    return _ordered(players, BATTER_SORT_KEYS[key][0], descending), key, descending


def sort_pitchers(players: list[Player], key: str | None = None, descending: bool | None = None):
    """投手を並べ替える。key が未指定・不正なら防御率の低い順。"""
    key, descending = _resolve(PITCHER_SORT_KEYS, key, descending, DEFAULT_PITCHER_SORT)
    getter = PITCHER_SORT_KEYS[key][0]

    if key in _RATE_PITCHER_KEYS:
        pitched = [p for p in players if p.pitching.innings.outs > 0]
        unpitched = [p for p in players if p.pitching.innings.outs == 0]
        ordered = _ordered(pitched, getter, descending)
        ordered += sorted(unpitched, key=lambda p: p.number.value)
        return ordered, key, descending

    return _ordered(players, getter, descending), key, descending


# ------------------------------------------------------------------
# 順位表
# ------------------------------------------------------------------


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


# ------------------------------------------------------------------
# 対戦成績
# ------------------------------------------------------------------


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


# ------------------------------------------------------------------
# 期間別成績
# ------------------------------------------------------------------


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
        played = any(e.player_id == player_id for e in game.batting) or any(
            e.player_id == player_id for e in game.pitching
        )
        if not played:
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


# ------------------------------------------------------------------
# 勝敗・セーブ・ホールド（日本プロ野球の規則）
# ------------------------------------------------------------------
#
# どの記録も「継投した時点のスコア」で決まる。最終得点だけでは
# 「3点差以内のリードで登板して抑えた」を判定できないため、
# イニングスコア（LineScore）と各投手の登板した回から導く。

# セーブは3点差以内のリードで登板して1回以上、または3回以上を投げた場合。
SAVE_LEAD_LIMIT = 3
SAVE_MINIMUM_OUTS = 3
SAVE_LONG_RELIEF_OUTS = 9
# 先発が勝利投手になるには5回以上を投げる必要がある。
STARTER_WIN_MINIMUM_OUTS = 15


@dataclass(frozen=True)
class PitchingDecisions:
    """1試合ぶんの勝敗・セーブ・ホールド。選手idの集合で表す。

    勝利・敗戦・セーブは1試合に1人までなので単独、ホールドは複数付きうる。
    """

    winner_id: int | None = None
    loser_id: int | None = None
    save_id: int | None = None
    hold_ids: frozenset[int] = frozenset()

    def wins_for(self, player_id: int) -> int:
        return 1 if player_id == self.winner_id else 0

    def losses_for(self, player_id: int) -> int:
        return 1 if player_id == self.loser_id else 0

    def saves_for(self, player_id: int) -> int:
        return 1 if player_id == self.save_id else 0

    def holds_for(self, player_id: int) -> int:
        return 1 if player_id in self.hold_ids else 0


def _staff_of(game: Game, team_ids: set[int], team_of: dict[int, int]):
    """指定チームの投手を、登板順に並べて返す。"""
    entries = [entry for entry in game.pitching if team_of.get(entry.player_id) in team_ids]
    return sorted(entries, key=lambda e: (e.appearance_order, e.entered_inning))


def _pitcher_covering(staff, inning: int):
    """その回に投げていた投手。登板した回がその回以前で最も後の投手。"""
    covering = None
    for entry in staff:
        if entry.entered_inning <= inning:
            covering = entry
    return covering or (staff[0] if staff else None)


def _lead_before(game: Game, inning: int, *, is_home: bool) -> int:
    """その回に登板する直前の、自チームから見た得点差。

    ホームの投手は表に投げるので、直前は前の回の裏を終えた時点。
    ビジターの投手は裏に投げるので、直前は同じ回の表を終えた時点。
    """
    if is_home:
        away, home = game.line_score.score_after(inning - 1, bottom=True)
    else:
        away, home = game.line_score.score_after(inning, bottom=False)
    return (home - away) if is_home else (away - home)


def _lead_after(game: Game, inning: int, *, is_home: bool) -> int:
    """その回を投げ終えた時点の、自チームから見た得点差。"""
    if is_home:
        away, home = game.line_score.score_after(inning, bottom=False)
    else:
        away, home = game.line_score.score_after(inning, bottom=True)
    return (home - away) if is_home else (away - home)


def _decisive_inning(game: Game, *, winner_is_home: bool) -> tuple[int, bool]:
    """勝ちチームが最後にリードを奪った半回。(回, 裏か) を返す。

    そこで入った点が決勝点。その時点の投手が勝利投手・敗戦投手になる。
    """
    decisive = (1, False)
    led = False
    for inning in range(1, game.line_score.innings + 1):
        for bottom in (False, True):
            away, home = game.line_score.score_after(inning, bottom=bottom)
            margin = (home - away) if winner_is_home else (away - home)
            if margin > 0 and not led:
                decisive, led = (inning, bottom), True
            elif margin <= 0:
                led = False
    return decisive


def _most_effective_reliever(staff):
    """最も内容の良い救援。長く投げて自責点が少ない順。

    先発が5回未満で勝利投手の条件を満たさない場合の受け皿。規則では記録員の
    判断だが、ここでは「自責点が少なく、長く投げた投手」を選ぶ。
    """
    relievers = [entry for entry in staff if not entry.is_starter]
    if not relievers:
        return None
    return min(
        relievers,
        key=lambda e: (e.line.earned_runs, -e.line.innings.outs, e.appearance_order),
    )


def pitching_decisions(game: Game, team_of: dict[int, int]) -> PitchingDecisions:
    """1試合の勝敗・セーブ・ホールドを日本プロ野球の規則で決める。

    team_of は 選手id → チームid。試合の明細には両チームの投手が混ざって
    入っているため、どちらのチームの投手かを外から渡してもらう
    （Game 集約はロスターを知らない）。

    イニングスコアが無い試合では判定できないので、何も付けない。
    """
    if game.line_score.is_empty or not game.pitching:
        return PitchingDecisions()

    home_staff = _staff_of(game, {game.home_team_id}, team_of)
    away_staff = _staff_of(game, {game.away_team_id}, team_of)

    holds = set()
    for staff, is_home in ((home_staff, True), (away_staff, False)):
        holds |= _holds(game, staff, is_home=is_home)

    if game.is_tie:
        # 引分では勝敗もセーブも付かない。ホールドは付く
        return PitchingDecisions(hold_ids=frozenset(holds))

    winner_is_home = game.winner_team_id == game.home_team_id
    winning_staff = home_staff if winner_is_home else away_staff
    losing_staff = away_staff if winner_is_home else home_staff

    inning, _ = _decisive_inning(game, winner_is_home=winner_is_home)

    winner = _pitcher_covering(winning_staff, inning)
    if winner is not None and winner.is_starter and (winner.line.innings.outs < STARTER_WIN_MINIMUM_OUTS):
        winner = _most_effective_reliever(winning_staff) or winner

    loser = _pitcher_covering(losing_staff, inning)
    save = _save(game, winning_staff, winner, is_home=winner_is_home)

    return PitchingDecisions(
        winner_id=winner.player_id if winner else None,
        loser_id=loser.player_id if loser else None,
        save_id=save.player_id if save else None,
        # 勝利投手にホールドは付かない
        hold_ids=frozenset(pid for pid in holds if winner is None or pid != winner.player_id),
    )


def _save(game: Game, staff, winner, *, is_home: bool):
    """セーブ。試合を締めた投手が条件を満たす場合に付く。

    完投は勝利のみでセーブは付かない。勝利投手にも付かない。
    """
    if len(staff) < 2:
        return None

    finisher = staff[-1]
    if winner is not None and finisher.player_id == winner.player_id:
        return None

    lead = _lead_before(game, finisher.entered_inning, is_home=is_home)
    if lead <= 0:
        return None

    outs = finisher.line.innings.outs
    qualifies = (lead <= SAVE_LEAD_LIMIT and outs >= SAVE_MINIMUM_OUTS) or outs >= SAVE_LONG_RELIEF_OUTS
    return finisher if qualifies else None


def _holds(game: Game, staff, *, is_home: bool) -> set[int]:
    """ホールド。セーブの条件を満たす状況で登板し、1つ以上アウトを取って
    リードを保ったまま次の投手に引き継いだ救援投手に付く。
    """
    if len(staff) < 3:
        # 先発と最後の投手だけでは、引き継いだ救援が存在しない
        return set()

    holds = set()
    for index, entry in enumerate(staff[1:-1], start=1):
        if entry.line.innings.outs < 1:
            continue
        lead = _lead_before(game, entry.entered_inning, is_home=is_home)
        if not (0 < lead <= SAVE_LEAD_LIMIT):
            continue
        # 引き継いだ時点でリードが残っているか
        last_inning = staff[index + 1].entered_inning - 1
        if _lead_after(game, max(entry.entered_inning, last_inning), is_home=is_home) > 0:
            holds.add(entry.player_id)
    return holds
