"""ペナントの日程（未消化の対戦）の生成。Django にも numpy にも依存しない。

作り方:
  1. 同一リーグは円周法（総当たりの1因子分解）で「全球団が1回ずつ当たる組」を球団数-1 個作る。
     これを1周とし、1カードにつき `ceil(試合数 / 連戦の長さ)` 周する。
     全球団が毎日試合をするので、1周の中の全カードは同じ日数（3 か 2）になる。
     25試合なら 3 が7周・2 が2周。2 の周は「偶数番目の周」に置く（下のホーム数のため）。
  2. ホームは de Werra の向き付けで決める。1周の中でどの球団もホームとビジターがほぼ交互に
     なり（切れ目は各球団1回以下）、次の周は向きを反転する。カードのホームは周ごとに交互に
     なるので、25試合のホームは 12 か 13 に収まる。奇数番目の周は組の順を入れ替えて、
     周の境目で同じカードが続かず、連続ホーム・ビジターが長くならないようにする。
  3. 交流戦は A リーグと B リーグの総当たり（ラテン方陣）を1組=3連戦として並べる。
     ホームは組ごとに交互にするので、どの球団も交流戦はホーム半分・ビジター半分になる。
  4. 月曜を除いた日に、交流戦の開始日より前に入るだけの連戦を置き、交流戦を挟み、残りを
     交流戦のあとに置く。端数の日は全球団の休みになる。
  5. 乱数で変えるのは、球団の並び（どの球団がどの番号になるか）・2試合の周の位置・
     ホームの向きの反転・交流戦の組み合わせの順。

乱数は `random()` だけを使う（`random.Random` の他の API は版で列が変わりうるため）。
"""

import datetime
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from itertools import combinations
from typing import Protocol, TypeVar

from myapp.domain.exceptions import InvalidSchedule

MONDAY = 0
# NPB の1リーグの球団数。`ScheduleRules` の既定値はこの規模で1球団143試合になる
NPB_TEAMS_PER_LEAGUE = 6
_T = TypeVar("_T")
_MAX_ATTEMPTS = 20


class RandomSource(Protocol):
    """乱数源。`random.Random` がそのまま満たす。"""

    def random(self) -> float: ...


@dataclass(frozen=True)
class Fixture:
    """未消化の対戦。消化したら `Game` を作ってこれを消す（出典を2つにしない）。"""

    date: datetime.date
    home_team_id: int
    visitor_team_id: int

    def __post_init__(self) -> None:
        if self.home_team_id == self.visitor_team_id:
            raise InvalidSchedule("同じ球団どうしの対戦は組めません。")

    def involves(self, team_id: int) -> bool:
        return team_id in (self.home_team_id, self.visitor_team_id)


@dataclass(frozen=True)
class MonthDay:
    """年を持たない月日（交流戦の期間の指定に使う）。"""

    month: int
    day: int

    def in_year(self, year: int) -> datetime.date:
        return datetime.date(year, self.month, self.day)


@dataclass(frozen=True)
class ScheduleRules:
    """日程の規則。既定値は NPB 形式（2リーグ6球団ずつで 1球団143試合・全858試合）。"""

    intra_games: int = 25  # 同一リーグの1カードの試合数
    inter_games: int = 3  # 交流戦の1カードの試合数（1つの連戦で行う）
    series_length: int = 3  # 同一リーグの連戦の標準の長さ
    rest_weekday: int = MONDAY  # 試合をしない曜日（月曜 = 0）
    interleague_start: MonthDay = field(default_factory=lambda: MonthDay(5, 24))
    interleague_end: MonthDay = field(default_factory=lambda: MonthDay(6, 30))
    max_streak: int = 9  # 連続ホーム・連続ビジターの上限（試合数）

    def __post_init__(self) -> None:
        if self.intra_games < 1 or self.inter_games < 1 or self.series_length < 1:
            raise InvalidSchedule("試合数と連戦の長さは1以上にしてください。")
        if not 0 <= self.rest_weekday <= 6:
            raise InvalidSchedule("休みの曜日が不正です。")

    def games_per_team(self, teams_per_league: int) -> int:
        """1球団の年間試合数（2リーグ・各リーグ teams_per_league 球団のとき）。"""
        return (teams_per_league - 1) * self.intra_games + teams_per_league * self.inter_games


def generate_schedule(
    leagues: Mapping[int, Sequence[int]],
    rules: ScheduleRules,
    start: datetime.date,
    rng: RandomSource,
) -> list[Fixture]:
    """1シーズン分の日程を作る。同じ乱数列なら同じ日程になる。日付順（同日はホームの id 順）。"""
    league_a, league_b = _validated_leagues(leagues, rules, start)
    for _ in range(_MAX_ATTEMPTS):
        fixtures = _build(league_a, league_b, rules, start, rng)
        if longest_streak(fixtures) <= rules.max_streak:
            return fixtures
    raise InvalidSchedule("連続ホーム・連続ビジターの上限を守る日程が組めませんでした。")


def longest_streak(fixtures: Sequence[Fixture]) -> int:
    """どれかの球団の、連続ホーム（または連続ビジター）の最長の試合数。"""
    games: dict[int, list[tuple[datetime.date, bool]]] = {}
    for f in fixtures:
        games.setdefault(f.home_team_id, []).append((f.date, True))
        games.setdefault(f.visitor_team_id, []).append((f.date, False))
    longest = 0
    for sequence in games.values():
        sequence.sort()
        run, previous = 0, None
        for _, at_home in sequence:
            run = run + 1 if at_home == previous else 1
            previous = at_home
            longest = max(longest, run)
    return longest


# --- 進める範囲 -------------------------------------------------------------


class AdvanceTarget(Enum):
    """シーズンをどこまで進めるか。"""

    DAY = "day"  # 次に試合がある日まで（試合のない日は数えない）
    WEEK = "week"  # 次に試合がある日から7日間
    NEXT_MANAGED_GAME = "next_managed_game"  # 自軍の次の試合の日まで（その日を含む。自軍の未消化が無ければ残り全部）
    MONTH_END = "month_end"  # 次に試合がある日の月の末まで
    SEASON_END = "season_end"  # 未消化の日程をすべて


def dates_to_play(
    fixtures: Sequence[Fixture],
    today: datetime.date,
    target: AdvanceTarget,
    managed_team_id: int | None = None,
) -> list[datetime.date]:
    """未消化の日程のうち、今回の「進める」で消化する日付を日付順で返す。

    `today` は消化した最後の試合日（保存せず導く）。それより後の日程だけを対象にする。
    どの単位でも少なくとも次の試合日を含むので、進めても今日が動かない状態にはならない。
    未消化が無ければ空。
    """
    upcoming = sorted({f.date for f in fixtures if f.date > today})
    if not upcoming:
        return []
    first = upcoming[0]
    if target is AdvanceTarget.DAY:
        return [first]
    if target is AdvanceTarget.WEEK:
        limit = first + datetime.timedelta(days=6)
        return [d for d in upcoming if d <= limit]
    if target is AdvanceTarget.MONTH_END:
        return [d for d in upcoming if (d.year, d.month) == (first.year, first.month)]
    if target is AdvanceTarget.NEXT_MANAGED_GAME:
        if managed_team_id is None:
            raise InvalidSchedule("自軍が指定されていません。")
        mine = sorted({f.date for f in fixtures if f.date > today and f.involves(managed_team_id)})
        if not mine:
            return upcoming
        return [d for d in upcoming if d <= mine[0]]
    return upcoming


# --- 内部 -------------------------------------------------------------------

_Pair = tuple[int, int]  # (ホーム, ビジター)


def _validated_leagues(
    leagues: Mapping[int, Sequence[int]], rules: ScheduleRules, start: datetime.date
) -> tuple[list[int], list[int]]:
    if len(leagues) != 2:
        raise InvalidSchedule(f"日程を組めるのは2リーグのときだけです（{len(leagues)}リーグが指定されました）。")
    league_a, league_b = (list(leagues[key]) for key in sorted(leagues))
    if len(league_a) != len(league_b):
        raise InvalidSchedule("2つのリーグの球団数が違うため交流戦が組めません。")
    n = len(league_a)
    if n < 2 or n % 2:
        raise InvalidSchedule(
            f"1リーグの球団数が{n}では、毎日全球団が試合をする日程が組めません（偶数で2以上が必要）。"
        )
    if len(set(league_a) | set(league_b)) != 2 * n:
        raise InvalidSchedule("同じ球団が重複して指定されています。")
    if start.weekday() == rules.rest_weekday:
        raise InvalidSchedule("開幕日が休みの曜日です。")
    return league_a, league_b


def _shuffle(items: list[_T], rng: RandomSource) -> None:
    """Fisher-Yates。使う乱数は random() だけ。"""
    for i in range(len(items) - 1, 0, -1):
        j = min(int(rng.random() * (i + 1)), i)
        items[i], items[j] = items[j], items[i]


def _pick(options: Sequence[_T], rng: RandomSource) -> _T:
    return options[min(int(rng.random() * len(options)), len(options) - 1)]


def _series_lengths(games: int, standard: int, rng: RandomSource) -> list[int]:
    """1カードの試合数を周ごとの連戦の長さに分ける（25試合・標準3なら 3 が7つと 2 が2つ）。

    偶数番目の周の試合数の和が、試合数の半分（切り捨て・切り上げ）に収まる並びだけを選ぶ。
    偶数番目の周はカードの基準の向きでホームになるので、これで両チームのホームが偏らない。
    """
    count = -(-games // standard)
    deficit = count * standard - games
    if deficit > count:
        raise InvalidSchedule("同一リーグの試合数を連戦に分けられません。")
    allowed = {games // 2, games - games // 2}
    options = []
    for reduced in combinations(range(count), deficit):
        lengths = [standard - 1 if i in reduced else standard for i in range(count)]
        if sum(lengths[0::2]) in allowed:
            options.append(lengths)
    if not options:
        raise InvalidSchedule("同一リーグの試合数を、ホームが偏らない連戦に分けられません。")
    return _pick(options, rng)


def _round_robin(teams: Sequence[int], rng: RandomSource) -> list[list[_Pair]]:
    """円周法 + de Werra の向き付け。n-1 組あり、各組は全球団が1回ずつ当たる。

    球団の並びは乱数で入れ替える。どの球団もホームとビジターが組ごとにほぼ交互になる。
    """
    p = list(teams)
    _shuffle(p, rng)
    n = len(p)
    rounds = []
    for r in range(n - 1):
        pairs = [(p[n - 1], p[r]) if r % 2 == 0 else (p[r], p[n - 1])]
        for i in range(1, n // 2):
            first, second = p[(r + i) % (n - 1)], p[(r - i) % (n - 1)]
            pairs.append((first, second) if i % 2 == 0 else (second, first))
        rounds.append(pairs)
    return rounds


def _odd_cycle_order(rounds: int) -> list[int]:
    """奇数番目の周の組の順。偶数番目の周は 0.. の順。

    周の境目で同じカードが続かず（最初は 0 でも最後でもなく、最後は 0 でない）、
    連続ホーム・ビジターが長くならない並び（球団が6のとき 3連戦 × 3 = 9試合までに収まる）。
    """
    if rounds < 3:
        return list(range(rounds))
    return [*range(1, rounds - 1), 0, rounds - 1]


def _playing_days(first: datetime.date, rest_weekday: int) -> Iterator[datetime.date]:
    day = first
    while True:
        if day.weekday() != rest_weekday:
            yield day
        day += datetime.timedelta(days=1)


def _count_playing_days(first: datetime.date, end: datetime.date, rest_weekday: int) -> int:
    """first 以上 end 未満の試合日の数。"""
    days = (end - first).days
    return sum(1 for k in range(max(days, 0)) if (first + datetime.timedelta(days=k)).weekday() != rest_weekday)


def _build(
    league_a: list[int],
    league_b: list[int],
    rules: ScheduleRules,
    start: datetime.date,
    rng: RandomSource,
) -> list[Fixture]:
    n = len(league_a)
    lengths = _series_lengths(rules.intra_games, rules.series_length, rng)
    flip = int(rng.random() * 2) % 2
    rounds_a = _round_robin(league_a, rng)
    rounds_b = _round_robin(league_b, rng)

    def intra(days: Iterator[datetime.date], cycle: int, r: int) -> list[Fixture]:
        reverse = (cycle + flip) % 2 == 1
        pairs = rounds_a[r] + rounds_b[r]
        series_days = [next(days) for _ in range(lengths[cycle])]
        return [Fixture(d, v, h) if reverse else Fixture(d, h, v) for d in series_days for h, v in pairs]

    odd_order = _odd_cycle_order(n - 1)
    slots = [(cycle, (odd_order[r] if cycle % 2 else r)) for cycle in range(len(lengths)) for r in range(n - 1)]
    window_start = rules.interleague_start.in_year(start.year)
    window_end = rules.interleague_end.in_year(start.year)
    available = _count_playing_days(start, window_start, rules.rest_weekday)
    taken, used = 0, 0
    while taken < len(slots) and used + lengths[slots[taken][0]] <= available:
        used += lengths[slots[taken][0]]
        taken += 1

    fixtures: list[Fixture] = []
    days = _playing_days(start, rules.rest_weekday)
    for cycle, r in slots[:taken]:
        fixtures.extend(intra(days, cycle, r))

    days = _playing_days(max(start, window_start), rules.rest_weekday)
    perm_a, perm_b = list(league_a), list(league_b)
    _shuffle(perm_a, rng)
    _shuffle(perm_b, rng)
    host_a_first = int(rng.random() * 2) % 2
    last_inter = start
    for r in range(n):
        series_days = [next(days) for _ in range(rules.inter_games)]
        last_inter = series_days[-1]
        a_hosts = (r + host_a_first) % 2 == 0
        for i, a in enumerate(perm_a):
            b = perm_b[(i + r) % n]
            fixtures.extend(Fixture(d, a, b) if a_hosts else Fixture(d, b, a) for d in series_days)
    if last_inter > window_end:
        raise InvalidSchedule(f"交流戦が期間内（{window_end:%m月%d日}まで）に収まりません。開幕日を早めてください。")

    days = _playing_days(last_inter + datetime.timedelta(days=1), rules.rest_weekday)
    for cycle, r in slots[taken:]:
        fixtures.extend(intra(days, cycle, r))

    fixtures.sort(key=lambda f: (f.date, f.home_team_id))
    return fixtures
