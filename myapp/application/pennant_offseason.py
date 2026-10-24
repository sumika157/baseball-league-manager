"""ペナントのシーズンを締める（オフ）。引退・戦力外・ドラフト・成長と衰え・編成の後始末・翌年の日程を、1つの操作にまとめる。

**進行（`PennantSeasonService`）には足さない。** 進行は試合を作って保存する処理で、こちらは集約（`Team`）の
書き換えと能力・日程の一括保存で、依存も検査も違う。

締めたかどうかは保存しない。「次の対戦が翌年にある」「翌年の能力がある」という事実から導く
（`PennantSeasonService` の進行と同じく、状態を持たない）。

処理は「検査 → 読む → 計算する（`plan_offseason`。DB に触れない）→ 書く」。
**読む・計算するはトランザクションの外**で行い
（本番の SQLite は書き手が1つで、中にいる間は他の書き込みを止める。8リーグの10シーズン目で 6秒）、
**中では、最後の試合日と次の対戦の日が読み込みの前と変わっていないこと・翌年の能力が無いことを読み直してから書く**。
変わっていたら（外で計算している間に別のリクエストが締めた・進めた）、書かずに既存の例外
（`AlreadyClosed` / `SeasonNotFinished`）で断る。編成（`ClubPlan`）は GM が保存しうるので、書く直前に中で読む。
受け持つ球団の戦力外の指名（`ReleasePlan`）も GM が保存しうるが、こちらは計算に使うので `_state()` に入れて前後で比べる
（変わっていたら断る）。指名を決める操作（`set_releases` など）もこのサービスが持つ
（指名できるかの検査 `_season_over_year` と、それにポストシーズンの検査を足した締められるかの検査 `_check` を
1か所に保つ。指名は公式戦を終えればポストシーズン中もできる）。

締める年のタイトル受賞者は**表彰として保存する**（#248。CLAUDE.md の「集計を保存しない」の例外）。受賞者は外で
タイトルの画面と同じ集計から導き（`pennant_awards.derive_awards`。8リーグで 0.6〜2.9秒）、中では書くだけ。
戦力外の実績の加点に使うタイトルの数は、締めた年までの表彰と、いま導いた締める年の受賞を足す（過去の年を導き直さない）。
締める年のポストシーズンの優勝球団（日本一）も**記録として保存する**（#242。表彰と同じ例外）。勝ち上がりは外で1回だけ導き、
締められるかの検査（ポストシーズンが終わったか）と、書く優勝球団の両方に使う。

**前提: 世界の `Team` 集約に書くのは、世界の作成とこの処理だけ。** 外で読んだ `Team` を中でそのまま
`save()` して安全なのは、この前提による（`save()` は変わった行だけを書くが、比べる相手は今の DB なので、
読んだあとに別の書き込みがあれば、集約の古い値が「違う」として書かれ、その書き込みを上書きして失う）。念のため、在籍の目印（`TeamRepository.roster_signature()`。行数・現在在籍の人数・
最大の id）も読み込みの前後で比べ、変わっていれば書かずに断る。
**世界の中で選手を書く操作を足すなら、その操作が `_state()` に現れるか、ここで中に読み直すこと。**

同時に2回呼ばれても、翌年の能力の一意制約（選手 × 年）で後から来た方が
失敗し、新人や在籍の変更ごと巻き戻る（`add_all` が保存前に既存の能力を検査して `InvalidRatings` を出し、
それもすり抜けたら DB の一意制約）。**この2段が最後の砦**。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date

from ..domain.entities import Player, Team
from ..domain.exceptions import (
    AlreadyClosed,
    PostseasonNotFinished,
    SeasonLimitReached,
    SeasonNotFinished,
    TeamNotFound,
)
from ..domain.pennant.awards import Award, title_counts
from ..domain.pennant.championship import Championship
from ..domain.pennant.club_plan import ClubMember, PlanSection
from ..domain.pennant.offseason import Draftee, OffseasonClub, OffseasonPlan, OffseasonPlayer, plan_offseason
from ..domain.pennant.offseason_player import TrackRecord
from ..domain.pennant.postseason import PostseasonFormat, PostseasonStatus
from ..domain.pennant.ratings import PlayerRatings
from ..domain.pennant.release import ReleasePlan, ensure_releasable
from ..domain.pennant.retirement import PlayingTime
from ..domain.pennant.schedule import Fixture, season_schedule
from ..domain.pennant.season import is_final_season, is_season_closed, regular_season_over_year
from ..domain.pennant.world import World
from ..domain.repositories import (
    AwardRepository,
    ChampionshipRepository,
    ClubPlanRepository,
    FixtureRepository,
    LeagueRepository,
    RatingsRepository,
    ReleasePlanRepository,
    TeamRepository,
    WorldRepository,
)
from ..domain.value_objects import ContractStatus, DepartureRoute, ForeignPlayerRules, JerseyNumber, RosterLimits
from .dto import ReleasePlanView, ReleasePlayerRow, SeasonClosed, SeasonCloseOption, SimulationTeam
from .pennant_awards import derive_awards
from .pennant_postseason import PostseasonBracketReader
from .pennant_season import AtomicBlock
from .queries import SeasonPlayingTimeQuery, SimulationContextQuery
from .services import TeamApplicationService


def _saved_id(value: int | None) -> int:
    """保存済みの集約から取り出す id。永続化された後は必ず値がある。"""
    assert value is not None, "保存済みの集約には id がある"
    return value


@dataclass
class _RosterChange:
    """メモリの上で引退・戦力外・新人を反映した球団（保存はまだ）。`added` と `draftees` は同じ並び。"""

    team: Team
    added: list[Player]
    draftees: list[Draftee]


@dataclass
class _PreparedClose:
    """トランザクションの外で読んで計算した、締める処理の書く内容。"""

    plan: OffseasonPlan
    changes: list[_RosterChange]
    schedule: Sequence[Fixture]
    year: int
    # 締める年のタイトル受賞者（表彰として書く。ロックの外で導いた値）
    awards: list[Award]
    # 締める年の日本一（記録として書く。ロックの外で導いた勝ち上がりから `close_season` が入れる）。無い年は None
    championship: Championship | None = None


class PennantOffseasonService:
    """ペナントの世界ひとつのオフ。リポジトリと参照クエリは、その世界の範囲で組み立てて渡す。"""

    def __init__(
        self,
        *,
        world_id: int,
        worlds: WorldRepository,
        leagues: LeagueRepository,
        teams: TeamRepository,
        fixtures: FixtureRepository,
        ratings: RatingsRepository,
        plans: ClubPlanRepository,
        releases: ReleasePlanRepository,
        awards: AwardRepository,
        championships: ChampionshipRepository,
        titles: TeamApplicationService,
        context_query: SimulationContextQuery,
        playing_time: SeasonPlayingTimeQuery,
        postseason: PostseasonBracketReader,
        atomic: AtomicBlock,
    ) -> None:
        self._world_id = world_id
        self._worlds = worlds
        self._leagues = leagues
        self._teams = teams
        self._fixtures = fixtures
        self._ratings = ratings
        self._plans = plans
        self._releases = releases
        self._awards = awards
        self._championships = championships
        # タイトルの受賞者を導く（世界の範囲の、タイトルの画面と同じサービス）
        self._titles = titles
        self._context_query = context_query
        self._playing_time = playing_time
        # ポストシーズンが終わるまで締めない（その年の勝ち上がりを導いて状況を見る。方式「なし」は何も読まない）
        self._postseason = postseason
        self._atomic = atomic

    def close_option(self, *, postseason_status: PostseasonStatus | None = None) -> SeasonCloseOption:
        """いま締められるか。締められないときは理由を返す（例外にしない。画面のボタンの出し分けの材料）。

        `postseason_status` は呼び手（GM ホーム）が既に導いたその年のポストシーズンの状況。渡せば導き直さない
        （1回の表示で勝ち上がりを2回導かない）。**表示にだけ使う**: `close_season()` は必ず自分で導き直す。
        """
        world = self._worlds.find_by_id(self._world_id)
        try:
            year = self._check(world, expected_year=None, postseason_status=postseason_status)
        except (SeasonNotFinished, PostseasonNotFinished, AlreadyClosed, SeasonLimitReached) as error:
            last_played = self._context_query.last_played_on()
            blocked_year = None if last_played is None else last_played.year
            return SeasonCloseOption(
                year=blocked_year,
                next_year=None if blocked_year is None else blocked_year + 1,
                can_close=False,
                reason=str(error),
            )
        chosen = self._chosen_releases(self._managed_release_plan(), year)
        return SeasonCloseOption(
            year=year, next_year=year + 1, can_close=True, release_count=None if chosen is None else len(chosen)
        )

    def close_season(self, *, expected_year: int | None = None) -> SeasonClosed:
        """シーズンを締める。締める年は最後に試合をした年 Y で、翌年 Y+1 の在籍・能力・日程を作る。

        - 未消化の対戦が残っている、または試合を一度もしていない → `SeasonNotFinished`
        - 既に締めている、または `expected_year`（画面を開いたときの年）が Y と違う → `AlreadyClosed`
        - 世界の最後のシーズン → `SeasonLimitReached`
        - その年のポストシーズンが終わっていない（始まっていない・進行中） → `PostseasonNotFinished`

        どれも何も書かない。書き込みの途中で失敗したときも何も残さない（1つのトランザクション）。
        引退・戦力外の選手の在籍は Y で閉じ（退団の経路を記録する）、新人は Y+1 から加入する。
        **年を省略した在籍の操作は呼ばない**
        （省くと現実の今日の年が入る）。
        """
        # 読み込みと計算（1〜6秒）は、書き込みトランザクションの外で済ませる。本番の SQLite は書き手が1つで、
        # トランザクションの中にいる間は他の書き込みを止めるため。中では状態が変わっていないことだけを確かめ、書く
        world = self._worlds.find_by_id(self._world_id)
        state = self._state()
        year = self._season_over_year(world, expected_year=expected_year)
        # 勝ち上がりは1回だけ導き、締められるかの検査と、記録する日本一の両方に使う
        bracket = self._postseason.bracket(world, year)
        _ensure_postseason_settled(year, bracket.status)
        self._ensure_not_closed(world, year)
        champion = bracket.champion
        prepared = replace(
            self._prepare(world, year),
            championship=Championship(year=year, team_id=champion) if champion is not None else None,
        )
        with self._atomic():
            if self._state() != state:
                # 計算している間に、別のリクエストが進めた・締めた。古い読み込みを書かない。
                # いまの状態で検査し直し、それぞれの理由（未消化・既に締めた）で断る
                world = self._worlds.find_by_id(self._world_id)
                self._ensure_not_closed(world, self._check(world, expected_year=expected_year))
                raise AlreadyClosed("計算している間に世界の状態が変わりました。開き直してください。")
            self._ensure_not_closed(world, year)
            return self._commit(world, prepared)

    def _state(
        self,
    ) -> tuple[date | None, date | None, tuple[int, int, int], ReleasePlan | None, PostseasonFormat]:
        """締められるかと何を書くかを決める事実（最後に試合をした日・次の対戦の日・在籍の目印・受け持つ球団の戦力外の指名・
        ポストシーズンの方式）。

        読み込みの前後で比べる。戦力外の指名は GM が締める処理の計算中にも保存しうるので、ここに入れる（#100）。
        方式も設定の画面で計算中に変えうる（「なし」から変えたのに、その年のポストシーズンを行わずに締めない。#280）。
        """
        world = self._worlds.find_by_id(self._world_id)
        return (
            self._context_query.last_played_on(),
            self._fixtures.first_date(),
            self._teams.roster_signature(),
            self._release_plan_of(world.managed_team_id),
            world.postseason_format,
        )

    def _managed_release_plan(self) -> ReleasePlan | None:
        """受け持つ球団の戦力外の手動の指名（自動・受け持ちが無いなら None）。"""
        return self._release_plan_of(self._worlds.find_by_id(self._world_id).managed_team_id)

    def _release_plan_of(self, team_id: int | None) -> ReleasePlan | None:
        return None if team_id is None else self._releases.find_by_team(team_id)

    def _ensure_not_closed(self, world: World, year: int) -> None:
        if is_season_closed(
            year=year,
            start_year=world.start_year,
            next_year_ratings_exist=bool(self._ratings.find_by_year(year + 1)),
        ):
            raise AlreadyClosed(f"{year}年のシーズンは既に締めています。", year=year)

    # --- 表彰の記録の補い（#248 より前に締めた世界） ---

    def record_missing_awards(self) -> list[int]:
        """締めた年のうち表彰の無い年の受賞者を、締める処理と同じ関数で導いて書く。書いた年（古い順）を返す。

        表彰を記録するようになる前（#248 より前）に締めた世界のためのもの。締めていない年には書かない
        （締めていない年は画面が試合から導く）。何度呼んでも同じ（表彰のある年は飛ばす）。
        導くのは書き込みのトランザクションの外、書くのは中（締める処理と同じ）。
        """
        years = self.missing_award_years()
        if not years:
            return []
        league_ids = [_saved_id(league.id) for league in self._leagues.find_all()]
        awards = [award for year in years for award in derive_awards(self._titles, year, league_ids)]
        with self._atomic():
            # 同じ年を2度書かない（外で導いている間に別の呼び出しが書いた年は、`add_all` が断る）
            self._awards.add_all(awards)
        return years

    def missing_award_years(self) -> list[int]:
        """締めた年のうち、表彰の無い年（古い順）。何も書かない（`pennant_record_awards --check` が一覧する）。"""
        world = self._worlds.find_by_id(self._world_id)
        last_played = self._context_query.last_played_on()
        if last_played is None:
            return []
        recorded = self._awards.recorded_years()
        return [
            year
            for year in range(world.start_year, last_played.year + 1)
            if year not in recorded
            and is_season_closed(
                year=year,
                start_year=world.start_year,
                next_year_ratings_exist=bool(self._ratings.find_by_year(year + 1)),
            )
        ]

    # --- 戦力外の指名（受け持つ球団だけ。公式戦を終えてから締めるまで。ポストシーズン中も選べる） ---

    def release_view(self) -> ReleasePlanView:
        """編成画面の「戦力外」タブの材料。公式戦の途中・締めた後・最終シーズンは例外（ポストシーズン中は出す）。

        - `SeasonNotFinished` / `AlreadyClosed` / `SeasonLimitReached`: 指名できない（タブを出さない）
        - `TeamNotFound`: 受け持つ球団が無い
        """
        world = self._worlds.find_by_id(self._world_id)
        year = self._season_over_year(world, expected_year=None)
        self._ensure_not_closed(world, year)
        team_id = self._managed_team_id(world)
        team = _team_of(self._context_query.teams(), team_id)
        chosen = self._chosen_releases(self._releases.find_by_team(team_id), year)
        return ReleasePlanView(
            team_id=team_id,
            team_name=team.name,
            year=year,
            is_manual=chosen is not None,
            chosen_ids=chosen if chosen is not None else frozenset(),
            players=tuple(
                ReleasePlayerRow(player_id=p.player_id, name=p.name, position=p.position, is_foreign=p.is_foreign)
                for p in team.players
            ),
        )

    def can_choose_releases(self) -> bool:
        """「戦力外」タブを出せるか（`release_view` が例外にならないか）。支配下の一覧までは読まない。"""
        world = self._worlds.find_by_id(self._world_id)
        if world.managed_team_id is None:
            return False
        try:
            self._ensure_not_closed(world, self._season_over_year(world, expected_year=None))
        except (SeasonNotFinished, AlreadyClosed, SeasonLimitReached):
            return False
        return True

    def start_manual_releases(self, *, expected_year: int | None) -> None:
        """戦力外を手動にする（0人から選ぶ）。すでに手動なら何も変えない。"""
        year, team_id = self._release_target(expected_year)
        current = self._releases.find_by_team(team_id)
        if current is None or current.year != year:
            self._releases.save(ReleasePlan(team_id=team_id, year=year, player_ids=frozenset()))

    def set_releases(self, player_ids: Sequence[int], *, expected_year: int | None) -> None:
        """戦力外を手動で指名する（丸ごと置き換える。0人も可）。成立しなければ `InvalidReleasePlan`。"""
        year, team_id = self._release_target(expected_year)
        teams = self._context_query.teams()
        players = _team_of(teams, team_id).players
        ensure_releasable(
            player_ids,
            [ClubMember(p.player_id, p.name, p.position, p.is_foreign) for p in players],
            foreign_rules=ForeignPlayerRules.strictest(t.foreign_rules for t in teams),
        )
        self._releases.save(ReleasePlan(team_id=team_id, year=year, player_ids=frozenset(player_ids)))

    def reset_releases(self, *, expected_year: int | None) -> None:
        """戦力外を自動に戻す。すでに自動なら何も変わらない。"""
        year, team_id = self._release_target(expected_year)
        self._releases.save(ReleasePlan(team_id=team_id, year=year, player_ids=None))

    def _release_target(self, expected_year: int | None) -> tuple[int, int]:
        """指名を変えられるか（公式戦を終えた年が画面を開いたときの年と同じ）を調べ、締める年と受け持つ球団を返す。"""
        world = self._worlds.find_by_id(self._world_id)
        year = self._season_over_year(world, expected_year=expected_year)
        self._ensure_not_closed(world, year)
        return year, self._managed_team_id(world)

    @staticmethod
    def _managed_team_id(world: World) -> int:
        if world.managed_team_id is None:
            raise TeamNotFound("この世界には、受け持つ球団が決まっていません。")
        return world.managed_team_id

    def _manual_releases(self, world: World, year: int) -> dict[int, frozenset[int]]:
        """締める処理が使う手動の指名（受け持つ球団の、締める年の指名だけ。AI 球団は常に自動）。"""
        if world.managed_team_id is None:
            return {}
        chosen = self._chosen_releases(self._releases.find_by_team(world.managed_team_id), year)
        return {} if chosen is None else {world.managed_team_id: chosen}

    @staticmethod
    def _chosen_releases(plan: ReleasePlan | None, year: int) -> frozenset[int] | None:
        """締める年 `year` に使う手動の指名。自動なら None。

        締める年と違う指名（保存した年のまま締めなかったもの。ふつうは締めるときに消えるので残らない）は使わない。
        """
        if plan is None or plan.year != year:
            return None
        return plan.player_ids

    # --- 検査 ---

    def _check(
        self, world: World, *, expected_year: int | None, postseason_status: PostseasonStatus | None = None
    ) -> int:
        """締められるかを調べ、締める年 Y を返す。締められなければ例外。

        戦力外を指名できる条件（`_season_over_year`）に、「その年のポストシーズンが終わった」を足したもの。
        """
        year = self._season_over_year(world, expected_year=expected_year)
        status = self._postseason.status(world, year) if postseason_status is None else postseason_status
        _ensure_postseason_settled(year, status)
        return year

    def _season_over_year(self, world: World, *, expected_year: int | None) -> int:
        """その年の公式戦を終えて、まだ締めていない年 Y を返す（最終シーズンは除く）。そうでなければ例外。

        **戦力外を指名できる条件の唯一の出典**（ポストシーズン中も指名できる。使うのは締めるときだけ）。
        締められる条件（`_check`）はこれにポストシーズンの検査を足したもの。
        「公式戦を終えた年」は domain の `regular_season_over_year` が出典（ここで書き直さない）。ここで足すのは
        画面を開いたときの年（`expected_year`）と最終シーズンの検査だけ。
        """
        year = regular_season_over_year(
            last_played_on=self._context_query.last_played_on(),
            next_fixture_on=self._fixtures.first_date(),
            start_year=world.start_year,
        )
        if expected_year is not None and expected_year != year:
            if world.start_year <= expected_year < year:
                raise AlreadyClosed(f"{expected_year}年のシーズンは既に締めています。", year=expected_year)
            raise AlreadyClosed("表示している年度と世界の年度が違います。開き直してください。")
        if is_final_season(current_year=year, start_year=world.start_year):
            raise SeasonLimitReached("この世界のシーズン数の上限に達しているので、これ以上は締められません。")
        return year

    # --- 読む・計算する（トランザクションの外）→ 書く（中） ---

    def _prepare(self, world: World, year: int) -> _PreparedClose:
        """読んで計算し、集約の変更（引退・戦力外・新人）をメモリの上で済ませる。DB には何も書かない。"""
        teams = self._teams.find_all_with_roster()
        leagues = self._leagues.find_all()
        limits = {league.id: league.roster_limits for league in leagues}
        foreign = {league.id: league.foreign_rules for league in leagues}
        ratings_by_player = {item.player_id: item for item in self._ratings.find_by_year(year)}
        playing = self._playing_time.for_year(year)
        manual = self._manual_releases(world, year)
        # 締める年のタイトル受賞者（表彰として書く）。画面と同じ集計で、リーグごとに1回（8リーグで0.6〜2.9秒）
        awards = derive_awards(self._titles, year, [_saved_id(league.id) for league in leagues])
        # 実績の加点の材料: 締めた年までの表彰の数と、いま締める年の受賞の数（過去の年を導き直さない）
        titles = title_counts(awards) + Counter(self._awards.count_by_player())

        clubs = [
            OffseasonClub(
                team_id=_saved_id(team.id),
                foreign_rules=foreign.get(team.league_id, ForeignPlayerRules.UNLIMITED),
                registered_player_limit=_limits_of(team, limits).registered,
                players=tuple(
                    _offseason_player(team, player, ratings_by_player, playing, titles, year=year)
                    for player in team.active_players
                ),
            )
            for team in teams
        ]
        plan = plan_offseason(
            clubs,
            year=year,
            world_seed=world.seed,
            start_year=world.start_year,
            used_names={player.name for team in teams for player in team.players},
            manual_releases=manual,
        )
        changes = self._apply_roster_changes(teams, plan, limits, year)
        schedule = season_schedule(
            {
                league_id: sorted(_saved_id(team.id) for team in teams if team.league_id == league_id)
                for league_id in sorted({team.league_id for team in teams if team.league_id is not None})
            },
            world_seed=world.seed,
            year=year + 1,
        )
        return _PreparedClose(plan=plan, changes=changes, schedule=schedule, year=year, awards=awards)

    def _commit(self, world: World, prepared: _PreparedClose) -> SeasonClosed:
        """計算済みの変更を書く（トランザクションの中。SQL を流すだけにする）。"""
        plan, year = prepared.plan, prepared.year
        rookie_ratings: list[PlayerRatings] = []
        for change in prepared.changes:
            self._teams.save(change.team)
            # 保存すると新人に id が入る。その id で翌年の能力と結びつける
            rookie_ratings.extend(
                PlayerRatings(player_id=_saved_id(player.id), year=year + 1, ratings=draftee.ratings)
                for player, draftee in zip(change.added, change.draftees, strict=True)
            )
        retained = [change.after for change in plan.retained]
        self._ratings.add_all([*retained, *rookie_ratings])

        reverted, reverted_clubs = self._revert_plans(plan, world)
        # 戦力外の指名は使い終わった（締める年の違う指名も、使わずに消す）。事実は在籍の退団の経路に残る
        self._releases.delete_all()
        self._fixtures.add_all(prepared.schedule)
        # 締める年のタイトル受賞者を表彰として書く（導いたのはロックの外。#248）
        self._awards.add_all(prepared.awards)
        # 締める年の日本一を記録として書く（導いたのはロックの外。#242）
        if prepared.championship is not None:
            self._championships.add(prepared.championship)

        return SeasonClosed(
            year=year,
            next_year=year + 1,
            retired_count=len(plan.retired),
            released_count=len(plan.released),
            draftee_count=len(plan.draftees),
            foreign_draftee_count=sum(1 for draftee in plan.draftees if draftee.is_foreign),
            without_ratings_count=len(plan.without_ratings),
            reverted_sections=reverted,
            reverted_club_count=reverted_clubs,
            fixture_count=len(prepared.schedule),
        )

    def _apply_roster_changes(
        self, teams: list[Team], plan: OffseasonPlan, limits: Mapping[int | None, RosterLimits], year: int
    ) -> list[_RosterChange]:
        """引退・戦力外と新人を集約の操作でメモリの上に反映する（保存は `_commit`）。変わった球団だけ返す。"""
        retired = set(plan.retired)
        released = set(plan.released)
        next_year = year + 1
        changes: list[_RosterChange] = []
        for team in teams:
            team_id = _saved_id(team.id)
            retiring = [player for player in team.active_players if player.id in retired]
            releasing = [player for player in team.active_players if player.id in released]
            draftees = [draftee for draftee in plan.draftees if draftee.team_id == team_id]
            if not retiring and not releasing and not draftees:
                continue
            # 引退と戦力外を先に済ませ、在籍を Y で閉じて退団の経路を記録する（空いた背番号を新人へ渡せる）
            for player in retiring:
                team.retire_player(player, year=year, left_via=DepartureRoute.RETIRED)
            for player in releasing:
                team.release_player(player, year=year)
            added: list[Player] = []
            team_limits = _limits_of(team, limits)
            for draftee in draftees:
                # 新人は支配下で入り、入団の経路（ドラフト / 新外国人）を記録する。支配下の上限は集約が検査する
                # （人数は domain の `draftee_count` が上限を超えないように決めるので、ここでは失敗しない）。
                # 外国人の保有の人数は縛らない（協約第82条の2。外国人枠は1軍の登録にかかる）
                player = team.add_player(
                    draftee.name,
                    JerseyNumber(draftee.number),
                    draftee.position,
                    from_year=next_year,
                    contract=ContractStatus.REGISTERED,
                    acquired_via=draftee.acquisition_route,
                    limits=team_limits,
                )
                player.profile = draftee.profile
                added.append(player)
            changes.append(_RosterChange(team=team, added=added, draftees=draftees))
        return changes

    def _revert_plans(self, plan: OffseasonPlan, world: World) -> tuple[tuple[PlanSection, ...], int]:
        """引退・戦力外の選手を含む編成の区画を自動に戻して保存する（判断26。Wiki「ペナントモードの仕組み」の「決めたこと」）。受け持つ球団のぶんの区画と、戻した球団の数。"""
        leaving = set(plan.retired) | set(plan.released)
        own_sections: tuple[PlanSection, ...] = ()
        clubs = 0
        for club_plan in self._plans.find_all():
            reverted_plan, sections = club_plan.revert(leaving)
            if not sections:
                continue
            self._plans.save(reverted_plan)
            clubs += 1
            if club_plan.team_id == world.managed_team_id:
                own_sections = sections
        return own_sections, clubs


def _offseason_player(
    team: Team,
    player: Player,
    ratings: Mapping[int, PlayerRatings],
    playing: Mapping[int, PlayingTime],
    titles: Mapping[int, int],
    *,
    year: int,
) -> OffseasonPlayer:
    """締める年の現役の選手。`joined_year` は現在の在籍の開始年（分岐した選手は一律に開幕年）。

    引退の確率の「入団2年以内」の保護は、ドラフトの翌年（`DraftPick.entry_year`）を優先して数える（`OffseasonPlayer.entered_year`）。
    """
    player_id = _saved_id(player.id)
    stint = team.current_stint(player)
    joined = year if stint is None else stint.from_year
    return OffseasonPlayer(
        player_id=player_id,
        position=player.position,
        profile=player.profile,
        number=player.number.value,
        joined_year=joined,
        ratings=ratings.get(player_id),
        # 今の区分は集約の判定（`Team.contract_of`＝`Stint.contract_now`）をそのまま渡す。支配下の上限の検査
        # （`Team.registered_player_count`）と同じ判定なので、domain の指名数が上限を超えない
        contract=team.contract_of(player),
        playing_time=playing.get(player_id, PlayingTime()),
        # 実績の加点の材料。通算成績は `find_all_with_roster` が世界の試合から集計した値（保存しない）
        track_record=TrackRecord.of(
            team_id=_saved_id(team.id),
            career=player.career,
            year=year,
            batting=player.batting,
            pitching=player.pitching,
            is_captain=team.current_captain is player,
            titles=titles.get(player_id, 0),
        ),
    )


def _limits_of(team: Team, limits: Mapping[int | None, RosterLimits]) -> RosterLimits:
    """球団のリーグの人数の上限。リーグが見つからなければ無制限（世界の球団はどれもリーグに属す）。"""
    return limits.get(team.league_id, RosterLimits.UNLIMITED)


def _team_of(teams: Sequence[SimulationTeam], team_id: int) -> SimulationTeam:
    """球団と、いま在籍している支配下の選手（`SimulationContextQuery.teams()` は育成を含まない）。"""
    team = next((team for team in teams if team.team_id == team_id), None)
    if team is None:
        raise TeamNotFound(f"球団が見つかりません（id={team_id}）。")
    return team


def _ensure_postseason_settled(year: int, status: PostseasonStatus) -> None:
    """その年のポストシーズンが終わった（または無い）か。終わっていなければ `PostseasonNotFinished`（締めない）。"""
    if not status.is_settled:
        raise PostseasonNotFinished(f"{year}年のポストシーズンがまだ終わっていません。")
