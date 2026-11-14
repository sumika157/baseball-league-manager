"""ペナントのドラフト会議（GM が1巡ずつ進める。#297 D3）。

会議を開かなかった年は、シーズンを締める処理（`PennantOffseasonService`）が全球団を AI として一度に進める
（今までどおり）。このサービスは、GM が会議を開いて自分の番だけを選ぶための更新サービス。
会議の間、`Team` 集約・能力・日程には何も書かない（新人を `Team` に入れるのは締める処理の1つのトランザクション）。
書くのは会議の記録（`DraftRecord`。人数の幅と行動の並び）だけで、締める処理が再生して使い、締めた時点で消す。

- **開ける時点**: ポストシーズンの後（締められる条件と同じ。#297 Q1）。検査は締める処理と共有する（`OffseasonPhase`）
- **始める**（`start`）: 締める処理と同じ入力（`OffseasonInputsReader`）から、同じ順（引退・戦力外 → 外国人の獲得 →
  人数の幅と候補。domain の `prepare_meeting`）で計算し、人数の幅と候補の指紋を記録に写す。GM の番まで AI を進めた
  行動も一緒に書く。8リーグで数秒かかるので、**読む・計算するはトランザクションの外**で行い、中では状態の目印
  （`OffseasonPhase.state`。戦力外の指名・指名順の規則を含む）を読み直してから書く（締める処理と同じ形）
- **1手ごと**（`nominate`・`pass_turn`・`delegate_turn`・`auto_complete`）: 外で記録を読み、写した人数の幅から候補を
  作り直し（`meeting_pool`。名前は `PlayerNameQuery`。集約は組み立てない）、再生して GM の行動を当て、次の GM の番まで
  AI を進める（`advance_ai(stop_at=GM)`）。中では最後の行動の番号を読み直して足す（`DraftRepository.append` の
  `expected_seq`。締めた・やり直した後は記録が無いので断る）。**行動は必ず `DraftState.apply` で当ててから書く**
  （当てられない行動を書くと、次に開いたときに `DraftChanged` になり、原因と違う案内でやり直しを求めることになる）
- **やり直し**（`reset`）: コードが変わって保存した会議を再生できないとき（`DraftChanged`）だけ。GM が抽選の結果を
  見てからやり直す操作は作らない（#297 Q5）
- 会議を始めた後は、戦力外の指名（`PennantOffseasonService.set_releases` など）・育成の指名（`set_development` など。
  #170 P2）・外国人の指名（`set_foreign_signings` など。#169 F2）と指名順の規則（設定）を変えられない
  （`pre_draft_input_blocker`・`draft_order_rule_change_blocker`。どちらも書く直前に会議の有無を読み直す）

見せるのは受け持つ球団のスカウトの見立ての見込みの区分だけ（球団の好み・伸びしろは見せない。#297 Q3・Q4）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from ..domain import services as domain_services
from ..domain.entities import Team
from ..domain.exceptions import (
    AlreadyClosed,
    DraftChanged,
    DraftConflict,
    DraftStarted,
    InvalidDraftAction,
    PostseasonNotFinished,
    SeasonLimitReached,
    SeasonNotFinished,
    TeamNotFound,
)
from ..domain.pennant.draft import DraftRoute
from ..domain.pennant.draft_meeting import (
    DraftAction,
    DraftActionKind,
    DraftMove,
    DraftState,
    Lottery,
    meeting_pool,
    projected_grade,
)
from ..domain.pennant.draft_record import DraftMeetingStatus, DraftRecord
from ..domain.pennant.offseason import prepare_meeting
from ..domain.pennant.postseason import PostseasonStatus
from ..domain.pennant.world import World
from ..domain.repositories import DraftRepository, TeamRepository, WorldRepository
from ..domain.value_objects import Position
from .draft_board import BoardPick, draft_board
from .dto import (
    DraftBoard,
    DraftBoardCell,
    DraftBoardColumn,
    DraftLotteryView,
    DraftMeetingView,
    DraftMoveRow,
    DraftProspectRow,
    DraftRoundRow,
    DraftStep,
    DraftTurnView,
)
from .ids import saved_id, team_names
from .pennant_offseason_inputs import OffseasonInputsReader, OffseasonPhase, managed_team_id
from .pennant_season import AtomicBlock
from .queries import PlayerNameQuery

# 会議の画面の「直近の動き」に出す数
RECENT_MOVES = 20

# 会議を開けない理由になる例外（画面は理由を出す）
_CLOSED_REASONS = (SeasonNotFinished, PostseasonNotFinished, AlreadyClosed, SeasonLimitReached, TeamNotFound)


class PennantDraftService:
    """世界ひとつのドラフト会議。リポジトリと参照クエリは、その世界の範囲で組み立てて渡す。"""

    def __init__(
        self,
        *,
        world_id: int,
        worlds: WorldRepository,
        teams: TeamRepository,
        drafts: DraftRepository,
        names: PlayerNameQuery,
        phase: OffseasonPhase,
        inputs: OffseasonInputsReader,
        atomic: AtomicBlock,
    ) -> None:
        self._world_id = world_id
        self._worlds = worlds
        # 球団の名前（画面の材料）。集約の在籍は読まない（`find_all`）
        self._teams = teams
        self._drafts = drafts
        # 世界の全選手の名前（候補を作り直すときに、名前が重ならないようにする）
        self._names = names
        # 局面の検査と状態の目印・締める処理の入力の読み口（締める処理と共有する）
        self._phase = phase
        self._inputs = inputs
        self._atomic = atomic

    # --- 読む ---

    def view(
        self,
        *,
        expected_year: int | None = None,
        sort: str | None = None,
        descending: bool | None = None,
        position: str | None = None,
        route: str | None = None,
        postseason_status: PostseasonStatus | None = None,
    ) -> DraftMeetingView:
        """会議の画面の材料。会議を開けないときは理由（`blocker`）だけを返す（例外にしない）。

        保存した会議を再生できないとき（`DraftChanged`）は `changed` を立てて返す（「会議をやり直す」だけを出す）。
        候補は `position`・`route`（`Position`・`DraftRoute` の名前）で絞り、`sort`・`descending` で並べる
        （不正な値は絞らず、不正なキーは見込みの区分の高い順に落とす）。
        `postseason_status` は呼び手（GM ホーム）が既に導いたその年のポストシーズンの状況（渡せば導き直さない。
        **表示にだけ使う**: 書き込みの操作は必ず導き直す）。
        """
        world = self._worlds.find_by_id(self._world_id)
        try:
            year, managed = self._open_year(world, expected_year, postseason_status=postseason_status)
        except _CLOSED_REASONS as error:
            return DraftMeetingView(
                year=None,
                status=DraftMeetingStatus.NOT_STARTED,
                blocker=str(error),
                managed_team_id=world.managed_team_id,
            )
        record = self._drafts.find(year)
        if record is None:
            return DraftMeetingView(
                year=year, status=DraftMeetingStatus.NOT_STARTED, blocker="", managed_team_id=managed
            )
        try:
            state = self._replay(world, record)
        except DraftChanged:
            return DraftMeetingView(
                year=year,
                status=DraftMeetingStatus.IN_PROGRESS,
                blocker="",
                managed_team_id=managed,
                last_seq=record.last_seq,
                changed=True,
            )
        return self._meeting_view(
            year,
            managed,
            record,
            state,
            self._teams.find_all(),
            sort=sort,
            descending=descending,
            position=position,
            route=route,
        )

    # --- 書く ---

    def start(self, *, expected_year: int | None) -> DraftStep:
        """その年の会議を始める（人数の幅と候補の指紋を写し、GM の番まで AI を進める）。

        - 会議を開けない（ポストシーズンの途中・締めた後など） → それぞれの例外（`view` の `blocker` と同じ理由）
        - その年の会議が既にある → `DraftStarted`
        - 計算している間に世界の状態（戦力外の指名・指名順の規則・在籍・日程）が変わった → `AlreadyClosed`

        どれも何も書かない。
        """
        world = self._worlds.find_by_id(self._world_id)
        year, managed = self._open_year(world, expected_year)
        if self._drafts.last_seq(year) is not None:
            raise DraftStarted(f"{year}年のドラフト会議は既に始まっています。")
        state_before = self._phase.state()
        # 読む・計算する（トランザクションの外。締める処理と同じ入力から同じ順で）
        inputs = self._inputs.read(world, year)
        setup = prepare_meeting(
            inputs.clubs,
            year=year,
            world_seed=world.seed,
            start_year=world.start_year,
            used_names=inputs.used_names,
            draft=inputs.draft,
            manual_releases=inputs.manual_releases,
            manual_development=inputs.manual_development,
            manual_signings=inputs.manual_signings,
        ).setup
        state = DraftState.open(setup.strategy, setup.slots, setup.pool)
        actions = state.advance_ai(stop_at=managed)
        record = DraftRecord.start(year, setup.pool, setup.slots, actions)
        with self._atomic():
            if self._phase.state() != state_before:
                # 計算している間に、戦力外の指名・規則・在籍・日程が変わった。古い計算を写さない
                world = self._worlds.find_by_id(self._world_id)
                self._open_year(world, expected_year)
                raise AlreadyClosed("計算している間に世界の状態が変わりました。開き直してください。")
            self._phase.ensure_not_closed(world, year)
            self._drafts.start(record)
        return self._step(year, state, after_seq=0)

    def nominate(self, prospect_key: int, *, expected_year: int | None, expected_seq: int) -> DraftStep:
        """GM の番に候補を選ぶ（入札の巡なら入札、そうでなければ指名。どちらかは番が決める）。

        成り立たない（GM の番でない・指名済み・位置の最低人数を割る・育成で指名できない経路）→ `InvalidDraftAction`。
        画面が古い（`expected_seq` が違う・会議が無い）→ `DraftConflict`。
        """

        def act(state: DraftState, managed: int) -> None:
            turn = state.next_turn()
            kind = DraftActionKind.BID if turn is not None and turn.is_bid else DraftActionKind.PICK
            state.apply(DraftAction(kind, managed, prospect_key))

        return self._write(expected_year, expected_seq, act)

    def pass_turn(self, *, expected_year: int | None, expected_seq: int) -> DraftStep:
        """GM の球団の選択を終える（その区分＝本指名か育成の巡で、もう指名しない）。

        本指名で最少に届いていない・位置の最低人数に届いていない → `InvalidDraftAction`。
        """

        def act(state: DraftState, managed: int) -> None:
            state.apply(DraftAction(DraftActionKind.PASS, managed))

        return self._write(expected_year, expected_seq, act)

    def delegate_turn(self, *, expected_year: int | None, expected_seq: int) -> DraftStep:
        """GM の番を AI に任せる。**指名できる候補がいないときだけ**（AI はその場で候補を足して指名する）。

        指名できる候補がいる・GM の番でない → `InvalidDraftAction`。
        """

        def act(state: DraftState, managed: int) -> None:
            turn = state.next_turn()
            if turn is None or turn.team_id != managed:
                raise InvalidDraftAction("あなたの番ではありません。")
            if any(state.selectable(managed, prospect) for prospect in state.pool.prospects):
                raise InvalidDraftAction("指名できる候補がいるので、この番は任せられません。選手を選んでください。")
            state.apply(state.ai_action())

        return self._write(expected_year, expected_seq, act)

    def auto_complete(self, *, expected_year: int | None, expected_seq: int) -> DraftStep:
        """残りを全部 AI に任せて、会議を最後まで進める（GM の球団の番も AI が選ぶ）。会議が終わっていれば
        `InvalidDraftAction`。"""

        def act(state: DraftState, managed: int) -> None:
            if state.is_complete:
                raise InvalidDraftAction("ドラフト会議は終わっています。")
            state.auto_complete()

        return self._write(expected_year, expected_seq, act)

    def reset(self, *, expected_year: int | None, expected_seq: int) -> None:
        """会議の記録を消して、始める前に戻す。**保存した会議を再生できないとき（`DraftChanged`）だけ**（#297 Q5）。

        再生できる会議 → `InvalidDraftAction`。画面が古い → `DraftConflict`。
        再生できるかは、候補と行動のほかに人数の幅も計算し直して確かめる（締める処理が `DraftChanged` で断る会議を
        やり直せるように。8リーグで数秒。トランザクションの外）。
        """
        world = self._worlds.find_by_id(self._world_id)
        year, _ = self._open_year(world, expected_year)
        record = self._drafts.find(year)
        if record is None or record.last_seq != expected_seq:
            raise DraftConflict()
        if not self._changed(world, record):
            raise InvalidDraftAction("ドラフト会議は続けられるので、やり直せません。")
        with self._atomic():
            if self._drafts.last_seq(year) != expected_seq:
                raise DraftConflict()
            self._drafts.remove(year)

    # --- 中身 ---

    def _open_year(
        self, world: World, expected_year: int | None, *, postseason_status: PostseasonStatus | None = None
    ) -> tuple[int, int]:
        """会議を開ける年（締められる年と同じ条件。#297 Q1）と、受け持つ球団。開けなければ例外。"""
        year = self._phase.closable_year(world, expected_year=expected_year, postseason_status=postseason_status)
        self._phase.ensure_not_closed(world, year)
        return year, managed_team_id(world)

    def _replay(self, world: World, record: DraftRecord) -> DraftState:
        """保存した会議を再生する（写した人数の幅から候補を作り直す。集約は組み立てない）。

        再生できなければ `DraftChanged`。"""
        pool = meeting_pool(record.slots, world_seed=world.seed, year=record.year, used_names=self._names.names())
        return record.state(self._inputs.strategy(world, record.year), pool)

    def _changed(self, world: World, record: DraftRecord) -> bool:
        """保存した会議を、締める処理と同じ計算で再生できないか（候補・行動・人数の幅のどれかが食い違う）。"""
        try:
            self._replay(world, record)
            inputs = self._inputs.read(world, record.year)
            setup = prepare_meeting(
                inputs.clubs,
                year=record.year,
                world_seed=world.seed,
                start_year=world.start_year,
                used_names=inputs.used_names,
                draft=inputs.draft,
                manual_releases=inputs.manual_releases,
                manual_development=inputs.manual_development,
                manual_signings=inputs.manual_signings,
            ).setup
            record.ensure_same_slots(setup.slots)
            record.state(setup.strategy, setup.pool)
        except DraftChanged:
            return True
        return False

    def _write(
        self, expected_year: int | None, expected_seq: int, act: Callable[[DraftState, int], None]
    ) -> DraftStep:
        """外で再生して GM の行動（`act`）を当て、次の GM の番まで AI を進めてから、中で足す。"""
        world = self._worlds.find_by_id(self._world_id)
        year, managed = self._open_year(world, expected_year)
        record = self._drafts.find(year)
        if record is None or record.last_seq != expected_seq:
            # 画面が古い（締めた・やり直した・別のタブで進めた）。再生の前に断る（中でもう一度比べる）
            raise DraftConflict()
        state = self._replay(world, record)
        act(state, managed)
        state.advance_ai(stop_at=managed)
        added = state.actions[record.last_seq :]
        # 当てた行動だけを足す（`expected_seq` が今の最後の番号と違えば `DraftConflict`）
        record.appended(added, expected_seq=expected_seq)
        with self._atomic():
            self._drafts.append(year, added, expected_seq=expected_seq)
        return self._step(year, state, after_seq=expected_seq)

    def _step(self, year: int, state: DraftState, *, after_seq: int) -> DraftStep:
        names = team_names(self._teams)
        return DraftStep(
            year=year,
            last_seq=len(state.actions),
            moves=tuple(_move_row(state, move, names) for move in state.resolved_moves(after_seq)),
            is_complete=state.is_complete,
        )

    def _meeting_view(
        self,
        year: int,
        managed: int,
        record: DraftRecord,
        state: DraftState,
        teams: Sequence[Team],
        *,
        sort: str | None,
        descending: bool | None,
        position: str | None,
        route: str | None,
    ) -> DraftMeetingView:
        names = {saved_id(team.id): team.name for team in teams}
        turn = state.next_turn()
        mine = turn is not None and turn.team_id == managed
        # いまの巡で確定した動き（球団ごと。1つの巡で球団が確定させるのは1回だけ）
        this_round = {
            move.team_id: move
            for move in state.moves
            if move.round_number == state.round_number and move.is_development == state.is_development
        }
        taken = {selection.prospect_key: selection.team_id for selection in state.selections}
        rows = [
            DraftProspectRow(
                key=prospect.key,
                name=prospect.rookie.name,
                position=prospect.position,
                route=prospect.route,
                age=prospect.age,
                grade=projected_grade(prospect, state.report(managed, prospect)),
                taken_by=names.get(taken[prospect.key], "") if prospect.key in taken else "",
                is_taken=prospect.key in taken,
                selectable=mine and state.selectable(managed, prospect),
            )
            for prospect in state.pool.prospects
        ]
        kept, chosen_position, chosen_route = domain_services.filter_prospects(rows, position=position, route=route)
        ordered, sort_key, sort_descending = domain_services.sort_prospects(kept, sort, descending)
        # 絞り込みの選択肢は候補にいる位置と経歴だけ（並びは値オブジェクトの定義の順）
        positions = {row.position for row in rows}
        routes = {row.route for row in rows}
        return DraftMeetingView(
            year=year,
            status=DraftMeetingStatus.of(record, state),
            blocker="",
            managed_team_id=managed,
            last_seq=record.last_seq,
            turn=None
            if turn is None
            else DraftTurnView(
                team_id=turn.team_id,
                team_name=names.get(turn.team_id, ""),
                round_number=turn.round_number,
                is_bid=turn.is_bid,
                attempt=turn.attempt,
                is_development=turn.is_development,
                is_mine=mine,
            ),
            round_rows=tuple(
                DraftRoundRow(
                    team_id=team_id,
                    team_name=names.get(team_id, ""),
                    is_mine=team_id == managed,
                    move=None if team_id not in this_round else _move_row(state, this_round[team_id], names),
                )
                for team_id in state.round_order
            ),
            own_moves=tuple(_move_row(state, move, names) for move in state.moves if move.team_id == managed),
            recent_moves=tuple(_move_row(state, move, names) for move in state.moves[-RECENT_MOVES:]),
            prospects=tuple(ordered),
            sort=sort_key,
            descending=sort_descending,
            can_pass=mine and state.passable(managed),
            # 絞り込みで隠れた候補も数える（隠れているだけで指名できる候補がいれば、任せられない）
            can_delegate=mine and not any(row.selectable for row in rows),
            prospect_total=len(rows),
            position_choices=tuple(choice for choice in Position if choice in positions),
            route_choices=tuple(choice for choice in DraftRoute if choice in routes),
            position_filter=chosen_position,
            route_filter=chosen_route,
            board=_meeting_board(state, teams, managed, names),
        )


def _meeting_board(
    state: DraftState, teams: Sequence[Team], managed: int, names: Mapping[int, str]
) -> DraftBoard | None:
    """会議の途中の盤（受け持つ球団のリーグの球団。#298 D4）。セルは確定した指名と、抽選になった入札。"""
    league_id = next((team.league_id for team in teams if team.id == managed), None)
    columns = [
        DraftBoardColumn(team_id=saved_id(team.id), team_name=team.name, is_mine=team.id == managed)
        for team in sorted(teams, key=lambda team: saved_id(team.id))
        if team.league_id == league_id
    ]
    # 抽選は当たった球団のその巡の指名に付く（1つの巡で球団が指名するのは1人）
    lotteries = {
        (lottery.winner, lottery.round_number, lottery.is_development): lottery for lottery in state.lotteries
    }
    picks = []
    for selection in state.selections:
        prospect = state.pool.get(selection.prospect_key)
        lottery = lotteries.get((selection.team_id, selection.round_number, selection.is_development))
        picks.append(
            BoardPick(
                round_number=selection.round_number,
                is_development=selection.is_development,
                cell=DraftBoardCell(
                    team_id=selection.team_id,
                    name=prospect.rookie.name,
                    position=prospect.position.label,
                    lottery=None if lottery is None else _lottery_view(lottery, names),
                ),
            )
        )
    return draft_board(columns, picks)


def _lottery_view(lottery: Lottery, names: Mapping[int, str]) -> DraftLotteryView:
    return DraftLotteryView(
        bidders=tuple(names.get(team_id, "") for team_id in lottery.bidders),
        winner=names.get(lottery.winner, ""),
        bidder_ids=lottery.bidders,
    )


def _move_row(state: DraftState, move: DraftMove, names: Mapping[int, str]) -> DraftMoveRow:
    prospect = None if move.prospect_key is None else state.pool.get(move.prospect_key)
    lottery = move.lottery
    return DraftMoveRow(
        seq=move.seq,
        team_id=move.team_id,
        team_name=names.get(move.team_id, ""),
        kind=move.kind,
        round_number=move.round_number,
        is_development=move.is_development,
        prospect_key=move.prospect_key,
        prospect_name="" if prospect is None else prospect.rookie.name,
        position=None if prospect is None else prospect.position,
        lottery=None if lottery is None else _lottery_view(lottery, names),
    )
