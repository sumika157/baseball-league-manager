"""ペナントの日程（未消化の対戦）の生成。Django にも numpy にも依存しない。

形式: 1球団の年間試合数（既定 143）= 交流戦 + リーグ内の総当たり。
  - 交流戦: 各リーグは毎年「他の2リーグ」と当たる（リーグが2つなら相手は1つ、1つなら交流戦なし）。
    相手リーグの全球団と3連戦を1回ずつ。相手の組み合わせは対称で、年ごとに入れ替わる
    （`interleague_pairs`）。
  - リーグ内: 残りの試合を、同じリーグの相手に総当たりで配る。割り切れないときは相手ごとに
    1試合差まで（余りを持つ相手の組は、球団数 s のリーグを r 本の辺で結ぶ正則グラフにする）。
  2リーグ6球団ずつなら、リーグ内 25 × 5 + 交流戦 3 × 6 = 143、全 858 試合（NPB 形式）。

作り方:
  1. リーグ内は円周法（総当たりの1因子分解）で「全球団が1回ずつ当たる組」を作り、これを
     1周とする。カード1組は周ごとに連戦（3 か 2、余りの組は1試合長い）を行う。
     球団数が奇数のリーグは休みの球団（ダミー）を入れて組む。
  2. ホームは de Werra の向き付けで決める。1周の中でどの球団もホームとビジターがほぼ交互に
     なり、次の周は向きを反転する。偶数番目の周の試合数の和が試合数の半分（切り捨て・切り上げ）
     に収まる並びだけを選ぶので、カードのホームの偏りは1以内になる。奇数番目の周は組の順を
     入れ替えて、周の境目で同じカードが続かず、連続ホーム・ビジターが長くならないようにする。
  3. 交流戦は、リーグの組を辺とするグラフ（各リーグの次数は2以下）を辺着色して「フェーズ」に
     分ける。フェーズは交流戦の開始日から連続して置く。辺（2リーグ）は総当たりを、小さい側の
     球団がすべての大きい側の球団と1回ずつ当たるよう組ごとに並べ、1組=3連戦とする。
     ホームは組ごとに交互にする（球団数の違うリーグどうしでは、大きい側が±1組ずれることがある）。そのフェーズで交流戦の無いリーグは、リーグ内の試合を進める。
  4. 月曜を除いた日に、交流戦の開始日より前に入るだけのリーグ内の組を置く。残りは交流戦のあと。
  5. 乱数で変えるのは、球団の並び・連戦の長さの並び・ホームの向きの反転・交流戦の対戦順。

乱数は `random()` だけを使う（`random.Random` の他の API は版で列が変わりうるため）。
"""

import datetime
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from itertools import combinations, islice
from typing import Protocol

from myapp.domain.exceptions import InvalidSchedule
from myapp.domain.simulation.randomness import game_seed, make_random

MONDAY = 0
FRIDAY = 4
# NPB の1リーグの球団数。`ScheduleRules` の既定値はこの規模で1球団143試合になる
NPB_TEAMS_PER_LEAGUE = 6
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

    games_per_team: int = 143  # 1球団の年間試合数（交流戦 + リーグ内）
    inter_games: int = 3  # 交流戦の1カードの試合数（1つの連戦で行う）
    series_length: int = 3  # リーグ内の連戦の標準の長さ
    rest_weekday: int = MONDAY  # 試合をしない曜日（月曜 = 0）
    interleague_start: MonthDay = field(default_factory=lambda: MonthDay(5, 24))
    # 交流戦のフェーズ（リーグが3つ以上なら2〜3つ）を連続で置いて、ここまでに収める。
    # 2リーグなら 6月中旬に終わる（NPB と同じ）。8リーグ（2フェーズ）なら 7月上旬。
    interleague_end: MonthDay = field(default_factory=lambda: MonthDay(7, 31))
    max_streak: int = 12  # 連続ホーム・連続ビジターの上限（試合数。2リーグの NPB 形式は 9 に収まる）

    def __post_init__(self) -> None:
        if self.games_per_team < 1 or self.inter_games < 1:
            raise InvalidSchedule("試合数は1以上にしてください。")
        if self.series_length < 2:
            raise InvalidSchedule("連戦の長さは2以上にしてください。")
        if not 0 <= self.rest_weekday <= 6:
            raise InvalidSchedule("休みの曜日が不正です。")


def interleague_pairs(league_ids: Sequence[int], season: int) -> list[tuple[int, int]]:
    """その年に交流戦で当たるリーグの組（対称。各リーグは最大2つの相手）。年ごとに入れ替わる。

    リーグを id 順に輪に並べ、年ごとにずらした両隣と当たる（4リーグだけは、4つの輪の
    つなぎ方 3 通りを回す）。リーグが2つなら相手は1つ、1つなら交流戦なし、3つなら総当たり。
    """
    ids = sorted(league_ids)
    return [(ids[a], ids[b]) for a, b in _interleague_index_pairs(len(ids), season)]


def generate_schedule(
    leagues: Mapping[int, Sequence[int]],
    rules: ScheduleRules,
    start: datetime.date,
    rng: RandomSource,
    season: int | None = None,
) -> list[Fixture]:
    """1シーズン分の日程を作る。同じ乱数列なら同じ日程になる。日付順（同日はホームの id 順）。

    `leagues` はリーグ id → 球団 id の並び。`season` は交流戦の相手リーグを決める年
    （省略すると開幕日の年）。
    """
    teams = _validated_leagues(leagues, rules, start)
    index_pairs = _interleague_index_pairs(len(teams), start.year if season is None else season)
    for _ in range(_MAX_ATTEMPTS):
        fixtures = _build(teams, index_pairs, rules, start, rng)
        if longest_streak(fixtures) <= rules.max_streak:
            return fixtures
    raise InvalidSchedule("連続ホーム・連続ビジターの上限を守る日程が組めませんでした。")


def default_opening_day(year: int, rules: ScheduleRules) -> datetime.date:
    """開幕日の既定。3月25日以降で最初の金曜日（NPB は3月最終週の金曜に開幕する）。

    休みの曜日（既定は月曜）に当たる規則なら、翌日にずらす。
    """
    day = datetime.date(year, 3, 25)
    while day.weekday() != FRIDAY:
        day += datetime.timedelta(days=1)
    while day.weekday() == rules.rest_weekday:
        day += datetime.timedelta(days=1)
    return day


def season_schedule(
    leagues: Mapping[int, Sequence[int]],
    *,
    world_seed: int,
    year: int,
    rules: ScheduleRules | None = None,
) -> list[Fixture]:
    """その年の1シーズン分の日程。開幕年の日程も、締めたあとの翌年の日程も、この関数で作る。

    開幕日は `default_opening_day(year)`、乱数は `game_seed(世界のシード, year, "schedule")`。
    同じ世界・同じ年なら、いつ作っても同じ日程になる。
    """
    rules = rules if rules is not None else ScheduleRules()
    return generate_schedule(
        leagues,
        rules,
        default_opening_day(year, rules),
        make_random(game_seed(world_seed, year, "schedule")),
        season=year,
    )


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


# --- 内部: 交流戦の組 ---------------------------------------------------------

_Pair = tuple[int, int]  # (ホーム, ビジター)
_FOUR_LEAGUE_RINGS = (
    ((0, 1), (1, 2), (2, 3), (3, 0)),
    ((0, 1), (1, 3), (3, 2), (2, 0)),
    ((0, 2), (2, 1), (1, 3), (3, 0)),
)


def _interleague_index_pairs(count: int, season: int) -> list[tuple[int, int]]:
    if count < 2:
        return []
    if count == 2:
        return [(0, 1)]
    if count == 3:
        return [(0, 1), (0, 2), (1, 2)]
    if count == 4:
        return [(min(a, b), max(a, b)) for a, b in _FOUR_LEAGUE_RINGS[season % 3]]
    offsets = [d for d in range(1, (count - 1) // 2 + 1) if 2 * d != count]
    d = offsets[season % len(offsets)]
    return [(min(i, (i + d) % count), max(i, (i + d) % count)) for i in range(count)]


def _phases(pairs: list[tuple[int, int]]) -> list[list[tuple[int, int]]]:
    """交流戦の組（各リーグの次数 2 以下）を、同じリーグが重ならないフェーズに分ける（辺着色）。

    輪をたどった順に貪欲に塗るので、偶数の輪は2色、奇数の輪は3色になる。
    """
    remaining = list(pairs)
    ordered: list[tuple[int, int]] = []
    while remaining:
        edge = remaining.pop(0)
        ordered.append(edge)
        head = edge[1]
        while True:
            following = next((e for e in remaining if head in e), None)
            if following is None:
                break
            remaining.remove(following)
            ordered.append(following)
            head = following[0] if following[1] == head else following[1]
    colors: list[int] = []
    for i, edge in enumerate(ordered):
        used = {colors[j] for j in range(i) if set(ordered[j]) & set(edge)}
        colors.append(next(c for c in range(3) if c not in used))
    return [
        [e for e, c in zip(ordered, colors, strict=True) if c == color] for color in range(max(colors, default=-1) + 1)
    ]


# --- 内部: 共通 ---------------------------------------------------------------


def _validated_leagues(
    leagues: Mapping[int, Sequence[int]], rules: ScheduleRules, start: datetime.date
) -> list[list[int]]:
    if not leagues:
        raise InvalidSchedule("リーグが指定されていません。")
    teams = [list(leagues[key]) for key in sorted(leagues)]
    if any(not league for league in teams):
        raise InvalidSchedule("球団のいないリーグがあります。")
    if len({team for league in teams for team in league}) != sum(len(league) for league in teams):
        raise InvalidSchedule("同じ球団が重複して指定されています。")
    if start.weekday() == rules.rest_weekday:
        raise InvalidSchedule("開幕日が休みの曜日です。")
    return teams


def _shuffle[T](items: list[T], rng: RandomSource) -> None:
    """Fisher-Yates。使う乱数は random() だけ。"""
    for i in range(len(items) - 1, 0, -1):
        j = min(int(rng.random() * (i + 1)), i)
        items[i], items[j] = items[j], items[i]


def _pick[T](options: Sequence[T], rng: RandomSource) -> T:
    return options[min(int(rng.random() * len(options)), len(options) - 1)]


def _allowed_home(games: int) -> set[int]:
    """1カードの試合数 games に対して、片方のホーム数として許す値（差が1以内）。"""
    return {games // 2, games - games // 2}


def _series_lengths(games: int, standard: int, rng: RandomSource) -> list[int]:
    """1カードの試合数を周ごとの連戦の長さに分ける（25試合・標準3なら 3 が7つと 2 が2つ）。

    偶数番目の周の試合数の和が、ホームの許容範囲に収まる並びだけを選ぶ。
    偶数番目の周はカードの基準の向きでホームになるので、これで両チームのホームが偏らない。
    周の数は、そのような並びができるまで1周ずつ増やす。
    """
    base = -(-games // standard)
    for count in range(base, base + 3):
        deficit = count * standard - games
        if deficit > count:
            break
        options = []
        for reduced in combinations(range(count), deficit):
            lengths = [standard - 1 if i in reduced else standard for i in range(count)]
            if sum(lengths[0::2]) in _allowed_home(games):
                options.append(lengths)
        if options:
            return _pick(options, rng)
    raise InvalidSchedule(f"リーグ内の{games}試合を、ホームが偏らない連戦に分けられません。")


def _odd_cycle_order(rounds: int) -> list[int]:
    """奇数番目の周の組の順。偶数番目の周は 0.. の順。

    周の境目で同じカードが続かず（最初は 0 でも最後でもなく、最後は 0 でない）、
    連続ホーム・ビジターが長くならない並び（球団が6のとき 3連戦 × 3 = 9試合までに収まる）。
    """
    if rounds < 3:
        return list(range(rounds))
    return [*range(1, rounds - 1), 0, rounds - 1]


def _round_robin(order: list[int | None]) -> list[list[_Pair]]:
    """円周法 + de Werra の向き付け。None は休み（球団数が奇数のとき）。

    組の数は len(order) - 1。各組は全球団が1回ずつ当たる。
    どの球団もホームとビジターが組ごとにほぼ交互になる。休みの球団の試合は含めない。
    """
    p = order
    n = len(p)
    rounds = []
    for r in range(n - 1):
        pairs = [(p[n - 1], p[r]) if r % 2 == 0 else (p[r], p[n - 1])]
        for i in range(1, n // 2):
            first, second = p[(r + i) % (n - 1)], p[(r - i) % (n - 1)]
            pairs.append((first, second) if i % 2 == 0 else (second, first))
        rounds.append([(h, v) for h, v in pairs if h is not None and v is not None])
    return rounds


def _playing_days(first: datetime.date, rest_weekday: int) -> Iterator[datetime.date]:
    day = first
    while True:
        if day.weekday() != rest_weekday:
            yield day
        day += datetime.timedelta(days=1)


def _days_between(first: datetime.date, end: datetime.date, rest_weekday: int) -> list[datetime.date]:
    """first 以上 end 未満の試合日。"""
    return [
        d
        for d in (first + datetime.timedelta(days=k) for k in range(max((end - first).days, 0)))
        if d.weekday() != rest_weekday
    ]


# --- 内部: リーグ内 -----------------------------------------------------------


@dataclass
class _IntraPlan:
    """1リーグ分のリーグ内の日程の計画。組(slot)を順に消化して日に載せる。"""

    rounds: list[list[_Pair]]
    lengths: list[int]
    flip: int
    extra_pairs: frozenset[frozenset[int]]  # 余りの1試合を持つカード
    extra_cycle: int | None  # その1試合を足す周
    slots: list[tuple[int, int]]  # (周の番号, 組の番号)
    next_slot: int = 0

    def _length(self, cycle: int, pair: _Pair) -> int:
        longer = cycle == self.extra_cycle and frozenset(pair) in self.extra_pairs
        return self.lengths[cycle] + (1 if longer else 0)

    def _span(self, slot: tuple[int, int]) -> int:
        cycle, r = slot
        return max(self._length(cycle, pair) for pair in self.rounds[r])

    def remaining_span(self) -> int:
        return sum(self._span(slot) for slot in self.slots[self.next_slot :])

    def consume(self, days: Sequence[datetime.date]) -> list[Fixture]:
        """days に収まる範囲で、次の組から順に日に載せる（組を途中で割らない）。"""
        out: list[Fixture] = []
        used = 0
        while self.next_slot < len(self.slots):
            slot = self.slots[self.next_slot]
            span = self._span(slot)
            if used + span > len(days):
                break
            cycle, r = slot
            reverse = (cycle + self.flip) % 2 == 1
            for home, visitor in self.rounds[r]:
                if reverse:
                    home, visitor = visitor, home
                length = self._length(cycle, (home, visitor))
                out.extend(Fixture(d, home, visitor) for d in days[used : used + length])
            used += span
            self.next_slot += 1
        return out


def _circulant_extra_pairs(order: list[int], degree: int) -> frozenset[frozenset[int]]:
    """order の球団を輪に並べて、各球団が degree 本の辺を持つ正則グラフを作る。"""
    s = len(order)
    pairs = set()
    for i in range(s):
        for offset in range(1, degree // 2 + 1):
            pairs.add(frozenset((order[i], order[(i + offset) % s])))
        if degree % 2:
            pairs.add(frozenset((order[i], order[(i + s // 2) % s])))
    return frozenset(pairs)


def _plan_intra(teams: list[int], games: int, rules: ScheduleRules, rng: RandomSource) -> _IntraPlan:
    s = len(teams)
    if s == 1:
        if games:
            raise InvalidSchedule(f"球団が1つのリーグでは、リーグ内の試合が{games}試合になり組めません。")
        return _IntraPlan([], [], 0, frozenset(), None, [])
    base, extra = divmod(games, s - 1)
    if base < 1:
        raise InvalidSchedule(f"リーグ内の試合数（{games}）が相手の数（{s - 1}）より少なく、総当たりになりません。")
    if (s * extra) % 2:
        raise InvalidSchedule(
            f"球団数{s}のリーグでは、リーグ内の{games}試合を相手ごとに1試合差までで配れません（余り{extra}）。球団数か年間試合数を変えてください。"
        )
    lengths = _series_lengths(base, rules.series_length, rng)
    flip = int(rng.random() * 2) % 2
    order = list(teams)
    _shuffle(order, rng)
    extra_cycle = None
    extra_pairs: frozenset[frozenset[int]] = frozenset()
    if extra:
        even_sum = sum(lengths[0::2])
        candidates = [c for c in range(len(lengths)) if even_sum + (1 if c % 2 == 0 else 0) in _allowed_home(base + 1)]
        if not candidates:
            raise InvalidSchedule("余りの試合を、ホームが偏らないように置けません。")
        extra_cycle = _pick(candidates, rng)
        extra_pairs = _circulant_extra_pairs(order, extra)
    padded: list[int | None] = [*order, None] if s % 2 else [*order]
    rounds = _round_robin(padded)
    odd_order = _odd_cycle_order(len(rounds))
    slots = [(cycle, odd_order[r] if cycle % 2 else r) for cycle in range(len(lengths)) for r in range(len(rounds))]
    return _IntraPlan(rounds, lengths, flip, extra_pairs, extra_cycle, slots)


# --- 内部: 交流戦 -------------------------------------------------------------


def _inter_block(first: list[int], second: list[int], rng: RandomSource) -> list[list[_Pair]]:
    """2リーグの総当たり。1組(round)ごとの (ホーム, ビジター)。1組=1回の連戦。

    小さい側の全球団が毎組当たり、大きい側の球団は順に当たる（球団数が同じなら全員が毎組）。
    ホームは組ごとに交互にする。
    """
    small, large = (list(first), list(second)) if len(first) <= len(second) else (list(second), list(first))
    _shuffle(small, rng)
    _shuffle(large, rng)
    small_first = int(rng.random() * 2) % 2
    size = len(large)
    rounds = []
    for r in range(size):
        small_home = (r + small_first) % 2 == 0
        pairs = []
        for i, a in enumerate(small):
            b = large[(i + r) % size]
            pairs.append((a, b) if small_home else (b, a))
        rounds.append(pairs)
    return rounds


def _build(
    teams: list[list[int]],
    index_pairs: list[tuple[int, int]],
    rules: ScheduleRules,
    start: datetime.date,
    rng: RandomSource,
) -> list[Fixture]:
    opponents: list[list[int]] = [[] for _ in teams]
    for a, b in index_pairs:
        opponents[a].append(b)
        opponents[b].append(a)
    plans = []
    for li, league in enumerate(teams):
        inter = rules.inter_games * sum(len(teams[o]) for o in opponents[li])
        if inter > rules.games_per_team:
            raise InvalidSchedule("交流戦の試合数が年間試合数を超えます。")
        plans.append(_plan_intra(league, rules.games_per_team - inter, rules, rng))

    fixtures: list[Fixture] = []
    if not index_pairs:
        for plan in plans:
            days = list(islice(_playing_days(start, rules.rest_weekday), plan.remaining_span()))
            fixtures.extend(plan.consume(days))
        return _sorted(fixtures)

    window_start = rules.interleague_start.in_year(start.year)
    window_end = rules.interleague_end.in_year(start.year)
    before = _days_between(start, window_start, rules.rest_weekday)
    for plan in plans:
        fixtures.extend(plan.consume(before))

    cursor = _playing_days(max(start, window_start), rules.rest_weekday)
    last_day = start
    for edges in _phases(index_pairs):
        blocks = [(edge, _inter_block(teams[edge[0]], teams[edge[1]], rng)) for edge in edges]
        phase_days = [next(cursor) for _ in range(max(len(rounds) for _, rounds in blocks) * rules.inter_games)]
        busy: dict[int, int] = {}
        for (a, b), rounds in blocks:
            for r, pairs in enumerate(rounds):
                series = phase_days[r * rules.inter_games : (r + 1) * rules.inter_games]
                fixtures.extend(Fixture(d, home, visitor) for d in series for home, visitor in pairs)
            busy[a] = busy[b] = len(rounds) * rules.inter_games
        for li, plan in enumerate(plans):
            fixtures.extend(plan.consume(phase_days[busy.get(li, 0) :]))
        last_day = phase_days[-1]
    if last_day > window_end:
        raise InvalidSchedule(f"交流戦が期間内（{window_end:%m月%d日}まで）に収まりません。開幕日を早めてください。")

    after = last_day + datetime.timedelta(days=1)
    for plan in plans:
        days = list(islice(_playing_days(after, rules.rest_weekday), plan.remaining_span()))
        fixtures.extend(plan.consume(days))
    return _sorted(fixtures)


def _sorted(fixtures: list[Fixture]) -> list[Fixture]:
    fixtures.sort(key=lambda f: (f.date, f.home_team_id))
    return fixtures
