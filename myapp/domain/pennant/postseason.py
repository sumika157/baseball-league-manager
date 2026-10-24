"""ポストシーズン（クライマックスシリーズと日本シリーズ）の勝ち上がり。**純粋な関数**で、DB にも Django にも触れない。

勝ち上がりは保存しない。リーグの最終順位と、その年のポストシーズンの試合の結果から**毎回導く**
（いまのステージ・次の対戦・どのシリーズの第何戦かは持たない。`season.py` の「進行の状態は持たない」と同じ）。
同じ年の勝ち上がりで同じ2球団が当たるのは1回だけなので、シリーズは2球団から、第何戦かは日付の順から決まる。

規則は NPB の現行の規定（2026年）を 1〜8リーグに写したもの（出典と一般化の理由は Issue #39 と
`docs/design/postseason.md`）:

- **CS**（方式 `NPB` だけ）: 各リーグの上位 `min(3, 球団数)`。ファーストステージ（2位 vs 3位、3試合・先に2勝・
  全試合2位のホーム）→ ファイナルステージ（1位 vs FS の勝者、6試合・1位に1勝のアドバンテージ・先に4勝・
  全試合1位のホーム。相手が1位から10ゲーム差以上か勝率5割未満なら7試合・2勝のアドバンテージ・先に5勝）。
  2球団のリーグはファイナルだけ、1球団のリーグは CS なしでその球団が代表。延長は12回まで（引分は再試合しない）。
- **打ち切り**: 上位の勝利数（アドバンテージ込み）が「下位の勝利数＋残り試合数」以上になった時点で上位の勝ち。
  試合数を使い切って勝利数が同じなら上位。下位も、残りを全部上位が勝っても上回れない時点で決着する
  （残りの試合で結果が変わらない。NPB の条文は上位の側だけを書いている）。
- **日本シリーズ**: リーグの代表（CS の勝者。方式 `CHAMPIONS` は各リーグの1位）が2つなら1シリーズ。
  3つ以上は勝率の順に種を付けた勝ち抜きのトーナメント（2の累乗に満たない分は上位の種がシード。
  勝率が並べばリーグの優先順で先）。各回とも先に4勝・2-3-2 の本拠地・第7戦までは12回で引分・
  第8戦以降は延長の制限なし。第1・2・6・7・8戦の本拠地は、2球団のうちリーグの優先順（`world.league_priority`）で
  先のリーグの球団。第9戦からは移動日を1日置いて本拠地を交互にする。
- **日付**: 雨天の中止が無いので予備日は置かない。最初の回は最後の公式戦の `DAYS_BEFORE_POSTSEASON` 日後に始め、
  CS は連日、ファイナルは FS の予定の最終日の `DAYS_BETWEEN_CS_STAGES` 日後、日本シリーズの各回は前の回の
  予定の最終日と実際の最後の試合の遅い方の `DAYS_BEFORE_SERIES_ROUND` 日後に始める。同じ回のシリーズは同じ日に始める。
  年はまたがない（またぐ日付を導いたら `InvalidPostseason`。年をまたぐと試合の年と能力の年がずれる）。
  この検査は次の試合を導くとき（`next_day()`・`played()`）だけに効く。`Series.date_of`・`scheduled_last_day`
  （画面に予定日を出すとき）は検査しないので、年をまたいだ日付を返しうる。
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from enum import Enum

from ..exceptions import InvalidPostseason
from ..value_objects import TeamRecord

# ポストシーズンの延長の上限（CS の全試合と日本シリーズの第7戦まで）。12回を終えて同点なら引分。
# 公式戦の上限（`simulation/engine.py` の `MAX_INNINGS`）とは別の規定（NPB でも年によって公式戦だけ変わったことがある）
POSTSEASON_MAX_INNINGS = 12
# CS に出る球団の数（リーグの上位から）
CS_TEAMS = 3
# 最後の公式戦の日から、最初の回の第1戦までの日数（NPB 2026 は公式戦の終わりから数日で CS が始まる）
DAYS_BEFORE_POSTSEASON = 3
# CS のファーストステージの予定の最終日から、ファイナルステージの第1戦までの日数
# （1日休み。NPB 2026 は予備日を挟んで翌々日）
DAYS_BETWEEN_CS_STAGES = 2
# 前の回の予定の最終日（と実際の最後の試合の遅い方）から、日本シリーズの各回の第1戦までの日数
DAYS_BEFORE_SERIES_ROUND = 4
# ファイナルステージの2勝のアドバンテージ（2026年の規定）の条件: 1位と相手のゲーム差がこれ以上、
TWO_WIN_ADVANTAGE_GAMES_BEHIND = 10.0
# または相手の勝率がこれ未満
TWO_WIN_ADVANTAGE_PERCENTAGE = 0.5
# 2-3-2 の日割り（第1〜8戦の、第1戦からの日数）。第2戦と第3戦・第5戦と第6戦の間に移動日、第8戦は第7戦の翌日
_TWO_THREE_TWO_DAYS = (0, 1, 3, 4, 5, 7, 8, 9)
# 2-3-2 で下位（第1戦の本拠地でない側）の本拠地になる試合
_TWO_THREE_TWO_AWAY_GAMES = frozenset({3, 4, 5})
# 2-3-2 の日割りの最後の試合（これより後は移動日を1日置いて本拠地を交互にする）
_TWO_THREE_TWO_LAST = len(_TWO_THREE_TWO_DAYS)
# 日本シリーズの回の名前（最後の回から数えて）
_SERIES_ROUND_NAMES = ("日本シリーズ", "日本シリーズ準決勝", "日本シリーズ準々決勝")


class PostseasonFormat(Enum):
    """ポストシーズンの方式。**方式の選択肢の唯一の出典**で、画面やモデルに複製しない。値は画面に出す名前。"""

    NPB = "CS と日本シリーズ（NPB 方式）"
    CHAMPIONS = "優勝球団で日本シリーズ"
    NONE = "なし"

    @property
    def label(self) -> str:
        return self.value

    @property
    def description(self) -> str:
        """方式の1行の説明（作成フォームで選択肢の下に出す）。"""
        return _FORMAT_DESCRIPTIONS[self]


_FORMAT_DESCRIPTIONS = {
    PostseasonFormat.NPB: (
        "各リーグの上位3球団でクライマックスシリーズを行い、勝ち抜いた球団で日本シリーズを行います"
        "（1リーグの世界はクライマックスシリーズの優勝まで）。"
    ),
    PostseasonFormat.CHAMPIONS: "各リーグの優勝球団で日本シリーズを行います（1リーグの世界は行いません）。",
    PostseasonFormat.NONE: "ポストシーズンを行わず、公式戦が終わればシーズンを締められます。",
}


class PostseasonStatus(Enum):
    """その年のポストシーズンの状況。保存せず、結果から導く。値は画面に出す名前。"""

    NONE = "なし"  # 方式が「なし」か、対戦が組めない（代表が1球団だけなど）
    NOT_STARTED = "開始前"
    IN_PROGRESS = "進行中"
    FINISHED = "終了"

    @property
    def is_settled(self) -> bool:
        """ポストシーズンが終わった、または無い（シーズンを締めてよい・世界の終了を出してよい）。"""
        return self in (PostseasonStatus.NONE, PostseasonStatus.FINISHED)


class SeriesStage(Enum):
    """シリーズの段。値は画面に出す名前（日本シリーズの回の名前は `Series.name`）。"""

    FIRST = "CS ファーストステージ"
    FINAL = "CS ファイナルステージ"
    JAPAN_SERIES = "日本シリーズ"


class SeriesPattern(Enum):
    """シリーズの日割りと本拠地の型。"""

    UPPER_HOME_DAILY = "連日・全試合上位の本拠地"  # CS
    TWO_THREE_TWO = "2-3-2"  # 日本シリーズ

    def day_offset(self, number: int) -> int:
        """第 `number` 戦の、第1戦からの日数。"""
        if self is SeriesPattern.UPPER_HOME_DAILY:
            return number - 1
        if number <= _TWO_THREE_TWO_LAST:
            return _TWO_THREE_TWO_DAYS[number - 1]
        # 第9戦からは移動日を1日置く
        return _TWO_THREE_TWO_DAYS[-1] + 2 * (number - _TWO_THREE_TWO_LAST)

    def upper_is_home(self, number: int) -> bool:
        """第 `number` 戦が上位（第1戦の本拠地の側）の本拠地か。"""
        if self is SeriesPattern.UPPER_HOME_DAILY:
            return True
        if number <= _TWO_THREE_TWO_LAST:
            return number not in _TWO_THREE_TWO_AWAY_GAMES
        # 第8戦（上位）のあと、第9戦は下位・第10戦は上位…と交互
        return (number - _TWO_THREE_TWO_LAST) % 2 == 0


@dataclass(frozen=True)
class SeriesRules:
    """1シリーズの規則。値は下の定数（domain が出典）。"""

    wins_needed: int  # 先に何勝で勝ち抜けか（アドバンテージ込み）
    max_games: int | None  # 試合数の上限。None は上限なし（勝ちの数に届くまで）
    advantage: int  # 上位に与える勝ち
    pattern: SeriesPattern
    # 予定の最終日を決める試合の番号（次の回の第1戦の日付に使う）。CS は上限、日本シリーズは第7戦
    scheduled_games: int
    # この番号以降の試合は延長の制限なし。None なら全試合が `POSTSEASON_MAX_INNINGS` まで
    unlimited_from: int | None = None

    def max_innings(self, number: int) -> int | None:
        """第 `number` 戦の延長の上限（`simulate_game` の `max_innings` に渡す）。None は制限なし。"""
        if self.unlimited_from is not None and number >= self.unlimited_from:
            return None
        return POSTSEASON_MAX_INNINGS


FIRST_STAGE = SeriesRules(
    wins_needed=2, max_games=3, advantage=0, pattern=SeriesPattern.UPPER_HOME_DAILY, scheduled_games=3
)
FINAL_STAGE = SeriesRules(
    wins_needed=4, max_games=6, advantage=1, pattern=SeriesPattern.UPPER_HOME_DAILY, scheduled_games=6
)
FINAL_STAGE_TWO_WIN_ADVANTAGE = SeriesRules(
    wins_needed=5, max_games=7, advantage=2, pattern=SeriesPattern.UPPER_HOME_DAILY, scheduled_games=7
)
JAPAN_SERIES = SeriesRules(
    wins_needed=4,
    max_games=None,
    advantage=0,
    pattern=SeriesPattern.TWO_THREE_TWO,
    scheduled_games=7,
    unlimited_from=8,
)


def final_stage_rules(leader: TeamRecord, challenger: TeamRecord) -> SeriesRules:
    """ファイナルステージの規則。相手が1位から `TWO_WIN_ADVANTAGE_GAMES_BEHIND` 以上離れているか、
    勝率が `TWO_WIN_ADVANTAGE_PERCENTAGE` 未満なら、2勝のアドバンテージの7試合制（2026年の規定）。

    相手は FS の勝者（2球団のリーグなら2位。**一般化**）。ゲーム差と勝率は順位表と同じ `TeamRecord` が出典。
    """
    if (
        challenger.games_behind(leader) >= TWO_WIN_ADVANTAGE_GAMES_BEHIND
        or challenger.winning_percentage < TWO_WIN_ADVANTAGE_PERCENTAGE
    ):
        return FINAL_STAGE_TWO_WIN_ADVANTAGE
    return FINAL_STAGE


class OutcomeKind(Enum):
    """球団のポストシーズンの結果の区分。値は区分の名前（画面には `PostseasonOutcome.label` を出す）。"""

    CHAMPION = "優勝"
    ELIMINATED = "敗退"
    NOT_QUALIFIED = "出場なし"


@dataclass(frozen=True)
class PostseasonOutcome:
    """球団1つのその年のポストシーズンの結果。

    `label` は画面に出す名前（「日本一」・「CS ファイナルステージ敗退」・「出場なし」）。
    """

    kind: OutcomeKind
    label: str


@dataclass(frozen=True)
class PostseasonResult:
    """ポストシーズンの試合1つの結果。`winner_team_id` が None なら引分。"""

    played_on: datetime.date
    home_team_id: int
    away_team_id: int
    winner_team_id: int | None

    def __post_init__(self) -> None:
        if self.home_team_id == self.away_team_id:
            raise InvalidPostseason("同じ球団どうしの試合はありません。")
        if self.winner_team_id is not None and self.winner_team_id not in (self.home_team_id, self.away_team_id):
            raise InvalidPostseason("勝った球団がその試合の球団ではありません。")


@dataclass(frozen=True)
class FinalStanding:
    """リーグの最終順位の1行（球団と公式戦の勝敗）。"""

    team_id: int
    record: TeamRecord


@dataclass(frozen=True)
class LeagueFinish:
    """リーグの最終順位。`standings` は**全順序**（`race.final_order` で同率を決めた並び）。試合の無いリーグは空。"""

    league_id: int
    standings: tuple[FinalStanding, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "standings", tuple(self.standings))
        ids = [row.team_id for row in self.standings]
        if len(ids) != len(set(ids)):
            raise InvalidPostseason("最終順位に同じ球団が2回あります。")


@dataclass(frozen=True)
class Series:
    """1つのシリーズと、そこまでの試合。`upper_team_id` は上位（アドバンテージ・同数のときの勝ち・
    CS の全試合と日本シリーズの第1戦の本拠地の側）。"""

    stage: SeriesStage
    name: str
    league_id: int | None  # CS のリーグ。日本シリーズは None
    upper_team_id: int
    lower_team_id: int
    rules: SeriesRules
    starts_on: datetime.date
    games: tuple[PostseasonResult, ...] = ()

    @property
    def upper_wins(self) -> int:
        """上位の勝利数（アドバンテージ込み）。"""
        return self.rules.advantage + sum(1 for game in self.games if game.winner_team_id == self.upper_team_id)

    @property
    def lower_wins(self) -> int:
        return sum(1 for game in self.games if game.winner_team_id == self.lower_team_id)

    @property
    def ties(self) -> int:
        return sum(1 for game in self.games if game.winner_team_id is None)

    @property
    def winner(self) -> int | None:
        """勝ち抜けた球団。まだ決まっていなければ None。"""
        rules = self.rules
        upper, lower = self.upper_wins, self.lower_wins
        if upper >= rules.wins_needed:
            return self.upper_team_id
        if lower >= rules.wins_needed:
            return self.lower_team_id
        if rules.max_games is not None:
            remaining = rules.max_games - len(self.games)
            # 打ち切り: 残りを下位が全部勝っても、上位の勝利数が同数以上（同数なら上位の勝ち）
            if upper >= lower + remaining:
                return self.upper_team_id
            # 残りを上位が全部勝っても、下位を上回れない
            if lower > upper + remaining:
                return self.lower_team_id
        return None

    @property
    def loser(self) -> int | None:
        winner = self.winner
        if winner is None:
            return None
        return self.lower_team_id if winner == self.upper_team_id else self.upper_team_id

    @property
    def is_decided(self) -> bool:
        return self.winner is not None

    def involves(self, team_id: int) -> bool:
        return team_id in (self.upper_team_id, self.lower_team_id)

    def date_of(self, number: int) -> datetime.date:
        """第 `number` 戦の日付。年をまたぐかは検査しない（検査は `next_day()`。予定日を画面に出すときは注意）。"""
        return self.starts_on + datetime.timedelta(days=self.rules.pattern.day_offset(number))

    @property
    def scheduled_last_day(self) -> datetime.date:
        """予定の最終日（CS は試合数の上限、日本シリーズは第7戦の日）。"""
        return self.date_of(self.rules.scheduled_games)

    @property
    def last_played_on(self) -> datetime.date | None:
        return self.games[-1].played_on if self.games else None

    def next_game(self) -> ScheduledGame | None:
        """次の試合。決着していれば None。"""
        if self.is_decided:
            return None
        number = len(self.games) + 1
        upper_home = self.rules.pattern.upper_is_home(number)
        return ScheduledGame(
            series=self,
            number=number,
            played_on=self.date_of(number),
            home_team_id=self.upper_team_id if upper_home else self.lower_team_id,
            away_team_id=self.lower_team_id if upper_home else self.upper_team_id,
            max_innings=self.rules.max_innings(number),
        )

    def with_game(self, result: PostseasonResult) -> Series:
        return replace(self, games=(*self.games, result))


@dataclass(frozen=True)
class ScheduledGame:
    """勝ち上がりから導いた次の試合。`max_innings` は `simulate_game` に渡す延長の上限（None は制限なし）。"""

    series: Series
    number: int  # 第何戦か
    played_on: datetime.date
    home_team_id: int
    away_team_id: int
    max_innings: int | None


@dataclass(frozen=True)
class PostseasonDay:
    """次に試合をする日と、その日の対戦（シリーズの並び順）。"""

    played_on: datetime.date
    games: tuple[ScheduledGame, ...]


@dataclass(frozen=True)
class _Plan:
    """結果によらず決まる、その年の勝ち上がりの骨組み。"""

    postseason_format: PostseasonFormat
    leagues: tuple[LeagueFinish, ...]  # 試合のあるリーグだけ。リーグの優先順
    league_priority: tuple[int, ...]
    last_regular_date: datetime.date
    stages: tuple[SeriesStage, ...]  # 回ごとの段（日本シリーズは回の数だけ並ぶ）

    @property
    def series_rounds(self) -> int:
        return sum(1 for stage in self.stages if stage is SeriesStage.JAPAN_SERIES)

    def league_of(self, team_id: int) -> int:
        return next(league.league_id for league in self.leagues if any(r.team_id == team_id for r in league.standings))

    def record_of(self, team_id: int) -> TeamRecord:
        return next(r.record for league in self.leagues for r in league.standings if r.team_id == team_id)

    def priority_of(self, team_id: int) -> int:
        return self.league_priority.index(self.league_of(team_id))

    def is_participant(self, team_id: int) -> bool:
        """ポストシーズンに出る球団か（方式 `NPB` は各リーグの上位 `CS_TEAMS`、`CHAMPIONS` は各リーグの1位）。"""
        if not self.stages:
            return False
        places = CS_TEAMS if self.postseason_format is PostseasonFormat.NPB else 1
        return any(row.team_id == team_id for league in self.leagues for row in league.standings[:places])

    def stage_name(self, index: int) -> str:
        """`index` 番目の回の名前（日本シリーズは回の数から「準決勝」などを付ける。`Series.name` と同じ）。"""
        stage = self.stages[index]
        if stage is not SeriesStage.JAPAN_SERIES:
            return stage.value
        return _round_name(self, sum(1 for s in self.stages[: index + 1] if s is SeriesStage.JAPAN_SERIES))


def _stages(postseason_format: PostseasonFormat, leagues: Sequence[LeagueFinish]) -> tuple[SeriesStage, ...]:
    if postseason_format is PostseasonFormat.NONE:
        return ()
    stages: list[SeriesStage] = []
    if postseason_format is PostseasonFormat.NPB:
        if any(len(league.standings) >= CS_TEAMS for league in leagues):
            stages.append(SeriesStage.FIRST)
        if any(len(league.standings) >= 2 for league in leagues):
            stages.append(SeriesStage.FINAL)
    representatives = len(leagues)
    rounds = (representatives - 1).bit_length() if representatives >= 2 else 0
    stages.extend([SeriesStage.JAPAN_SERIES] * rounds)
    return tuple(stages)


def _seeded_slots(plan: _Plan, representatives: Sequence[int]) -> list[int | None]:
    """日本シリーズのトーナメントの枠（左から順に2つずつ当たる）。空きの枠（None）の隣はシードで次の回へ。

    勝率の高い順に種を付け（並べばリーグの優先順）、標準の組み合わせ（1位と最下位の種が当たり、
    1位と2位の種は決勝まで当たらない）に置く。2の累乗に満たない分は上位の種の相手が空きになる。
    """
    seeds = sorted(representatives, key=lambda t: (-plan.record_of(t).winning_percentage, plan.priority_of(t)))
    size = 1 << (len(seeds) - 1).bit_length()
    order = [1]
    while len(order) < size:
        width = len(order) * 2
        order = [seed for top in order for seed in (top, width + 1 - top)]
    return [seeds[seed - 1] if seed <= len(seeds) else None for seed in order]


def _representatives(plan: _Plan, rounds: Sequence[Sequence[Series]]) -> list[int]:
    """各リーグの代表（日本シリーズの出場球団）。リーグの優先順。"""
    finals = {series.league_id: series for round_ in rounds for series in round_ if series.stage is SeriesStage.FINAL}
    result = []
    for league in plan.leagues:
        final = finals.get(league.league_id)
        if final is not None:
            winner = final.winner
            assert winner is not None  # 次の回は前の回が全部決着してから組む
            result.append(winner)
        else:
            result.append(league.standings[0].team_id)
    return result


def _round_name(plan: _Plan, series_round: int) -> str:
    from_last = plan.series_rounds - series_round
    if from_last < len(_SERIES_ROUND_NAMES):
        return _SERIES_ROUND_NAMES[from_last]
    return f"日本シリーズ {series_round}回戦"


def _start_of(plan: _Plan, stage: SeriesStage, rounds: Sequence[Sequence[Series]]) -> datetime.date:
    if not rounds:
        return plan.last_regular_date + datetime.timedelta(days=DAYS_BEFORE_POSTSEASON)
    previous = rounds[-1]
    ends = [series.scheduled_last_day for series in previous]
    ends.extend(series.last_played_on for series in previous if series.last_played_on is not None)
    gap = DAYS_BETWEEN_CS_STAGES if stage is SeriesStage.FINAL else DAYS_BEFORE_SERIES_ROUND
    return max(ends) + datetime.timedelta(days=gap)


def _build_round(plan: _Plan, rounds: Sequence[Sequence[Series]]) -> tuple[Series, ...]:
    """次の回のシリーズ（前の回はすべて決着している）。"""
    stage = plan.stages[len(rounds)]
    starts_on = _start_of(plan, stage, rounds)
    if stage is SeriesStage.FIRST:
        return tuple(
            Series(
                stage=stage,
                name=stage.value,
                league_id=league.league_id,
                upper_team_id=league.standings[1].team_id,
                lower_team_id=league.standings[2].team_id,
                rules=FIRST_STAGE,
                starts_on=starts_on,
            )
            for league in plan.leagues
            if len(league.standings) >= CS_TEAMS
        )
    if stage is SeriesStage.FINAL:
        firsts = {s.league_id: s for round_ in rounds for s in round_ if s.stage is SeriesStage.FIRST}
        finals = []
        for league in plan.leagues:
            if len(league.standings) < 2:
                continue
            leader = league.standings[0]
            first = firsts.get(league.league_id)
            challenger = first.winner if first is not None else league.standings[1].team_id
            assert challenger is not None  # FS は決着している
            finals.append(
                Series(
                    stage=stage,
                    name=stage.value,
                    league_id=league.league_id,
                    upper_team_id=leader.team_id,
                    lower_team_id=challenger,
                    rules=final_stage_rules(leader.record, plan.record_of(challenger)),
                    starts_on=starts_on,
                )
            )
        return tuple(finals)
    # 日本シリーズ: 1回戦の枠から、前の回の勝者（とシード）を勝ち上がらせる
    slots = _seeded_slots(plan, _representatives(plan, rounds))
    played = [round_ for round_ in rounds if round_ and round_[0].stage is SeriesStage.JAPAN_SERIES]
    for round_ in played:
        slots = [_advancer(round_, slots[i], slots[i + 1]) for i in range(0, len(slots), 2)]
    series_round = len(played) + 1
    result = []
    for i in range(0, len(slots), 2):
        first_team, second_team = slots[i], slots[i + 1]
        if first_team is None or second_team is None:
            continue  # シード
        # 第1戦の本拠地はリーグの優先順で先の球団
        upper, lower = sorted((first_team, second_team), key=plan.priority_of)
        result.append(
            Series(
                stage=stage,
                name=_round_name(plan, series_round),
                league_id=None,
                upper_team_id=upper,
                lower_team_id=lower,
                rules=JAPAN_SERIES,
                starts_on=starts_on,
            )
        )
    return tuple(result)


def _advancer(round_: Sequence[Series], first: int | None, second: int | None) -> int | None:
    """枠の2つから次の回に進む球団（片方が空きならもう片方。両方いればその回のシリーズの勝者）。"""
    if first is None or second is None:
        return first if second is None else second
    series = next(s for s in round_ if s.involves(first) and s.involves(second))
    return series.winner


def _next_day(plan: _Plan, rounds: Sequence[Sequence[Series]]) -> PostseasonDay | None:
    if not rounds:
        return None
    upcoming = [game for series in rounds[-1] if (game := series.next_game()) is not None]
    if not upcoming:
        return None
    day = min(game.played_on for game in upcoming)
    if day.year != plan.last_regular_date.year:
        raise InvalidPostseason(f"ポストシーズンの試合が年をまたぎます（{day.isoformat()}）。")
    return PostseasonDay(played_on=day, games=tuple(game for game in upcoming if game.played_on == day))


def _built(plan: _Plan, rounds: list[tuple[Series, ...]]) -> list[tuple[Series, ...]]:
    """前の回がすべて決着していれば、次の回を組む（組める回が尽きるまで）。"""
    while len(rounds) < len(plan.stages) and (not rounds or all(s.is_decided for s in rounds[-1])):
        rounds.append(_build_round(plan, rounds))
    return rounds


@dataclass(frozen=True)
class PostseasonBracket:
    """その年の勝ち上がり。`derive` で最終順位と結果から導く（保存しない）。"""

    plan: _Plan
    results: tuple[PostseasonResult, ...]
    rounds: tuple[tuple[Series, ...], ...]  # 組めた回（結果が出て次の回が決まるまで、先の回は無い）

    @classmethod
    def derive(
        cls,
        postseason_format: PostseasonFormat,
        leagues: Iterable[LeagueFinish],
        league_priority: Sequence[int],
        last_regular_date: datetime.date,
        results: Iterable[PostseasonResult] = (),
    ) -> PostseasonBracket:
        """勝ち上がりを導く。結果が導いた対戦と食い違えば `InvalidPostseason`。

        `leagues` はリーグごとの最終順位（全順序）、`league_priority` はその年のリーグの優先順
        （`world.league_priority`）、`last_regular_date` はその年の最後の公式戦の日、`results` はその年の
        ポストシーズンの試合の結果（順不同）。
        """
        given = list(leagues)
        if len({league.league_id for league in given}) != len(given):
            raise InvalidPostseason("同じリーグの最終順位が2つあります。")
        priority = tuple(league_priority)
        if len(set(priority)) != len(priority):
            raise InvalidPostseason("リーグの優先順に同じリーグが2回あります。")
        playing = [league for league in given if league.standings]
        if any(league.league_id not in priority for league in playing):
            raise InvalidPostseason("リーグの優先順に無いリーグがあります。")
        team_ids = [row.team_id for league in playing for row in league.standings]
        if len(team_ids) != len(set(team_ids)):
            raise InvalidPostseason("同じ球団が2つのリーグの順位にあります。")
        playing.sort(key=lambda league: priority.index(league.league_id))
        plan = _Plan(
            postseason_format=postseason_format,
            leagues=tuple(playing),
            league_priority=priority,
            last_regular_date=last_regular_date,
            stages=_stages(postseason_format, playing),
        )
        return cls._replay(plan, tuple(results))

    @classmethod
    def _replay(cls, plan: _Plan, results: tuple[PostseasonResult, ...]) -> PostseasonBracket:
        rounds = _built(plan, [])
        by_date: dict[datetime.date, list[PostseasonResult]] = {}
        for result in results:
            by_date.setdefault(result.played_on, []).append(result)
        for played_on in sorted(by_date):
            day = _next_day(plan, rounds)
            if day is None:
                raise InvalidPostseason(
                    f"{played_on.isoformat()} の試合は、ポストシーズンの対戦が残っていない"
                    "（終わった・無い）ときの試合です。"
                )
            if played_on != day.played_on:
                raise InvalidPostseason(
                    f"{played_on.isoformat()} の試合は、勝ち上がりから導いた次の試合の日"
                    f"（{day.played_on.isoformat()}）と食い違います。"
                )
            expected = list(day.games)
            current = list(rounds[-1])
            for result in sorted(by_date[played_on], key=lambda r: (r.home_team_id, r.away_team_id)):
                game = next(
                    (
                        g
                        for g in expected
                        if (g.home_team_id, g.away_team_id) == (result.home_team_id, result.away_team_id)
                    ),
                    None,
                )
                if game is None:
                    raise InvalidPostseason(
                        f"{played_on.isoformat()} の試合（ホーム {result.home_team_id}・"
                        f"ビジター {result.away_team_id}）は、その日の対戦にありません"
                        "（決着したシリーズ・出場していない球団・同じ日の二重の試合・本拠地の取り違え）。"
                    )
                if game.max_innings is None and result.winner_team_id is None:
                    raise InvalidPostseason(
                        f"{played_on.isoformat()} の試合（{game.series.name} 第{game.number}戦）は"
                        "延長の制限が無いので、引分にはなりません。"
                    )
                expected.remove(game)
                index = current.index(game.series)
                current[index] = current[index].with_game(result)
            rounds[-1] = tuple(current)
            _built(plan, rounds)
        return cls(plan=plan, results=results, rounds=tuple(rounds))

    def played(self, results: Iterable[PostseasonResult]) -> PostseasonBracket:
        """結果を足した勝ち上がり。保存しようとしている試合が導いた対戦と食い違えば `InvalidPostseason`。"""
        return self._replay(self.plan, (*self.results, *results))

    @property
    def postseason_format(self) -> PostseasonFormat:
        return self.plan.postseason_format

    @property
    def champion(self) -> int | None:
        """優勝（日本一。1リーグは CS の勝者）の球団。まだ決まっていない・ポストシーズンが無ければ None。"""
        if not self.plan.stages or len(self.rounds) < len(self.plan.stages):
            return None
        last = self.rounds[-1]
        return last[0].winner if len(last) == 1 else None

    @property
    def status(self) -> PostseasonStatus:
        if not self.plan.stages:
            return PostseasonStatus.NONE
        if self.champion is not None:
            return PostseasonStatus.FINISHED
        if not self.results:
            return PostseasonStatus.NOT_STARTED
        return PostseasonStatus.IN_PROGRESS

    @property
    def champion_label(self) -> str:
        """優勝の呼び名。最後の回が日本シリーズなら「日本一」、1リーグ（CS で終わる）なら「CS 優勝」。
        ポストシーズンが無ければ空。"""
        if not self.plan.stages:
            return ""
        return "日本一" if self.plan.stages[-1] is SeriesStage.JAPAN_SERIES else "CS 優勝"

    def pending_stage_names(self) -> list[str]:
        """まだ組めていない先の回の名前（回の順）。前の回が決着するまで、対戦は決まらない。"""
        return [self.plan.stage_name(index) for index in range(len(self.rounds), len(self.plan.stages))]

    def outcome_of(self, team_id: int) -> PostseasonOutcome | None:
        """球団のその年の結果。勝ち残っている（まだ決まっていない）・ポストシーズンが無ければ None。

        出場しない球団は、勝ち上がりが始まる前から「出場なし」に決まる。
        """
        if not self.plan.stages:
            return None
        if not self.plan.is_participant(team_id):
            return PostseasonOutcome(OutcomeKind.NOT_QUALIFIED, OutcomeKind.NOT_QUALIFIED.value)
        if self.champion == team_id:
            return PostseasonOutcome(OutcomeKind.CHAMPION, self.champion_label)
        lost = next((series for series in self.series_of(team_id) if series.loser == team_id), None)
        if lost is not None:
            return PostseasonOutcome(OutcomeKind.ELIMINATED, f"{lost.name}敗退")
        return None

    def series(self) -> list[Series]:
        """組めたシリーズ（回の順。CS はリーグの優先順、日本シリーズはトーナメントの枠の順）。"""
        return [series for round_ in self.rounds for series in round_]

    def next_day(self) -> PostseasonDay | None:
        """次に試合をする日とその日の対戦。終わっている・ポストシーズンが無ければ None。"""
        return _next_day(self.plan, self.rounds)

    def series_of(self, team_id: int) -> list[Series]:
        """球団が出たシリーズ（回の順）。"""
        return [series for series in self.series() if series.involves(team_id)]
