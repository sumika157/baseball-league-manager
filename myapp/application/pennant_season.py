"""ペナントのシーズン進行。日程を作り、日を進めて試合を作る。

**進行の状態は保存しない。** 「今日」は消化した最後の試合日、シーズン中かは未消化の対戦が残っているか、
で日程（`FixtureRepository`）と試合から導く。カーソルを持つと、日程・試合と食い違いうる。

1日ずつ進めても1週間まとめて進めても、**同じ結果になる**。次の4つがそろっているため。

- 試合ごとの乱数は、世界のシード・年・試合の識別（日付とホーム・ビジター）から作る
  （進めた順序や回数に依存しない。`randomness.game_seed`）
- 疲労（先発の間隔・連投）は保存せず、直近の登板から導く。まとめて進めるときは、読んだ登板に
  その場で作った試合の登板を足していく
- 1軍登録・スタメン・継投は、能力と GM の編成の上書き（`ClubPlan`）だけから決まる。上書きが使えない区画は
  その区画だけ自動に落とし、理由を `AdvanceReport.plan_notices` で返す（例外で止めない）
- AI の期中の補強（昇格は #170 P3b、外国人の獲得は #169 F3）は判定日（6月1日・7月1日）にだけ決め、判定日以降の
  最初の試合日の前に来る（範囲を判定日の前で区切る。`reinforcement.split_at_reviews`）。材料は判定日の状態だけ
  （`MidseasonReview`）

試合を1つずつ作る部分（上の3つ）は `pennant_simulation.DaySimulator` にあり、ポストシーズンの進行
（`PennantPostseasonService`）と共有する。

試合は日の区切りで、`FLUSH_EVERY_GAMES` 試合ごとにまとめて保存する（メモリに溜める量の上限）。
**日の途中では切らない**。残りの対戦が「今日」より前になり、進められなくなるため。
保存と消化済みの対戦の削除は同じトランザクションで行う。判定日をまたぐときは、判定日の前の日までを書き出してから
AI の昇格と外国人の獲得を書き、登録候補を読み直して続ける。

**書くときに在籍の目印（`TeamRepository.roster_signature`）を確かめる。** 登録候補を読んだ後に名簿が変わっていたら
（進めている間に GM が期中の登録をした。#170 P3a の申し送り）、古い名簿で試合を書かずに `AlreadyAdvanced`。
同じく GM が手動にしたチケットの値段（#208 E2）も読み直し、読んだ後に変わっていたら（開幕前に値段を変えるのと
最初の試合を書くのが重なった）、古い値段の観客数で試合を書かずに `AlreadyAdvanced`（値段を変えられるのはその年の
試合が無い間だけなので、最初の書き出しの後は変わらない）。
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import date
from types import EllipsisType
from typing import TYPE_CHECKING

from ..domain.entities import Game
from ..domain.exceptions import AlreadyAdvanced, InvalidSchedule
from ..domain.pennant.reinforcement import split_at_reviews
from ..domain.pennant.schedule import (
    AdvanceTarget,
    Fixture,
    dates_to_play,
    season_schedule,
)
from ..domain.pennant.world import World
from ..domain.repositories import (
    ClubPlanRepository,
    FixtureRepository,
    GameRepository,
    LeagueRepository,
    RatingsRepository,
    TeamRepository,
    WorldRepository,
)
from ..domain.value_objects import GameStage
from .dto import AdvanceReport, PlanNotice
from .foreign_quota import ForeignQuotaExemptions
from .pennant_popularity import PopularityReader
from .pennant_simulation import DaySimulator
from .pennant_ticket_price import TicketPriceReader
from .queries import SimulationContextQuery

if TYPE_CHECKING:
    # 協力者は AtomicBlock をここから読むので、実行時には import しない（循環を避ける）
    from .pennant_midseason_review import MidseasonReview

# メモリに溜める試合数の上限（`seed_virtual_games` の `FLUSH_EVERY_GAMES` に倣う）
FLUSH_EVERY_GAMES = 200

# 保存と消化済みの対戦の削除を1つにまとめる。実装は組み立ての1か所が渡す（`transaction.atomic`）
AtomicBlock = Callable[[], AbstractContextManager[object]]


def _saved_id(value: int | None) -> int:
    """保存済みの集約から取り出す id。永続化された後は必ず値がある。"""
    assert value is not None, "保存済みの集約には id がある"
    return value


class PennantSeasonService:
    """ペナントの世界ひとつのシーズン進行。リポジトリと参照クエリは、その世界の範囲で組み立てて渡す。"""

    def __init__(
        self,
        *,
        world_id: int,
        worlds: WorldRepository,
        leagues: LeagueRepository,
        teams: TeamRepository,
        games: GameRepository,
        fixtures: FixtureRepository,
        ratings: RatingsRepository,
        plans: ClubPlanRepository,
        context_query: SimulationContextQuery,
        fa_exemptions: ForeignQuotaExemptions,
        popularities: PopularityReader,
        ticket_prices: TicketPriceReader,
        review: MidseasonReview,
        atomic: AtomicBlock,
    ) -> None:
        self._world_id = world_id
        self._worlds = worlds
        self._leagues = leagues
        self._teams = teams
        self._games = games
        self._fixtures = fixtures
        self._ratings = ratings
        self._plans = plans
        self._context_query = context_query
        # FA 資格の取得による外国人枠の対象外（協約 第82条 (4)）。1回の「進める」で1回だけ導く（`DaySimulator.load`）
        self._fa_exemptions = fa_exemptions
        # 球団の人気（ホーム試合の観客数。#208 E1）
        self._popularities = popularities
        # 球団のチケットの値段（ホーム試合の観客数。#208 E2）
        self._ticket_prices = ticket_prices
        self._atomic = atomic
        # 判定日の AI の期中の補強（昇格は #170 P3b、外国人の獲得は #169 F3）
        self._review = review

    def ensure_schedule(self) -> bool:
        """その年の日程が無ければ作って保存する。作ったかどうかを返す。

        作るのは世界の開幕年の日程だけで、**日程も試合もまだ無い世界**のときに限る（シーズンが終わって
        日程が空の世界に、日程を作り足さない。翌年の日程はシーズンを締めるときに作る）。
        同じ世界なら、いつ呼んでも同じ日程になる（乱数は世界のシードから作る）。
        """
        if self._fixtures.find_all() or self._context_query.last_played_on() is not None:
            return False
        world = self._worlds.find_by_id(self._world_id)
        teams = self._teams.find_all()
        leagues = {
            _saved_id(league.id): sorted(_saved_id(team.id) for team in teams if team.league_id == league.id)
            for league in self._leagues.find_all()
        }
        schedule = season_schedule(leagues, world_seed=world.seed, year=world.start_year)
        with self._atomic():
            self._fixtures.add_all(schedule)
        return True

    def advance(
        self,
        target: AdvanceTarget,
        *,
        max_games: int | None = None,
        expected_today: date | None | EllipsisType = ...,
    ) -> AdvanceReport:
        """日程を `target` の分だけ進める。未消化の対戦ごとに試合を作って保存し、その対戦を消す。

        日程がまだ無い世界は、先に開幕年の日程を作る。未消化が無ければ何もせず、空の結果を返す。
        試合ができなくなる世界（打順を組む野手がいない球団など）は InvalidRoster で、書き出し済みの日は
        そのまま残る（日の区切りで書き出すので、「今日」が中途半端にならない。まだ書き出していない
        直近の日（最大 `FLUSH_EVERY_GAMES` 試合ぶん）は捨てられ、次に進めるときに同じ結果で作り直される）。

        `max_games` を渡すと、今回の範囲の試合数がそれを超えるときは何も作らずに InvalidSchedule にする
        （画面から進めるときの上限。管理コマンドは渡さない）。

        `expected_today` を渡すと、世界の今日（消化した最後の試合日。まだ無ければ None）がそれと違うときは
        何も作らずに AlreadyAdvanced にする（画面を開いたあとに別の操作で進んだ世界を、さらに進めない。
        管理コマンドは渡さない）。
        """
        if target is AdvanceTarget.POSTSEASON_END:
            # 日程を作る前に断る（検査を先に。`dates_to_play` も同じ範囲を弾く）
            raise InvalidSchedule("ポストシーズンの終わりまでの範囲は、公式戦の日程には使えません。")
        world = self._worlds.find_by_id(self._world_id)
        last_played = self._context_query.last_played_on()
        if expected_today is not ... and expected_today != last_played:
            raise AlreadyAdvanced("既に進んでいます。最新の状態を表示しました。")
        created = self.ensure_schedule()
        pending = self._fixtures.find_all()
        dates = dates_to_play(
            pending, last_played if last_played is not None else date.min, target, world.managed_team_id
        )
        if not dates:
            return AdvanceReport(
                played_dates=(), games=0, today=last_played, remaining_fixtures=len(pending), schedule_created=created
            )
        if max_games is not None:
            days = set(dates)
            wanted = sum(1 for fixture in pending if fixture.date in days)
            if wanted > max_games:
                # 1回で作る試合が多すぎると応答が返らない。何も作らずに断る
                raise InvalidSchedule(
                    f"一度に進められるのは{max_games}試合までです（この範囲は{wanted}試合あります）。"
                )

        played, notices = self._play(world, pending, dates, last_played)
        return AdvanceReport(
            played_dates=tuple(dates),
            games=played,
            today=dates[-1],
            remaining_fixtures=len(pending) - played,
            schedule_created=created,
            plan_notices=notices,
        )

    # --- 内部 ---

    def _play(
        self, world: World, pending: list[Fixture], dates: list[date], last_played: date | None
    ) -> tuple[int, tuple[PlanNotice, ...]]:
        """`dates` の日の対戦をシミュレーションして保存し、作った試合数と、自動に落ちた編成の知らせを返す。

        AI の期中の補強の判定日（`ReinforcementWindow.review_dates`）をまたぐときは、判定日の前で区切る
        （`split_at_reviews`）: 判定日の前の日までを書き出し、AI の昇格と外国人の獲得を決めて書き
        （`MidseasonReview`）、登録候補を読み直してから続ける（上げた・獲得した選手は判定日以降の最初の試合から
        出られる）。区切りは日付だけで決まるので、1日ずつ進めてもまとめて進めても、判定は同じ試合日の前に来る。
        """
        by_date: dict[date, list[Fixture]] = {}
        for fixture in pending:
            by_date.setdefault(fixture.date, []).append(fixture)

        total = 0
        notices: list[PlanNotice] = []
        for segment in split_at_reviews(dates, last_played=last_played):
            first_day = segment.dates[0]
            simulator, signature = self._load(world, first_day)
            # 判定は区切りの前に1回（2つの判定日の間に試合日が無くても、同じ登録日に2人目は上げない・獲得しない）
            if (
                segment.reviews
                and self._review.run(
                    world=world, on=first_day, simulator=simulator, last_played=last_played, signature=signature
                ).changed
            ):
                # 上げた・獲得した選手を登録候補に入れる（読み直す）
                simulator, signature = self._load(world, first_day)
            games: list[Game] = []
            consumed: list[Fixture] = []
            for day in segment.dates:
                for fixture in by_date[day]:
                    games.append(
                        simulator.play(day, fixture.home_team_id, fixture.visitor_team_id, stage=GameStage.REGULAR)
                    )
                    consumed.append(fixture)
                # 日の途中では書き出さない（残りの対戦が「今日」より前になり、進められなくなる）
                if len(games) >= FLUSH_EVERY_GAMES:
                    total += self._flush(games, consumed, signature, simulator)
                    games, consumed = [], []
            total += self._flush(games, consumed, signature, simulator)
            last_played = segment.dates[-1]
            # 区切りごとに読み直すので、前の区切りで知らせた球団・区画は重ねない（最初の日の1回だけ）
            seen = {(notice.team_id, notice.section) for notice in notices}
            notices.extend(notice for notice in simulator.notices if (notice.team_id, notice.section) not in seen)
        return total, tuple(notices)

    def _load(self, world: World, first_day: date) -> tuple[DaySimulator, tuple[int, int, int, int]]:
        """`first_day` から進めるための材料と、読む前の在籍の目印（書くときに名簿が変わっていないかを確かめる）。"""
        signature = self._teams.roster_signature()
        # 能力を引く年は `first_day` の年。`first_day` は最後の試合日より後の未消化の最初の日。消化した日程は
        # 試合の保存と同じトランザクションで消えるので、日程の最初の日（`FixtureRepository.first_date`）と同じになり、
        # domain の `ratings_year`（編成・表示が使う規則）と一致する
        simulator = DaySimulator.load(
            world=world,
            first_day=first_day,
            context_query=self._context_query,
            ratings=self._ratings,
            plans=self._plans,
            fa_exemptions=self._fa_exemptions,
            popularities=self._popularities,
            ticket_prices=self._ticket_prices,
        )
        return simulator, signature

    def _flush(
        self,
        games: list[Game],
        consumed: list[Fixture],
        signature: tuple[int, int, int, int],
        simulator: DaySimulator,
    ) -> int:
        """試合を保存し、消化した対戦を消す。同じトランザクションで行う。

        試合の検査と行の組み立ては**トランザクションの外**で済ませる（CPU の仕事で、書き込みロックを
        持つ必要が無い。本番の SQLite は書き手が1つで、`BEGIN IMMEDIATE` から他の書き込みを止める）。
        ロックを持つのは、対戦を消す SQL と行を書く SQL を流す間だけ。

        中で在籍の目印を確かめ、登録候補を読んだ後に名簿が変わっていたら（進めている間に GM が期中の登録をした）
        書かずに `AlreadyAdvanced`（登録した選手が出られるはずの試合を、古い名簿で作らない。書き出し済みの日は残る）。
        GM が手動にしたチケットの値段も読み直し、読んだ後に変わっていたら同じく `AlreadyAdvanced`（#208 E2。
        古い値段で観客数を作った試合を書かない）。
        """
        if not games:
            return 0
        write = self._games.prepare_add_all(games)
        with self._atomic():
            if self._teams.roster_signature() != signature:
                raise AlreadyAdvanced("進めている間に選手の登録が変わりました。最新の状態からもう一度進めてください。")
            if self._ticket_prices.manual(consumed[0].date.year) != simulator.manual_ticket_prices:
                raise AlreadyAdvanced(
                    "進めている間にチケットの値段が変わりました。最新の状態からもう一度進めてください。"
                )
            # 先に消す。すでに消化された対戦があれば InvalidSchedule で、試合を書かずに止まる
            # （同じ世界を同時に進めて、同じ対戦の試合が2つできるのを防ぐ）
            self._fixtures.remove(consumed)
            write()
        return len(games)
