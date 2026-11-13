"""ペナントのシーズンを締める（オフ）。引退・戦力外・育成の昇格と契約の満了・外国人の獲得・ドラフト・成長と衰え・
編成の後始末・翌年の日程を、1つの操作にまとめる。

育成の昇格と契約の満了（#170 P1）は、戦力外の後・外国人の獲得の前に AI が決める（`domain/pennant/development.py`。
GM の指名は P2）。昇格は `Team.promote_player` に翌年（Y+1）を渡して書き（翌年から支配下・試合に出られる）、
満了の自由契約は戦力外と同じく `release_player`（退団の経路は戦力外）。再契約は何も書かない。

外国人の獲得（#169）は、戦力外の後・ドラフト会議の前に、AI が球団ごとの候補から選ぶ
（`domain/pennant/foreign_market.py`。GM の指名は F2）。獲得した外国人は翌年 Y+1 から新外国人として加入し、
前所属の区分をプロフィールに保存する。

ドラフトは世界で1回の会議（`domain/pennant/draft_meeting.py`。#198）で、指名順は世界の規則（`World.draft_order_rule`）と
締める年の**公式戦の最終順位**（`LeagueFinishReader`。NPB の「球団順位」は公式戦の年度順位で、CS・日本シリーズの
結果は使わない）から導く。GM が会議を開かなかった年は、この処理の中で全球団を AI として最後まで進める。
GM が会議（`PennantDraftService`。#297 D3）を開いた年は、途中なら締めずに `DraftInProgress` で断り、終わっていれば
保存した行動を再生する（会議を始めたときに写した人数の幅と、いま計算し直した幅を domain が比べる。
違えば `DraftChanged`）。
会議の記録は締めた時点で消す（指名の事実は新人のプロフィールと在籍の経路に残る）。

局面の検査（`OffseasonPhase`）と入力の読み口（`OffseasonInputsReader`）は会議と共有する（`pennant_offseason_inputs.py`）。

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
受け持つ球団の戦力外の指名（`ReleasePlan`）も GM が保存しうるが、こちらは計算に使うので状態の目印
（`OffseasonPhase.state`）に入れて前後で比べる（変わっていたら断る）。その年の会議の目印（最後の行動の番号。
`DraftRepository.last_seq`）も前後で比べる（計算している間に会議を始めた・進めたら、古い計算を書かない）。
指名を決める操作（`set_releases` など）もこのサービスが持つ（指名は公式戦を終えればポストシーズン中もできる。
会議を始めた後は変えられない＝`pre_draft_input_blocker`。書くトランザクションの中で会議の有無を読み直す）。

締める年のタイトル受賞者は**表彰として保存する**（#248。CLAUDE.md の「集計を保存しない」の例外）。受賞者は外で
タイトルの画面と同じ集計から導き（`pennant_awards.derive_awards`。8リーグで 0.6〜2.9秒）、中では書くだけ。
戦力外の実績の加点に使うタイトルの数は、締めた年までの表彰と、いま導いた締める年の受賞を足す（過去の年を導き直さない）。
締める年のポストシーズンの優勝球団（日本一）も**記録として保存する**（#242。表彰と同じ例外）。勝ち上がりは外で1回だけ導き、
締められるかの検査（ポストシーズンが終わったか）と、書く優勝球団の両方に使う。

**前提: 世界の `Team` 集約に書くのは、世界の作成とこの処理だけ。** 外で読んだ `Team` を中でそのまま
`save()` して安全なのは、この前提による（`save()` は変わった行だけを書くが、比べる相手は今の DB なので、
読んだあとに別の書き込みがあれば、集約の古い値が「違う」として書かれ、その書き込みを上書きして失う）。念のため、在籍の目印（`TeamRepository.roster_signature()`。行数・現在在籍の人数・
最大の id）も読み込みの前後で比べ、変わっていれば書かずに断る。
**世界の中で選手を書く操作を足すなら、その操作が状態の目印（`OffseasonPhase.state`）に現れるか、ここで中に読み直すこと。**

同時に2回呼ばれても、翌年の能力の一意制約（選手 × 年）で後から来た方が
失敗し、新人や在籍の変更ごと巻き戻る（`add_all` が保存前に既存の能力を検査して `InvalidRatings` を出し、
それもすり抜けたら DB の一意制約）。**この2段が最後の砦**。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from ..domain.entities import Player, Team
from ..domain.exceptions import (
    AlreadyClosed,
    DraftStarted,
    PostseasonNotFinished,
    SeasonLimitReached,
    SeasonNotFinished,
    TeamNotFound,
)
from ..domain.pennant.awards import Award
from ..domain.pennant.championship import Championship
from ..domain.pennant.club_plan import ClubMember, PlanSection
from ..domain.pennant.draft_lottery import DraftKey, lottery_records
from ..domain.pennant.draft_record import pre_draft_input_blocker
from ..domain.pennant.foreign_market import ForeignSigning
from ..domain.pennant.offseason import Draftee, OffseasonPlan, plan_offseason
from ..domain.pennant.postseason import PostseasonStatus
from ..domain.pennant.ratings import PlayerRatings
from ..domain.pennant.release import ReleasePlan, ensure_releasable
from ..domain.pennant.schedule import Fixture, season_schedule
from ..domain.pennant.season import is_season_closed
from ..domain.pennant.world import World
from ..domain.repositories import (
    AwardRepository,
    ChampionshipRepository,
    ClubPlanRepository,
    DraftLotteryRepository,
    DraftRepository,
    FixtureRepository,
    LeagueRepository,
    RatingsRepository,
    ReleasePlanRepository,
    TeamRepository,
    WorldRepository,
)
from ..domain.value_objects import DepartureRoute, ForeignPlayerRules, JerseyNumber, RosterLimits
from .dto import ReleasePlanView, ReleasePlayerRow, SeasonClosed, SeasonCloseOption, SimulationTeam
from .foreign_quota import ForeignQuotaExemptions
from .league_view import LeagueViewService
from .pennant_awards import derive_awards
from .pennant_offseason_inputs import (
    OffseasonInputsReader,
    OffseasonPhase,
    chosen_releases,
    ensure_postseason_settled,
    limits_of,
    managed_team_id,
    release_plan_of,
)
from .pennant_postseason import PostseasonBracketReader
from .pennant_season import AtomicBlock
from .queries import SimulationContextQuery


def _saved_id(value: int | None) -> int:
    """保存済みの集約から取り出す id。永続化された後は必ず値がある。"""
    assert value is not None, "保存済みの集約には id がある"
    return value


@dataclass
class _RosterChange:
    """メモリの上で引退・戦力外・獲得した外国人・新人を反映した球団（保存はまだ）。

    `added` と `newcomers` は同じ並び。"""

    team: Team
    added: list[Player]
    newcomers: list[ForeignSigning | Draftee]


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
        drafts: DraftRepository,
        lotteries: DraftLotteryRepository,
        titles: LeagueViewService,
        context_query: SimulationContextQuery,
        postseason: PostseasonBracketReader,
        phase: OffseasonPhase,
        inputs: OffseasonInputsReader,
        fa_exemptions: ForeignQuotaExemptions,
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
        # その年のドラフト会議の記録（GM が開いた会議。締めるときに再生して消す。#297 D3）
        self._drafts = drafts
        # 締めた年の抽選の記録（会議の記録が消えた後の抽選の出典。#298 D4）
        self._lotteries = lotteries
        # タイトルの受賞者を導く（世界の範囲の、タイトルの画面と同じサービス。表彰の補いが使う）
        self._titles = titles
        self._context_query = context_query
        # 締める年の勝ち上がり（締められるかの検査と、記録する日本一）
        self._postseason = postseason
        # 局面の検査と状態の目印・締める処理の入力の読み口（ドラフト会議と共有する）
        self._phase = phase
        self._inputs = inputs
        # FA 資格の取得による外国人枠の対象外（協約 第82条 (4)）。GM の戦力外の検査だけが使う
        self._fa_exemptions = fa_exemptions
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
        chosen = chosen_releases(release_plan_of(self._releases, world.managed_team_id), year)
        return SeasonCloseOption(
            year=year, next_year=year + 1, can_close=True, release_count=None if chosen is None else len(chosen)
        )

    def close_season(self, *, expected_year: int | None = None) -> SeasonClosed:
        """シーズンを締める。締める年は最後に試合をした年 Y で、翌年 Y+1 の在籍・能力・日程を作る。

        - 未消化の対戦が残っている、または試合を一度もしていない → `SeasonNotFinished`
        - 既に締めている、または `expected_year`（画面を開いたときの年）が Y と違う → `AlreadyClosed`
        - 世界の最後のシーズン → `SeasonLimitReached`
        - その年のポストシーズンが終わっていない（始まっていない・進行中） → `PostseasonNotFinished`
        - GM が開いたドラフト会議が途中 → `DraftInProgress`。会議を始めた後にコードが変わって候補か人数の幅が
          食い違う → `DraftChanged`（会議をやり直す）

        どれも何も書かない。書き込みの途中で失敗したときも何も残さない（1つのトランザクション）。
        引退・戦力外の選手の在籍は Y で閉じ（退団の経路を記録する）、新人は Y+1 から加入する。
        **年を省略した在籍の操作は呼ばない**
        （省くと現実の今日の年が入る）。
        """
        # 読み込みと計算（1〜6秒）は、書き込みトランザクションの外で済ませる。本番の SQLite は書き手が1つで、
        # トランザクションの中にいる間は他の書き込みを止めるため。中では状態が変わっていないことだけを確かめ、書く
        world = self._worlds.find_by_id(self._world_id)
        state = self._phase.state()
        year = self._season_over_year(world, expected_year=expected_year)
        # その年の会議の目印（最後の行動の番号。始めていなければ None）。状態の目印と同じく前後で比べる
        meeting_mark = self._drafts.last_seq(year)
        # 勝ち上がりは1回だけ導き、締められるかの検査と、記録する日本一の両方に使う
        bracket = self._postseason.bracket(world, year)
        ensure_postseason_settled(year, bracket.status)
        self._ensure_not_closed(world, year)
        champion = bracket.champion
        prepared = replace(
            self._prepare(world, year),
            championship=Championship(year=year, team_id=champion) if champion is not None else None,
        )
        with self._atomic():
            if self._phase.state() != state or self._drafts.last_seq(year) != meeting_mark:
                # 計算している間に、別のリクエストが進めた・締めた・会議を進めた。古い読み込みを書かない。
                # いまの状態で検査し直し、それぞれの理由（未消化・既に締めた）で断る
                world = self._worlds.find_by_id(self._world_id)
                self._ensure_not_closed(world, self._check(world, expected_year=expected_year))
                raise AlreadyClosed("計算している間に世界の状態が変わりました。開き直してください。")
            self._ensure_not_closed(world, year)
            return self._commit(world, prepared)

    def _ensure_not_closed(self, world: World, year: int) -> None:
        self._phase.ensure_not_closed(world, year)

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
        team_id = managed_team_id(world)
        team = _team_of(self._context_query.teams(), team_id)
        chosen = chosen_releases(self._releases.find_by_team(team_id), year)
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
            blocker=self._release_blocker(year) or "",
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
        """戦力外を手動にする（0人から選ぶ）。すでに手動なら何も変えない。会議を始めた後は `DraftStarted`。"""
        year, team_id = self._release_target(expected_year)
        with self._atomic():
            self._ensure_releases_open(year)
            current = self._releases.find_by_team(team_id)
            if current is None or current.year != year:
                self._releases.save(ReleasePlan(team_id=team_id, year=year, player_ids=frozenset()))

    def set_releases(self, player_ids: Sequence[int], *, expected_year: int | None) -> None:
        """戦力外を手動で指名する（丸ごと置き換える。0人も可）。成立しなければ `InvalidReleasePlan`、
        会議を始めた後は `DraftStarted`。"""
        year, team_id = self._release_target(expected_year)
        teams = self._context_query.teams()
        players = _team_of(teams, team_id).players
        # 残る選手が組むのは翌シーズン（Y+1 年）の名簿なので、外国人枠は Y+1 年の扱い（Y 年までの記録）で数える
        exemptions = self._fa_exemptions.for_year([p.player_id for p in players if p.is_foreign], year + 1)
        ensure_releasable(
            player_ids,
            [
                ClubMember(
                    p.player_id,
                    p.name,
                    p.position,
                    counts_as_foreign=exemptions.counts(p.player_id, is_foreign_player=p.is_foreign),
                )
                for p in players
            ],
            foreign_rules=ForeignPlayerRules.strictest(t.foreign_rules for t in teams),
        )
        with self._atomic():
            self._ensure_releases_open(year)
            self._releases.save(ReleasePlan(team_id=team_id, year=year, player_ids=frozenset(player_ids)))

    def reset_releases(self, *, expected_year: int | None) -> None:
        """戦力外を自動に戻す。すでに自動なら何も変わらない。会議を始めた後は `DraftStarted`。"""
        year, team_id = self._release_target(expected_year)
        with self._atomic():
            self._ensure_releases_open(year)
            self._releases.save(ReleasePlan(team_id=team_id, year=year, player_ids=None))

    def _release_target(self, expected_year: int | None) -> tuple[int, int]:
        """指名を変えられるか（公式戦を終えた年が画面を開いたときの年と同じ）を調べ、締める年と受け持つ球団を返す。"""
        world = self._worlds.find_by_id(self._world_id)
        year = self._season_over_year(world, expected_year=expected_year)
        self._ensure_not_closed(world, year)
        return year, managed_team_id(world)

    def _release_blocker(self, year: int) -> str | None:
        """戦力外の指名を変えられない理由（その年の会議を始めた。`pre_draft_input_blocker`）。変えられるなら None。"""
        return pre_draft_input_blocker(meeting_started=self._drafts.last_seq(year) is not None)

    def _ensure_releases_open(self, year: int) -> None:
        """書く直前（トランザクションの中）に、その年の会議が始まっていないことを読み直す。

        会議を始める処理も中で戦力外の指名を読み直す（状態の目印）ので、どちらが先に書いても、会議が写した人数の幅と
        戦力外の指名は食い違わない。
        """
        blocker = self._release_blocker(year)
        if blocker is not None:
            raise DraftStarted(blocker)

    # --- 検査（`OffseasonPhase` が出典。ここは世界を渡すだけ） ---

    def _check(
        self, world: World, *, expected_year: int | None, postseason_status: PostseasonStatus | None = None
    ) -> int:
        """締められるかを調べ、締める年 Y を返す。締められなければ例外（`OffseasonPhase.closable_year`）。"""
        return self._phase.closable_year(world, expected_year=expected_year, postseason_status=postseason_status)

    def _season_over_year(self, world: World, *, expected_year: int | None) -> int:
        """戦力外を指名できる年 Y（`OffseasonPhase.season_over_year`）。"""
        return self._phase.season_over_year(world, expected_year=expected_year)

    # --- 読む・計算する（トランザクションの外）→ 書く（中） ---

    def _prepare(self, world: World, year: int) -> _PreparedClose:
        """読んで計算し、集約の変更（引退・戦力外・新人）をメモリの上で済ませる。DB には何も書かない。

        GM が開いた会議があれば、その記録を再生する（途中なら `DraftInProgress`。domain の `replay_meeting`）。
        """
        inputs = self._inputs.read(world, year)
        teams = inputs.teams
        plan = plan_offseason(
            inputs.clubs,
            year=year,
            world_seed=world.seed,
            start_year=world.start_year,
            used_names=inputs.used_names,
            manual_releases=inputs.manual_releases,
            draft=inputs.draft,
            meeting=self._drafts.find(year),
        )
        changes = self._apply_roster_changes(teams, plan, inputs.limits, year)
        schedule = season_schedule(
            {
                league_id: sorted(_saved_id(team.id) for team in teams if team.league_id == league_id)
                for league_id in sorted({team.league_id for team in teams if team.league_id is not None})
            },
            world_seed=world.seed,
            year=year + 1,
        )
        return _PreparedClose(plan=plan, changes=changes, schedule=schedule, year=year, awards=inputs.awards)

    def _commit(self, world: World, prepared: _PreparedClose) -> SeasonClosed:
        """計算済みの変更を書く（トランザクションの中。SQL を流すだけにする）。"""
        plan, year = prepared.plan, prepared.year
        rookie_ratings: list[PlayerRatings] = []
        # 指名した新人の選手 id（球団・巡・育成か → id）。抽選の記録を選手に結びつける（#298 D4）
        drafted: dict[DraftKey, int] = {}
        for change in prepared.changes:
            self._teams.save(change.team)
            # 保存すると新人と獲得した外国人に id が入る。その id で翌年の能力と結びつける
            rookie_ratings.extend(
                PlayerRatings(player_id=_saved_id(player.id), year=year + 1, ratings=newcomer.ratings)
                for player, newcomer in zip(change.added, change.newcomers, strict=True)
            )
            for player, newcomer in zip(change.added, change.newcomers, strict=True):
                pick = newcomer.profile.draft if isinstance(newcomer, Draftee) else None
                if pick is not None and pick.round is not None:
                    drafted[(newcomer.team_id, pick.round, pick.is_development)] = _saved_id(player.id)
        retained = [change.after for change in plan.retained]
        self._ratings.add_all([*retained, *rookie_ratings])

        reverted, reverted_clubs = self._revert_plans(plan, world)
        # 戦力外の指名は使い終わった（締める年の違う指名も、使わずに消す）。事実は在籍の退団の経路に残る
        self._releases.delete_all()
        # その年のドラフト会議の記録も使い終わった（指名の事実は新人のプロフィールと在籍の経路に残る。#297 D3）
        self._drafts.remove(year)
        # 抽選の経過は、会議の記録が消えるとどこからも導けないので、指名された選手に結びつけて残す
        # （締めた後はこの記録だけが出典。導ける年・巡・当たった球団は持たない。#298 D4・2026-10-12 ユーザー決定）
        self._lotteries.add_all(lottery_records(plan.lotteries, drafted))
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
            promoted_count=len(plan.promotions),
            expired_count=len(plan.expired),
            draftee_count=len(plan.draftees),
            signed_foreign_count=len(plan.signings),
            development_draftee_count=sum(1 for draftee in plan.draftees if draftee.is_development),
            first_round_lottery_count=sum(
                1 for lottery in plan.lotteries if lottery.round_number == 1 and not lottery.is_development
            ),
            without_ratings_count=len(plan.without_ratings),
            reverted_sections=reverted,
            reverted_club_count=reverted_clubs,
            fixture_count=len(prepared.schedule),
        )

    def _apply_roster_changes(
        self, teams: list[Team], plan: OffseasonPlan, limits: Mapping[int | None, RosterLimits], year: int
    ) -> list[_RosterChange]:
        """引退・戦力外（育成契約の満了を含む）・昇格・獲得した外国人・新人を集約の操作でメモリの上に反映する
        （保存は `_commit`）。

        変わった球団だけ返す。"""
        retired = set(plan.retired)
        # 育成契約の満了の自由契約も、退団の経路は戦力外（2026-10-24 ユーザー決定。#170 Q3）
        released = set(plan.released) | set(plan.expired)
        next_year = year + 1
        changes: list[_RosterChange] = []
        for team in teams:
            team_id = _saved_id(team.id)
            retiring = [player for player in team.active_players if player.id in retired]
            releasing = [player for player in team.active_players if player.id in released]
            draftees = [draftee for draftee in plan.draftees if draftee.team_id == team_id]
            signings = [signing for signing in plan.signings if signing.team_id == team_id]
            promotions = [promotion for promotion in plan.promotions if promotion.team_id == team_id]
            if not retiring and not releasing and not promotions and not draftees and not signings:
                continue
            # 引退と戦力外を先に済ませ、在籍を Y で閉じて退団の経路を記録する（空いた背番号を新人へ渡せる）
            for player in retiring:
                team.retire_player(player, year=year, left_via=DepartureRoute.RETIRED)
            for player in releasing:
                team.release_player(player, year=year)
            added: list[Player] = []
            team_limits = limits_of(team, limits)
            # 育成から支配下への昇格（#170）。翌年から支配下（年は必ず渡す。省くと現実の今日の年になる）。
            # 背番号は domain が空いている支配下の番号から引いた。支配下の上限は集約が検査する
            # （domain が新人の枠を残して数えたので、ここでは失敗しない）
            for promotion in promotions:
                team.promote_player(promotion.player_id, JerseyNumber(promotion.number), next_year, limits=team_limits)
            # 獲得した外国人を新人の前に（獲得はドラフト会議の前。#169）。翌年から支配下・新外国人で加入する。
            # 前所属の区分はプロフィールに入っている（`ForeignSigning.profile`）。外国人の保有の人数は縛らない
            # （協約第82条の2。ゲームの上限 `foreign_market.MAX_FOREIGN_HELD` は domain が獲得の前に検査した）
            for signing in signings:
                player = team.add_player(
                    signing.name,
                    JerseyNumber(signing.number),
                    signing.position,
                    from_year=next_year,
                    contract=signing.contract,
                    acquired_via=signing.acquisition_route,
                    limits=team_limits,
                )
                player.profile = signing.profile
                added.append(player)
            for draftee in draftees:
                # 新人は契約区分（本指名は支配下、育成ドラフトは育成。`Draftee.contract`）と入団の経路
                # （ドラフト / 育成ドラフト）を記録する。区分と背番号・経路の食い違いと支配下の上限は
                # 集約が検査する
                # （人数は domain の `draftee_count` が上限を超えないように決めるので、ここでは失敗しない。
                # 育成は上限に数えない）。
                # 加入年は、ドラフトの指名があれば「入団はドラフトの翌年」の出典（`DraftPick.entry_year`）から
                pick = draftee.profile.draft
                player = team.add_player(
                    draftee.name,
                    JerseyNumber(draftee.number),
                    draftee.position,
                    from_year=next_year if pick is None else pick.entry_year,
                    contract=draftee.contract,
                    acquired_via=draftee.acquisition_route,
                    limits=team_limits,
                )
                player.profile = draftee.profile
                added.append(player)
            changes.append(_RosterChange(team=team, added=added, newcomers=[*signings, *draftees]))
        return changes

    def _revert_plans(self, plan: OffseasonPlan, world: World) -> tuple[tuple[PlanSection, ...], int]:
        """引退・戦力外の選手を含む編成の区画を自動に戻して保存する（判断26。Wiki「ペナントモードの仕組み」の「決めたこと」）。受け持つ球団のぶんの区画と、戻した球団の数。"""
        leaving = set(plan.retired) | set(plan.released) | set(plan.expired)
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


def _team_of(teams: Sequence[SimulationTeam], team_id: int) -> SimulationTeam:
    """球団と、いま在籍している支配下の選手（`SimulationContextQuery.teams()` は育成を含まない）。"""
    team = next((team for team in teams if team.team_id == team_id), None)
    if team is None:
        raise TeamNotFound(f"球団が見つかりません（id={team_id}）。")
    return team
