"""見本のリーグの組み立て（W5 #81）。**Django を import しない純粋な関数**で、DB にも触れない。

ワークスペースを作るときに、その実データとして入れる小さいリーグを作る: 1リーグ・6球団（本拠地の球場つき）・
各25人（全員支配下）・1球団12試合（全36試合。開幕から約2週間）。保存は application（`WorkspaceSampleService`。T2）が
集約とリポジトリを通して行い、ここは保存しない値（`SampleLeague`）と試合（`Game`）を作るだけ。

- 乱数はワークスペースの key と年から作る（`sample_random`・試合ごとの `game_seed`）。ワークスペースの id は使わない
  ので、同じ key と年なら同じ見本になる。`random()` だけを使う（版をまたいで同じ列）。
- 選手の名前・プロフィール・加入は `virtual_players/veteran.py` の `draw_veteran`（実データへの投入と同じ規則）、
  背番号は `pennant/draft.py` の `draw_number` の番号の並び、背ネームは `generator.back_name`、
  能力は `simulation/samples.py` の `draw_ratings`（25人は全員1軍に入るので、平均は1軍の水準）。
  名簿は `club_plan.ensure_roster_playable` を通す（試合を組めない名簿を作らない）。
- 日程は `pennant/schedule.py` の `generate_schedule`（1リーグ・1球団12試合）、試合は `simulation/engine.py` の
  `simulate_game`、時刻と観客数は `simulation/game_day.py`。
- 能力は試合を作るのに使うだけで、保存しない（実データは能力を持たない。成績の出典は試合だけ）。
"""

from __future__ import annotations

import datetime
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from random import Random

from ..entities import Game
from ..pennant.club_plan import ClubMember, ensure_roster_playable
from ..pennant.draft import draw_number
from ..pennant.schedule import Fixture, ScheduleRules, default_opening_day, generate_schedule
from ..simulation.engine import simulate_game
from ..simulation.fatigue import PitchingHistory
from ..simulation.game_day import simulated_attendance, simulated_times
from ..simulation.manager import ClubRoster, SimBatter, SimPitcher
from ..simulation.randomness import game_seed, make_random
from ..simulation.ratings import AVERAGE_RATING, BatterRatings, PitcherRatings
from ..simulation.samples import BATTER_SD, PITCHER_SD, draw_ratings
from ..value_objects import (
    DEFAULT_REGISTERED_PLAYER_LIMIT,
    AcquisitionRoute,
    ContractStatus,
    ForeignPlayerRules,
    GameStage,
    Position,
    Profile,
    StadiumProfile,
)
from ..virtual_players import generator
from ..virtual_players.veteran import draw_veteran
from .names import SAMPLE_CLUBS, SAMPLE_LEAGUE_NAME

# 見本の乱数の元。ワークスペースの key と年でずらす（`sample_random`）
SAMPLE_SEED = 20261113
SAMPLE_TEAM_COUNT = 6
SAMPLE_PLAYERS_PER_TEAM = 25
SAMPLE_GAMES_PER_TEAM = 12
# 見本の試合の総数（1試合に2球団が出るので、球団の数 × 1球団の試合数 ÷ 2 = 36）
SAMPLE_GAME_COUNT = SAMPLE_TEAM_COUNT * SAMPLE_GAMES_PER_TEAM // 2
# 1球団の構成（登録位置ごとの人数）。投手は先発向き・救援向きを半分ずつ（`STARTER_PITCHERS` 人が先発向き）
SAMPLE_POSITIONS: Mapping[Position, int] = {
    Position.PITCHER: 12,
    Position.CATCHER: 2,
    Position.INFIELDER: 6,
    Position.OUTFIELDER: 5,
}
STARTER_PITCHERS = 6
# 1球団の外国人（支配下）。投手1人・野手（捕手を除く）1人で、外国人枠（NPB は計4人・投手3人・野手3人）の内側
SAMPLE_FOREIGN_PITCHERS = 1
SAMPLE_FOREIGN_BATTERS = 1
# 能力の平均。25人は全員1軍に入るので、球団全体（2軍の控えを含む）の平均（`samples.BATTER_MEAN`）ではなく、
# 1軍の水準（`ratings.AVERAGE_RATING`。`samples.py` の注記のとおり 50）に合わせる
SAMPLE_RATING_MEAN = float(AVERAGE_RATING)
# 投手のスタミナの平均（先発向き・救援向き。`samples.spread_club` と同じ水準）
STARTER_STAMINA_MEAN = 62.0
RELIEVER_STAMINA_MEAN = 38.0
STAMINA_SD = 10.0
# 日程の規則（1リーグなので交流戦は無い。休みは月曜）。1球団12試合は5つの相手に2〜3試合ずつで、標準の3連戦では
# ホームの偏らない分け方が無い（`generate_schedule` が InvalidSchedule）ので、連戦の標準の長さを2にする
SAMPLE_SERIES_LENGTH = 2
SAMPLE_SCHEDULE_RULES = ScheduleRules(games_per_team=SAMPLE_GAMES_PER_TEAM, series_length=SAMPLE_SERIES_LENGTH)
# 開幕日から最後の試合までの日数の上限。`sample_year` はこれを過ぎた年を選ぶ（試合がすべて過去の日付になる）
SAMPLE_SEASON_DAYS = 21
SAMPLE_FOREIGN_RULES = ForeignPlayerRules.NPB


@dataclass(frozen=True)
class SampleStadium:
    name: str
    profile: StadiumProfile


@dataclass(frozen=True)
class SamplePlayer:
    """見本の選手ひとり。全員支配下。背ネームは `profile.back_name`。能力は試合を作るためだけに使う（保存しない）。"""

    name: str
    number: str
    position: Position
    from_year: int
    acquired_via: AcquisitionRoute
    profile: Profile
    ratings: BatterRatings | PitcherRatings

    @property
    def contract(self) -> ContractStatus:
        return ContractStatus.REGISTERED

    @property
    def is_foreign(self) -> bool:
        return self.profile.is_foreign_player


@dataclass(frozen=True)
class SampleTeam:
    name: str
    stadium: SampleStadium
    players: tuple[SamplePlayer, ...]


@dataclass(frozen=True)
class SampleLeague:
    """見本のリーグ（保存しない値）。外国人枠と支配下の登録枠は NPB の既定。"""

    name: str
    year: int
    teams: tuple[SampleTeam, ...]
    foreign_rules: ForeignPlayerRules = SAMPLE_FOREIGN_RULES
    registered_player_limit: int | None = DEFAULT_REGISTERED_PLAYER_LIMIT

    @property
    def stadiums(self) -> tuple[SampleStadium, ...]:
        return tuple(team.stadium for team in self.teams)


def opening_day(year: int) -> datetime.date:
    """見本の開幕日（`schedule.default_opening_day`。3月25日以降で最初の金曜）。"""
    return default_opening_day(year, SAMPLE_SCHEDULE_RULES)


def sample_year(today: datetime.date) -> int:
    """見本の年。試合がすべて `today` より前の日付になる年（今年の見本の期間が終わっていなければ前年）。"""
    if today > opening_day(today.year) + datetime.timedelta(days=SAMPLE_SEASON_DAYS):
        return today.year
    return today.year - 1


def sample_random(key: str, year: int) -> Random:
    """見本の名簿の乱数源。ワークスペースの key と年から作る（同じ key と年なら同じ列）。"""
    return make_random(game_seed(SAMPLE_SEED, year, f"sample:{key}"))


def _positions() -> list[Position]:
    """1球団の登録位置の並び（投手は先頭の `STARTER_PITCHERS` 人が先発向き）。"""
    return [position for position, count in SAMPLE_POSITIONS.items() for _ in range(count)]


def draw_sample_league(key: str, year: int) -> SampleLeague:
    """見本のリーグを作る（純粋。同じ key と年なら同じ値）。名簿はどの球団も `ensure_roster_playable` を通る。"""
    rng = sample_random(key, year)
    as_of = opening_day(year)
    used_names: set[str] = set()
    teams = []
    for club in SAMPLE_CLUBS:
        stadium = SampleStadium(
            club.stadium,
            StadiumProfile(
                city=club.city,
                capacity=club.capacity,
                surface=club.surface,
                roof=club.roof,
                opened_year=club.opened_year,
            ),
        )
        players = _draw_players(rng, as_of=as_of, used_names=used_names)
        ensure_roster_playable(
            club.team,
            [
                ClubMember(player_id=index, name=p.name, position=p.position, counts_as_foreign=p.is_foreign)
                for index, p in enumerate(players)
            ],
            foreign_rules=SAMPLE_FOREIGN_RULES,
        )
        teams.append(SampleTeam(club.team, stadium, players))
    return SampleLeague(name=SAMPLE_LEAGUE_NAME, year=year, teams=tuple(teams))


def _draw_players(rng: Random, *, as_of: datetime.date, used_names: set[str]) -> tuple[SamplePlayer, ...]:
    """1球団の25人。乱数は 外国人の枠 → 選手ごとに（名前とプロフィール → 背番号 → 能力）の順。"""
    positions = _positions()
    pitchers = [index for index, position in enumerate(positions) if position.is_pitcher]
    # 外国人の野手は捕手にしない（捕手の外国人は稀）
    fielders = [
        index for index, position in enumerate(positions) if position in (Position.INFIELDER, Position.OUTFIELDER)
    ]
    foreign = {
        *_pick_distinct(rng, pitchers, SAMPLE_FOREIGN_PITCHERS),
        *_pick_distinct(rng, fielders, SAMPLE_FOREIGN_BATTERS),
    }

    used_numbers: set[str] = set()
    draft_rounds: Counter[tuple[int, bool]] = Counter()
    drawn = []
    for index, position in enumerate(positions):
        veteran = draw_veteran(
            rng,
            position,
            contract=ContractStatus.REGISTERED,
            is_foreign=index in foreign,
            as_of=as_of,
            used_names=used_names,
            draft_rounds=draft_rounds,
        )
        number = draw_number(rng, used_numbers)
        if position.is_pitcher:
            starter = index < STARTER_PITCHERS
            ratings = draw_ratings(
                rng,
                mean=SAMPLE_RATING_MEAN,
                sd=PITCHER_SD,
                position=position,
                stamina_mean=STARTER_STAMINA_MEAN if starter else RELIEVER_STAMINA_MEAN,
                stamina_sd=STAMINA_SD,
            )
        else:
            ratings = draw_ratings(rng, mean=SAMPLE_RATING_MEAN, sd=BATTER_SD, position=position)
        drawn.append((veteran, number, ratings))

    surnames = Counter(veteran.surname_romaji for veteran, _, _ in drawn)
    players = []
    for veteran, number, ratings in drawn:
        back_name = generator.back_name(
            veteran.surname_romaji, veteran.given_romaji, shares_surname=surnames[veteran.surname_romaji] > 1
        )
        players.append(
            SamplePlayer(
                name=veteran.name,
                number=number,
                position=veteran.position,
                from_year=veteran.from_year,
                acquired_via=veteran.acquired_via,
                profile=replace(veteran.profile, back_name=back_name),
                ratings=ratings,
            )
        )
    return tuple(players)


def _pick_distinct(rng: Random, items: Sequence[int], count: int) -> list[int]:
    """`items` から重ならずに `count` 個選ぶ（`random()` だけを使う）。"""
    remaining = list(items)
    return [remaining.pop(generator.randint(rng, 0, len(remaining) - 1)) for _ in range(count)]


def sample_club(team: SampleTeam, *, team_id: int, player_ids: Sequence[int]) -> ClubRoster:
    """保存した見本の球団を、試合を回す型にする。`player_ids` は `team.players` と同じ並びの保存後の id。

    25人は全員1軍（`ACTIVE_ROSTER_SIZE` 以内）。外国人枠で数えるのは外国人の選手。
    """
    if len(player_ids) != len(team.players):
        raise ValueError("選手の id の数が名簿の人数と合いません。")
    batters = []
    pitchers = []
    for player_id, player in zip(player_ids, team.players, strict=True):
        if isinstance(player.ratings, PitcherRatings):
            pitchers.append(SimPitcher(player_id, player.name, player.ratings, counts_as_foreign=player.is_foreign))
        else:
            batters.append(
                SimBatter(
                    player_id,
                    player.name,
                    player.position,
                    player.ratings,
                    counts_as_foreign=player.is_foreign,
                    throws=player.profile.throws,
                )
            )
    return ClubRoster(team_id=team_id, name=team.name, batters=tuple(batters), pitchers=tuple(pitchers))


def sample_fixtures(team_ids: Sequence[int], *, key: str, year: int) -> list[Fixture]:
    """見本の日程（1リーグ・1球団12試合。開幕は `opening_day(year)`）。乱数は key と年から（日程の用途の接尾辞）。

    `generate_schedule` で組む（連戦の標準の長さを2にすれば、1リーグ6球団・12試合を組める。T1 で確かめた）。
    """
    rng = make_random(game_seed(SAMPLE_SEED, year, f"sample:{key}:schedule"))
    return generate_schedule({0: list(team_ids)}, SAMPLE_SCHEDULE_RULES, opening_day(year), rng, season=year)


def play_sample_games(
    clubs: Sequence[ClubRoster],
    fixtures: Sequence[Fixture],
    *,
    key: str,
    year: int,
    foreign_rules: ForeignPlayerRules,
    capacities: Mapping[int, int | None],
) -> list[Game]:
    """日程を日付順に消化して、試合（打席と明細まで組み立て済み）を返す。保存はしない。

    登板の疲労（`PitchingHistory`）は試合をまたいで引き継ぐ。試合ごとの乱数は key・年・試合の識別（日付と、
    `clubs` の並びでのホームとビジターの位置）から作るので、保存した球団の id が違っても同じ試合になる。
    時刻と観客数は試合の結果とは別の乱数源（用途の接尾辞付き）で作る（`pennant_simulation` と同じ分け方）。
    `capacities` はホーム球団の id → 本拠地の収容人数。
    """
    by_id = {club.team_id: club for club in clubs}
    order = {club.team_id: index for index, club in enumerate(clubs)}
    history = PitchingHistory()
    games = []
    for fixture in sorted(fixtures, key=lambda f: (f.date, order[f.home_team_id])):
        home, away = by_id[fixture.home_team_id], by_id[fixture.visitor_team_id]
        game_key = f"sample:{key}:{fixture.date.isoformat()}:{order[home.team_id]}:{order[away.team_id]}"
        simulated = simulate_game(
            make_random(game_seed(SAMPLE_SEED, year, game_key)),
            home,
            away,
            played_on=fixture.date,
            stage=GameStage.REGULAR,
            history=history,
            foreign_rules=foreign_rules,
        )
        day_rng = make_random(game_seed(SAMPLE_SEED, year, f"{game_key}:game-day"))
        simulated.game.times = simulated_times(simulated.game, day_rng)
        simulated.game.attendance = simulated_attendance(capacities.get(home.team_id), fixture.date, day_rng)
        games.append(simulated.game)
    return games
