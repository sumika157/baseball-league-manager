"""リポジトリのインターフェース。

ドメイン層は「永続化できる」ことだけを知り、それが Django ORM なのか
他の手段なのかは知らない。実装は infrastructure 層に置く。

**`TeamRepository` / `GameRepository` / `LeagueRepository` / `RatingsRepository` / `FixtureRepository` は、
組み立てるときに `WorldScope` を必須で受け取る**（実装のコンストラクタの話で、このインターフェースには
現れない）。読み出しはすべてその範囲に絞られ、範囲の外の id は「見つからない」になる。
`WorldRepository` だけは世界そのものの台帳なので範囲を持たない。
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from datetime import date
from typing import Protocol, runtime_checkable

from .entities import Game, League, Team
from .pennant.awards import Award
from .pennant.championship import Championship
from .pennant.club_plan import ClubPlan
from .pennant.development import DevelopmentPlan
from .pennant.draft_lottery import DraftLotteryRecord
from .pennant.draft_meeting import DraftAction
from .pennant.draft_record import DraftRecord
from .pennant.ratings import PlayerRatings
from .pennant.reinforcement import MidseasonRegistration
from .pennant.release import ReleasePlan
from .pennant.schedule import Fixture
from .pennant.world import World


@runtime_checkable
class TeamRepository(Protocol):
    """Team 集約の永続化。読み書きは常に集約単位で行う。"""

    def find_by_id(self, team_id: int) -> Team:
        """ロスター込みでチームを取得する。存在しなければ TeamNotFound。"""
        ...

    def roster_signature(self) -> tuple[int, int, int, int]:
        """範囲の在籍の目印（行数・現在在籍の人数・最大の id・昇格した在籍の数）。加入・退団・昇格があれば変わる。

        外で読んだ名簿が、書く前に変わっていないかを確かめるのに使う（クエリ1本）。昇格は在籍の行を書き換えるだけで
        行数も id も変えないので、昇格した在籍の数を足してある（昇格は取り消さないので単調に増える。#170 P3a）。
        """
        ...

    def find_all(self) -> list[Team]:
        """全チームを取得する（ロスターは含めない軽量版）。"""
        ...

    def find_by_league_with_roster(self, league_id: int) -> list[Team]:
        """そのリーグのチームを、ロスター込みで取得する。

        リーグ平均（FIP 定数・OPS+/ERA+ の基準）やリーグ内のランキングに使う。
        全チームを読んでから Python で絞ると、他リーグの選手の通算成績まで
        組み立てることになる。
        """
        ...

    def find_all_with_roster(self) -> list[Team]:
        """全チームをロスター込みで取得する。リーグ全体の順位づけに使う。"""
        ...

    def find_by_ids_with_roster(self, team_ids: Collection[int]) -> list[Team]:
        """渡した id のチームだけを、ロスター込みで取得する（id の順）。1つでも範囲に無ければ TeamNotFound。

        書き換える球団だけを読む（AI の期中の昇格。#170 P3b）。全球団を読むと、使わない選手の通算成績まで組み立てる。
        """
        ...

    def save(self, team: Team) -> Team:
        """集約の変更内容を永続化する。範囲の外のリーグ・チームには書けない。

        今の行と比べて変わった行だけを書く（保存した後の状態は、集約の全体を書き直したのと同じ）。
        集約が持っていない行は消さない。
        """
        ...


@runtime_checkable
class GameRepository(Protocol):
    """Game 集約の永続化。試合は2チームにまたがるため Team とは別の集約。"""

    def find_by_id(self, game_id: int) -> Game:
        """打撃・投球・イニングスコア・打席の記録込みで取得する。無ければ GameNotFound。

        区分（公式戦・ポストシーズン）を問わない（試合詳細はポストシーズンの試合も開ける）。

        打席まで読むのはこれだけ。1試合で約280行あり、まとめて読む用途に付けると
        数十万行になるため、下の3つは打席を省いて読む（省いた集約は
        `plate_appearances_loaded` が False になり、保存しても打席に触れない）。
        """
        ...

    def find_all(self, year: int | None = None) -> list[Game]:
        """公式戦の全試合を取得する（ポストシーズンは含まない）。年を渡すとそのシーズンだけ。打席は含まない。"""
        ...

    def find_by_team(self, team_id: int, year: int | None = None) -> list[Game]:
        """そのチームが出場した公式戦を取得する（ホーム・ビジターの別を問わない。ポストシーズンは含まない）。打席は含まない。"""
        ...

    def find_between_teams(self, team_ids: set[int], year: int | None = None) -> list[Game]:
        """渡したチームどうしの公式戦だけを取得する（両チームが対象に含まれるもの。ポストシーズンは含まない）。

        リーグ内の試合を集めるのに使う。全試合を読んでから Python で捨てると、
        使わない試合の明細まで組み立てることになる。
        """
        ...

    def save(self, game: Game) -> Game:
        """集約の変更内容を永続化する。範囲の外のチームの試合には書けない。"""
        ...

    def add_all(self, games: Sequence[Game]) -> None:
        """新しい試合をまとめて保存する（保存した試合の id は集約に入る）。

        `save()` と同じ検査を通す（明細と打席の照合 = `ensure_lines_match_plate_appearances`、
        範囲の外のチームには書けない）。**検査は書き込みの前に全試合ぶん済ませる**ので、
        1試合でも通らなければ何も書かない。すでに保存した試合（id がある）は渡せない
        （InvalidGame。更新は `save()`）。新しい集約だけが対象なので、打席を読んだかどうか
        （`plate_appearances_loaded`）は見ない。1試合ごとに `save()` するより桁違いに速い
        （1試合あたり約50回の update_or_create を、種類ごとの一括書き込みにする）。
        """
        ...

    def prepare_add_all(self, games: Sequence[Game]) -> Callable[[], None]:
        """`add_all` の書く前の仕事（検査と行の組み立て）だけを済ませ、書き込みの関数を返す。

        書き込みトランザクションの外で呼び、返った関数だけをトランザクションの中で呼ぶ
        （書き込みロックを持つ時間を、SQL を流す間だけにする）。検査は `add_all` と同じで、
        通らなければここで例外になり何も書かない。返した関数を呼ばなければ何も書かれない。
        **返した関数は1回だけ呼ぶ**（呼ぶと試合に id が入る）。失敗したら、`prepare_add_all` からやり直す。
        """
        ...


@runtime_checkable
class LeagueRepository(Protocol):
    """リーグの永続化。"""

    def find_by_id(self, league_id: int) -> League: ...

    def find_all(self) -> list[League]: ...

    def save(self, league: League) -> League:
        """リーグを保存する。新しいリーグは範囲の世界に属する形で作られる。"""
        ...


@runtime_checkable
class WorldRepository(Protocol):
    """世界の台帳。世界そのものの出入りだけを扱い、範囲は持たない。"""

    def find_by_id(self, world_id: int) -> World:
        """無ければ WorldNotFound。"""
        ...

    def find_all(self) -> list[World]:
        """新しく作った世界から順に。"""
        ...

    def count_by_owner(self, owner_id: int) -> int:
        """そのユーザーがオーナーの世界の数。世界の数の上限の検査に使う（世界を組み立てずに数える）。"""
        ...

    def save(self, world: World) -> World:
        """世界を保存する。"""
        ...

    def delete(self, world_id: int) -> None:
        """世界と、その世界に属するリーグ・球団・選手・試合・能力をすべて消す。無ければ WorldNotFound。"""
        ...


@runtime_checkable
class FixtureRepository(Protocol):
    """世界の未消化の対戦（日程）。消化したら `Game` を作って消す。

    **試合のあとまで残さない**（日付と対戦カードの出典が `Game` と2つになるため）。
    ペナントの世界にだけ置ける（実データには日程の概念が無い）。
    """

    def add_all(self, fixtures: Sequence[Fixture]) -> None:
        """日程を一括で保存する。範囲の外の球団の対戦は書けない（TeamNotFound）。

        同じ日・同じ対戦がすでにある、または渡した中で重複しているときは InvalidSchedule で、何も書かない。
        """
        ...

    def find_all(self) -> list[Fixture]:
        """範囲の未消化の対戦を、日付の順（同じ日はホームの id の順）に。"""
        ...

    def first_date(self, team_id: int | None = None) -> date | None:
        """範囲の未消化の対戦のうち最初の日。無ければ None。日程を全件読まずに「次の試合」を知るため。

        `team_id` を渡すと、その球団がホームかビジターの対戦のうち最初の日（自軍が休みの日は数えない）。
        """
        ...

    def remove(self, fixtures: Sequence[Fixture]) -> None:
        """消化した対戦を消す。

        渡した対戦のどれかが無い（すでに消化されている）ときは InvalidSchedule で、何も消さない。
        同じ日程を二重に消化して、同じ対戦の試合が2つできるのを防ぐ。
        """
        ...


@runtime_checkable
class RatingsRepository(Protocol):
    """世界の中の選手の能力。選手 × 年で1組。"""

    def add_all(self, ratings: Sequence[PlayerRatings]) -> None:
        """能力を一括で保存する（ある年の能力を足す）。

        範囲の外の選手には書けない（PlayerNotFound）。次は InvalidRatings で、**何も書かない**:
        登録位置と能力の種類が合わない（投手に野手の能力、またはその逆）、同じ選手 × 年が
        渡した中で重複している・すでに保存済みである。
        書き込みはまとめて行う（選手ごとに1回ずつ書かない）。ただし1回の SQL に載せる行数には
        データベースの変数の上限があるので、約1,600人ぶんは数百行ずつの数回に分かれる。
        """
        ...

    def find_by_year(self, year: int) -> list[PlayerRatings]:
        """その年の、範囲の全選手の能力。選手の id の順。"""
        ...

    def find_by_player(self, player_id: int) -> list[PlayerRatings]:
        """ひとりの選手の能力を、年の順に。範囲の外の選手や能力の無い選手は空。"""
        ...

    def find_by_players(self, player_ids: Sequence[int], year: int) -> list[PlayerRatings]:
        """指定した選手たちの、その年の能力。選手の id の順。範囲の外の選手や、その年の能力が無い選手は含まれない。

        球団の表のように、選手の数が決まっている画面が、範囲の全選手を読まずに済ませるため。
        選手ごとにクエリを投げず、1回でまとめて読む。
        """
        ...


@runtime_checkable
class ClubPlanRepository(Protocol):
    """球団の編成の上書き（`ClubPlan`）。球団ごとに1つで、自動の区画は持たない。

    ペナントの世界にだけ置ける（実データには編成の概念が無い）。
    """

    def find_all(self) -> list[ClubPlan]:
        """範囲の、上書きを持つ球団の編成。すべて自動の球団は含まない。球団の id の順。"""
        ...

    def find_by_team(self, team_id: int) -> ClubPlan:
        """その球団の編成。上書きが無ければ、すべて自動の編成を返す。範囲の外の球団は TeamNotFound。"""
        ...

    def save(self, plan: ClubPlan) -> ClubPlan:
        """編成を保存する（区画ごとに丸ごと置き換える）。すべて自動なら保存済みの行を消す。

        範囲の外の球団には書けない（TeamNotFound）。実データの範囲には書けない（InvalidWorld）。
        球団に在籍していない選手が入っていても保存は止めない（在籍は変わるので、使える
        かどうかは進めるときに `resolve_club` が判断する）。範囲の外の選手だけは書けない（PlayerNotFound）。
        """
        ...


@runtime_checkable
class ReleasePlanRepository(Protocol):
    """GM の戦力外の指名（`ReleasePlan`）。球団ごとに1つで、自動（`player_ids` が None）は行を持たない。

    ペナントの世界にだけ置ける。締める処理が使い終わったら消す（指名は入力で、事実は在籍の退団の経路に残る）。
    """

    def find_by_team(self, team_id: int) -> ReleasePlan | None:
        """その球団の手動の指名。自動なら None。範囲の外の球団は TeamNotFound。"""
        ...

    def save(self, plan: ReleasePlan) -> ReleasePlan:
        """指名を丸ごと置き換える。自動（`player_ids` が None）なら保存済みの行を消す。

        範囲の外の球団には書けない（TeamNotFound）。実データの範囲には書けない（InvalidWorld）。
        範囲の外の選手は書けない（PlayerNotFound）。
        """
        ...

    def delete_all(self) -> None:
        """範囲の指名をすべて消す（締める処理が、使い終わった指名と締める年の違う指名を消す）。"""
        ...


@runtime_checkable
class DevelopmentPlanRepository(Protocol):
    """GM の育成選手のオフの指名（`DevelopmentPlan`。#170 P2）。球団ごとに1つで、自動（`decisions` が None）は
    行を持たない。

    ペナントの世界にだけ置ける。締める処理が使い終わったら消す（指名は入力で、事実は在籍の昇格の年と退団の経路に残る）。
    """

    def find_by_team(self, team_id: int) -> DevelopmentPlan | None:
        """その球団の手動の指名。自動なら None。範囲の外の球団は TeamNotFound。"""
        ...

    def save(self, plan: DevelopmentPlan) -> DevelopmentPlan:
        """指名を丸ごと置き換える。自動（`decisions` が None）なら保存済みの行を消す。

        範囲の外の球団には書けない（TeamNotFound）。実データの範囲には書けない（InvalidWorld）。
        範囲の外の選手は書けない（PlayerNotFound）。
        """
        ...

    def delete_all(self) -> None:
        """範囲の指名をすべて消す（締める処理が、使い終わった指名と締める年の違う指名を消す）。"""
        ...


@runtime_checkable
class MidseasonRegistrationRepository(Protocol):
    """期中の支配下登録の記録（`MidseasonRegistration`。#170 P3a）。書くのは期中の登録の操作だけ。

    ペナントの世界にだけ置ける。在籍は年単位なので、オフの昇格と期中の昇格を区別するために登録日だけを足す
    （`domain/pennant/reinforcement.py`）。
    """

    def add(self, registration: MidseasonRegistration) -> None:
        """登録を1つ書く。在籍の昇格の年と登録日の年が合わなければ InvalidContract
        （`reinforcement.ensure_registration_matches_stint`）。

        実データの範囲には書けない（InvalidWorld）。範囲の外の球団は TeamNotFound、球団に在籍していない選手は
        PlayerNotFound。
        """
        ...

    def find_by_team(self, team_id: int) -> list[MidseasonRegistration]:
        """その球団の期中の登録（登録日・選手の id の順）。範囲の外の球団は TeamNotFound。"""
        ...

    def find_in_year(self, year: int) -> list[MidseasonRegistration]:
        """範囲の全球団の、登録日が `year` 年の期中の登録（球団・登録日・選手の id の順）。

        AI の期中の昇格（#170 P3b）が、判定日に球団ごとのその年の登録を1本で数える。
        """
        ...


@runtime_checkable
class AwardRepository(Protocol):
    """ペナントの表彰（締めたシーズンのタイトル受賞者。`Award`）。書くのはシーズンを締める処理だけ。

    ペナントの世界にだけ置ける。画面に出すときは参照クエリ（`application/queries.py` の `AwardQuery`）で、
    選手名・球団名を添えて読む（更新と参照を分ける）。
    """

    def add_all(self, awards: Sequence[Award]) -> None:
        """表彰をまとめて保存する。すでに表彰のある年をもう一度書くことはできない（InvalidAward）。

        実データの範囲には書けない（InvalidWorld）。範囲の外のリーグ・球団・選手は書けない（InvalidAward）。
        """
        ...

    def recorded_years(self) -> frozenset[int]:
        """表彰を記録してある年。"""
        ...

    def count_by_player(self) -> dict[int, int]:
        """選手ごとの表彰の数（戦力外の実績の加点の材料）。表彰の無い選手は含まない。"""
        ...


@runtime_checkable
class ChampionshipRepository(Protocol):
    """ペナントの日本一の記録（締めたシーズンの優勝球団。`Championship`）。書くのはシーズンを締める処理だけ。

    ペナントの世界にだけ置ける。画面に出すときは参照クエリ（`application/queries.py` の `ChampionshipQuery`）で読む。
    """

    def add(self, championship: Championship) -> None:
        """日本一を保存する。すでに記録のある年をもう一度書くことはできない（InvalidChampionship）。

        実データの範囲には書けない（InvalidWorld）。範囲の外の球団は書けない（InvalidChampionship）。
        """
        ...


@runtime_checkable
class DraftRepository(Protocol):
    """ペナントのドラフト会議の記録（`DraftRecord`。世界 × 年）。#297 D3。

    ペナントの世界にだけ置ける。会議は締める前の入力で、締める処理が使い終わったら消す（指名の事実は新人の
    `Profile.draft` と在籍の経路に残る）。行動は `append` で足すだけで、書き換えない。
    """

    def find(self, year: int) -> DraftRecord | None:
        """その年の会議の記録（人数の幅と、行動の並び）。始めていなければ None。"""
        ...

    def last_seq(self, year: int) -> int | None:
        """その年の会議の最後の行動の番号（行動が無ければ 0）。始めていなければ None。

        会議の目印（締める処理が状態の目印と並べて前後で比べる）と、書く前に「画面を開いたときと同じか」を比べるのに使う（クエリ1本）。
        """
        ...

    def start(self, record: DraftRecord) -> None:
        """会議を始める（記録の人数の幅と行動を書く）。その年の会議が既にあれば `DraftStarted` で、何も書かない。

        実データの範囲には書けない（InvalidWorld）。範囲の外の球団は書けない（TeamNotFound）。
        """
        ...

    def append(self, year: int, actions: Sequence[DraftAction], *, expected_seq: int) -> None:
        """行動を足す。最後の番号が `expected_seq` と違う（画面が古い・二重送信・別のタブで先に進めた）・
        その年の会議が無い（締めた・やり直した）ときは `DraftConflict` で、何も書かない。

        読み直しと書き込みは1つのトランザクションで行う（本番の SQLite は IMMEDIATE で書き込みが直列なので、
        読み直しで足りる）。
        直列にならない設定ですり抜けても、会議 × 番号の一意制約が重複を書かせない（最後の砦。SQLite の DEFERRED では
        `DraftConflict` ではなくロックのエラーになりうる）。
        会議に参加していない球団の行動は書けない（InvalidDraftAction）。
        """
        ...

    def remove(self, year: int) -> None:
        """その年の会議の記録を消す（締める処理・会議のやり直し）。無ければ何もしない。"""
        ...


@runtime_checkable
class DraftLotteryRepository(Protocol):
    """ペナントの締めた年のドラフトの抽選の記録（`DraftLotteryRecord`。#298 D4）。書くのはシーズンを締める処理だけ。

    ペナントの世界にだけ置ける。画面に出すときは参照クエリ（`OffseasonQuery.facts`）で読む。
    """

    def add_all(self, records: Sequence[DraftLotteryRecord]) -> None:
        """抽選の記録を保存する。既に記録のある選手をもう一度書くことはできない（InvalidDraftAction）。

        実データの範囲には書けない（InvalidWorld）。範囲の外の選手・球団は書けない（InvalidDraftAction）。
        どれかが書けなければ何も書かない。
        """
        ...
