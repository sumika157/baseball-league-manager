"""参照クエリのインターフェース。

一覧表示のような参照は集約を組み立てず、リードモデルが直接 DTO を作る
（実装は infrastructure/queries.py）。アプリケーション層はその実装ではなく、
ここで宣言した形だけに依存する。

永続化のインターフェース（domain/repositories.py）と分けているのは、
参照クエリの戻り値が画面向けの DTO（application/dto.py）で、ドメイン層から
参照できないため。
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from datetime import date, datetime
from typing import Protocol, runtime_checkable

from ..domain.entities import Game
from ..domain.pennant.postseason import PostseasonResult
from ..domain.pennant.retirement import PlayingTime
from ..domain.value_objects import FieldingLine
from .dto import (
    ActivePlayerStats,
    AwardRow,
    ChampionshipRow,
    FaSubjects,
    GameNote,
    GameRow,
    HomeStadium,
    InvitationPreview,
    InvitationRow,
    LeagueSetupRow,
    MemberRow,
    OffseasonFacts,
    PeriodBatting,
    PeriodPitching,
    PlayerFielding,
    PostseasonGameRow,
    RatingsHistoryFacts,
    ServiceHistory,
    SimulationContext,
    SimulationTeam,
    StadiumSetupRow,
    TeamAnalysisFacts,
    TeamSetupRow,
    TeamSummary,
    WorkspaceContext,
    WorkspaceRow,
    WorldSummary,
)


@runtime_checkable
class PlayerStatsQuery(Protocol):
    """球団に在籍する選手の成績の参照。ランキング・タイトル・選手の一覧の材料。

    チーム（集約）を経由すると、順位づけに要らない経歴・主将歴・プロフィールまで
    組み立てる。ここでは成績を SQL で集計して、選手ごとの DTO にして返す。

    **在籍は年と期間で引く**: 年 Y に在籍していたのは `from_year <= Y かつ (to_year が空 または Y <= to_year)`
    の選手（`to_year` は最後に在籍した年で、その年を含む）。引退した選手も、在籍していた年のタイトルや成績に残る。
    `year` が None なら「いま在籍中」（退団年が空）の選手の通算成績。
    """

    def list_season(self, league_id: int, year: int) -> list[ActivePlayerStats]:
        """そのリーグに `year` に在籍していた選手と、その年の成績。

        成績に数えるのは、その年の**すべての試合**（リーグをまたぐ交流戦も含む。NPB のタイトルと同じ）。
        チームの表示順、背番号順。
        """
        ...

    def list_season_by_league(self, year: int) -> dict[int, list[ActivePlayerStats]]:
        """全リーグぶんの `list_season(league_id, year)` を一度に。キーはリーグid。

        リーグごとの中身は `list_season` と同じ（そのリーグに `year` に在籍していた選手と、交流戦も含む
        その年の成績）。読むのは範囲（`WorldScope`）の中だけ。在籍していた選手がいないリーグはキーに現れない。
        """
        ...

    def list_roster(
        self,
        *,
        year: int | None,
        team_id: int | None = None,
        league_id: int | None = None,
        with_profile: bool = False,
        with_games: bool = False,
    ) -> list[ActivePlayerStats]:
        """球団（`team_id`）またはリーグ（`league_id`）の選手と成績。どちらも None なら世界の全選手。

        `year` が None なら在籍中の選手の通算成績、年を渡せばその年に在籍していた選手のその年の成績
        （`list_season` と同じ数え方）。球団を指定したときだけ、主将かどうか（`is_captain`）を読む。
        `with_profile` を立てたときだけ、年齢などの材料（`profile`）を読む（タイトルには要らない）。
        `with_games` を立てたときだけ、出場試合数と登板数（`games` / `appearances`）を数える（一覧の列に使う）。
        """
        ...


@runtime_checkable
class FieldingTotalsQuery(Protocol):
    """選手の通算守備成績の参照（世界の作成時に、守備力の推定に使う）。"""

    def totals_for(self, player_ids: Sequence[int]) -> dict[int, FieldingLine]:
        """選手 id → 通算の守備成績。守備に就いた試合が無い選手は含めない。"""
        ...


@runtime_checkable
class SimulationContextQuery(Protocol):
    """日を進めるのに要る世界の状態の参照（ペナントの進行が使う）。

    球団・選手・直近の登板を、集約（`Team` / `Game`）を組み立てずにまとめて読む。
    **1回の「進める」で1度だけ読み**、その後の登板は呼び出し側がメモリで足していく。
    能力はここに含めない（`RatingsRepository` が出典）。
    """

    def last_played_on(self) -> date | None:
        """世界の「今日」。消化した最後の試合日で、まだ1試合も無ければ None。"""
        ...

    def teams(self) -> tuple[SimulationTeam, ...]:
        """球団と、その現在の選手と、リーグの外国人の枠だけ（直近の登板は読まない。編成の画面が使う）。"""
        ...

    def load(self, *, before: date) -> SimulationContext:
        """`before` より前の試合から導ける状態を読む。

        - 球団と、その現在の選手（登録位置・外国人かどうか）と、リーグの外国人の枠
        - 投手ごとの直近の先発の日（`before` より前のすべての試合から。期間を切ると、
          まとめて進めるか1日ずつ進めるかで「中何日か」の答えが変わる）
        - `before` の直前の数日の登板（連投の判断。数日あれば足りる）
        """
        ...


@runtime_checkable
class WorldSummaryQuery(Protocol):
    """世界の見出し（今日・局面の材料・受け持つ球団）の参照。世界の台帳そのもので、実装はワークスペースで絞る。

    **世界の数にかかわらず、一定のクエリ数でまとめて読む**（世界ごとにサービスを組み立てて読み直さない）。
    """

    def get(self, world_id: int) -> WorldSummary:
        """無ければ（ほかのワークスペースの世界でも）WorldNotFound。"""
        ...

    def list_all(self) -> list[WorldSummary]:
        """このワークスペースの世界を、新しく作った世界から順に。"""
        ...


@runtime_checkable
class PlayerFieldingQuery(Protocol):
    """選手の守備成績の参照。"""

    def for_player(self, player_id: int, team_id: int) -> PlayerFielding | None:
        """通算と、そのチームでの年度別。守備に就いた試合が1つも無ければ None。"""
        ...


@runtime_checkable
class HomeStadiumQuery(Protocol):
    """球団の本拠地の球場の参照。観客数が収容人数を超えていないかの照合の材料。

    球場のリポジトリは無く、`Team` 集約は球場の id しか持たないので、参照クエリで引く。
    """

    def home_stadium_of(self, team_id: int) -> HomeStadium | None:
        """球団の本拠地の名前と収容人数。本拠地が無い（または球団が範囲に無い）なら None。"""
        ...


@runtime_checkable
class TeamListQuery(Protocol):
    """チーム一覧の参照。"""

    def list_summaries(self) -> list[TeamSummary]:
        """全チームの概要を表示順で返す。"""
        ...


@runtime_checkable
class GameListQuery(Protocol):
    """試合一覧の参照。**公式戦だけを返す**（ポストシーズンの試合は含まない。どのメソッドも同じ）。"""

    def list_rows(
        self,
        *,
        year: int | None = None,
        team_id: int | None = None,
        month: int | None = None,
        league_id: int | None = None,
        after: date | None = None,
        through: date | None = None,
    ) -> list[GameRow]:
        """絞り込んだ公式戦を新しい順に返す（ポストシーズンを含まない）。`after` より後、`through` 以前に絞れる。"""
        ...

    def list_for_standings(self, *, year: int | None = None) -> list[Game]:
        """順位表の計算に渡す試合（公式戦だけ。ポストシーズンを含まない）。成績の明細は持たない。"""
        ...

    def list_for_player(self, team_id: int, player_id: int) -> list[Game]:
        """そのチームの公式戦のうち、選手が出場した試合（ポストシーズンを含まない）。

        その選手の打撃・投球の明細だけを持つ（参照専用）。試合日の古い順。
        """
        ...

    def list_seasons(self, *, league_id: int | None = None, recorded_only: bool = False) -> list[int]:
        """公式戦のある年を新しい順に返す（ポストシーズンの試合だけの年は含まない）。

        `league_id` を渡すと、そのリーグのチームが出た試合のある年だけ。
        `recorded_only` を立てると記録済みの試合だけを数える
        （タイトルや順位表の年の選択肢。未記録の試合しか無い年は選べない）。全シーズンの試合を組み立てずに年を知るためのもの。
        """
        ...

    def count_by_team(self, *, year: int | None = None) -> dict[int, int]:
        """チームid → 公式戦の試合数（ポストシーズンを含まない）。ホーム・ビジターの両方を数える。"""
        ...

    def list_months(
        self, *, year: int | None = None, team_id: int | None = None, league_id: int | None = None
    ) -> list[int]:
        """その絞り込みで公式戦がある月を昇順に返す（ポストシーズンを含まない）。"""
        ...

    def latest_year(self) -> int | None:
        """最新シーズン（公式戦のある年）。試合が1件も無ければ None。"""
        ...


@runtime_checkable
class PennantActivityQuery(Protocol):
    """GM ホームの「進めた結果」に要る、期間の見どころの参照（責任投手・本塁打・期間の成績）。

    試合の明細（打撃・投球）を SQL で集計するだけで、集約（`Game`）は組み立てない。
    """

    def game_notes(self, game_ids: Sequence[int]) -> dict[int, GameNote]:
        """試合ごとの責任投手（勝・敗・セーブ）と本塁打を打った選手。明細の無い試合は含めない。"""
        ...

    def batting_between(self, team_id: int, *, after: date, through: date) -> list[PeriodBatting]:
        """`after` より後、`through` 以前の、球団の打者ごとの打撃の合計。出場の無い選手は含めない。"""
        ...

    def pitching_between(self, team_id: int, *, after: date, through: date) -> list[PeriodPitching]:
        """同じ期間の、球団の投手ごとの勝・敗・セーブ。登板の無い選手は含めない。"""
        ...


@runtime_checkable
class PostseasonQuery(Protocol):
    """世界のポストシーズンの材料の参照。勝ち上がり（`PostseasonBracket`）を導くのに使う。

    試合を集約として組み立てず、日付・2球団・得点だけを読む。
    """

    def results(self, year: int) -> list[PostseasonResult]:
        """その年のポストシーズンの試合の結果（順不同）。"""
        ...

    def games(self, year: int) -> list[PostseasonGameRow]:
        """その年のポストシーズンの試合（日付の順）。表の試合の行と、勝ち上がりの導出の両方に使う（2回読まない）。"""
        ...

    def results_by_year(self, years: Collection[int]) -> dict[int, list[PostseasonResult]]:
        """`years` の各年のポストシーズンの試合の結果（年 -> 結果。試合の無い年は載らない）。1クエリで読む
        （歴代の記録・球団史が年ごとに読まない）。"""
        ...

    def years(self) -> list[int]:
        """ポストシーズンの試合のある年（新しい順）。"""
        ...

    def last_regular_date(self, year: int) -> date | None:
        """その年の最後の公式戦の日。公式戦が無ければ None。"""
        ...


@runtime_checkable
class PostseasonStatsQuery(Protocol):
    """世界のポストシーズンの個人成績の参照（#39 のユーザーの決定: 表を出す・保存しない）。

    公式戦の `PlayerStatsQuery` には区分の引数を足さない（公式戦の集計にポストシーズンを混ぜる口を作らない）。
    集計の式は公式戦と同じ（`repositories.batting_totals` などに区分 `POSTSEASON_ONLY` を渡す）。
    """

    def list_players(self, year: int) -> list[ActivePlayerStats]:
        """その年のポストシーズンに出場した選手（打撃か投球の明細がある選手だけ）と、その年のポストシーズンの成績。

        所属はその年の終わりの球団（同じ年に移った選手は1行）。出場試合数と登板数も読む。球団の表示順、背番号順。
        """
        ...


@runtime_checkable
class SeasonPlayingTimeQuery(Protocol):
    """世界のその年の出場機会の参照。シーズンを締めるときの引退の材料。

    打席数と投球のアウト数を、選手ごとに SQL で集計する（集約は組み立てない）。
    """

    def for_year(self, year: int) -> Mapping[int, PlayingTime]:
        """その年に打席に立った・投げた選手の出場機会。どちらも無い選手は含めない。"""
        ...


@runtime_checkable
class OffseasonQuery(Protocol):
    """世界のオフの結果の参照。締める処理は結果を保存しないので、在籍・プロフィール・能力から導く。

    - 締めた年 Y: 翌年 Y+1 の能力の行がある（開幕年より前を除く。domain の `is_season_closed`）
    - 引退・退団: 在籍の退団年が Y
    - 新人: 在籍の加入年が Y+1
    - 残った選手: Y と Y+1 の両方の能力がある
    """

    def has_ratings(self, year: int) -> bool:
        """その年の能力の行があるか。「締めたか」の判定は domain の `is_season_closed` が持つ（これはその材料）。"""
        ...

    def facts(self, year: int) -> OffseasonFacts:
        """Y 年のオフの材料。集約は組み立てず、SQL で読む。"""
        ...


@runtime_checkable
class RatingsHistoryQuery(Protocol):
    """選手ひとりの能力の推移の参照（選手ページ）。球団の集約を読まず、小さな3本のクエリで読む。"""

    def for_player(self, player_id: int) -> RatingsHistoryFacts | None:
        """範囲の外の選手は None。能力の無い選手は、能力が空の材料を返す。"""
        ...


@runtime_checkable
class TeamAnalysisQuery(Protocol):
    """戦力分析（球団×年度の編成）の参照。

    在籍と試合の明細を SQL で集計して材料だけを返す。区分けの規則は
    ドメイン層（`domain/services/roster_analysis.py`）にあり、ここは数えるだけ。
    """

    def list_years(self, team_id: int) -> list[int]:
        """そのチームの記録済みの試合がある年を新しい順に返す。"""
        ...

    def load(self, team_id: int, year: int) -> TeamAnalysisFacts:
        """その年にそのチームへ在籍していた選手と、その年の出場・登板の数、入退団の材料（在籍）。"""
        ...


@runtime_checkable
class ServiceHistoryQuery(Protocol):
    """選手の FA 資格の推定の材料（年ごとの出場・在籍の契約・FA 宣言）の参照。

    チームを問わず選手のキャリア通算で読む。実装は組み立て時に `WorldScope`（必須）を受け取り、その範囲の
    試合と在籍だけを読む。推定の規則はドメイン層（`domain/services/free_agency.py`）にあり、ここは材料を返すだけ。
    """

    def load_service_history(self, player_ids: list[int], year: int) -> ServiceHistory:
        """選手の year 年までの年ごとの初出場〜最終出場と、全在籍の契約・FA 宣言。固定本数。"""
        ...

    def load_fa_subjects(self, player_ids: list[int], through_year: int) -> FaSubjects:
        """選手の属性（ドラフト・外国人選手フラグ・学歴）と最初の在籍の加入年・FA 宣言・記録のある最初の年。

        `through_year` 年までを読む。出場（`load_service_history`）より軽い前段で、外国人枠の対象外を導くとき、
        取得しえない選手の出場を読まないために使う。固定本数。
        """
        ...


@runtime_checkable
class AwardQuery(Protocol):
    """ペナントの表彰（締めたシーズンのタイトル受賞者）の参照。選手名と、受賞した年の球団名を添える。

    書くのは domain の `AwardRepository`（シーズンを締める処理だけ）。絞り込みはどれも任意で、範囲（世界）は
    組み立てるときに必須で渡す。並びは年 → 部門（`Title` の定義順）→ 選手名。
    """

    def list_awards(
        self, *, league_id: int | None = None, team_id: int | None = None, player_id: int | None = None
    ) -> list[AwardRow]:
        """範囲の表彰。リーグ・球団（受賞した年の所属）・選手で絞れる。"""
        ...


@runtime_checkable
class WorkspaceAccessQuery(Protocol):
    """URL の key と利用者から、ワークスペースとその人の権限を引く台帳（範囲は取らない）。

    読むのはワークスペースの行・編集者の行の有無・担当チームの id という**権限の事実**だけで、判定は
    domain の `WorkspaceAccess` が行う。ワークスペースの中のデータ（リーグ・試合など）は読まない。
    """

    def resolve(self, key: str, user_id: int | None) -> WorkspaceContext:
        """key のワークスペースと、`user_id`（未ログインなら None）の権限。無い key は `WorkspaceNotFound`。

        見えるかどうか（`access.can_view()`）は呼ぶ側が見る（非公開でも行は返す。部外者には `NONE` の権限）。
        """
        ...


@runtime_checkable
class WorkspaceListQuery(Protocol):
    """自分が入っているワークスペースの一覧（`/`）。範囲は取らない台帳。"""

    def list_for(self, user_id: int) -> list[WorkspaceRow]:
        """持ち主・編集者・チーム担当として入っているワークスペースを名前順に。

        メンバーでないワークスペースは公開でも出さない（公開ワークスペースの一覧は作らない。2026-10-03 Q3）。
        """
        ...


@runtime_checkable
class WorkspaceManagementQuery(Protocol):
    """1つのワークスペースのメンバーと招待（W4。メンバーの画面）。実装はワークスペースの id を必須で受け取る。

    読むのはそのワークスペースのメンバー（持ち主・編集者の行・実データのチームの担当）と招待だけ。
    """

    def members(self) -> list[MemberRow]:
        """持ち主 → 編集者 → チーム担当の順（同じ役割は名前順）。重なった行は上の役割だけ（`members_from_rows`）。"""
        ...

    def invitations(self, now: datetime) -> list[InvitationRow]:
        """未使用で期限内の招待を、新しく発行した順に。期限切れは出さない（使えないので取り消す対象でもない）。"""
        ...


@runtime_checkable
class InvitationPreviewQuery(Protocol):
    """招待のリンク（トークン）から、招待の中身を引く台帳（範囲は取らない。ワークスペースが決まる前に引く）。"""

    def preview(self, token: str, user_id: int | None, now: datetime) -> InvitationPreview:
        """使える招待の中身。無い・期限切れ・使えない（担当チームが消えた）は `InvalidInvitation`（同じ文言）。"""
        ...


@runtime_checkable
class ChampionshipQuery(Protocol):
    """ペナントの日本一の記録（締めたシーズンのポストシーズンの優勝球団）の参照。球団名を添える。

    書くのは domain の `ChampionshipRepository`（シーズンを締める処理だけ）。範囲（世界）は組み立てるときに必須で渡す。
    """

    def list_championships(self) -> list[ChampionshipRow]:
        """範囲の日本一の記録（古い順）。"""
        ...


@runtime_checkable
class SetupQuery(Protocol):
    """登録画面（リーグ・チーム・球場の作成・編集・削除。W3 #79）の参照。**実データの範囲だけ**。

    一覧は行数によらず一定のクエリ数で読む（数は相関した副問い合わせで数え、多段の prefetch を使わない）。
    消せない理由・所属リーグを変えられない理由は、DTO が件数から domain の規則で作る。
    1行を引くもの（編集フォームの初期値）は、範囲の外の id なら `*NotFound`。
    """

    def leagues(self) -> list[LeagueSetupRow]:
        """範囲のリーグを表示順に。"""
        ...

    def league(self, league_id: int) -> LeagueSetupRow:
        """範囲の外は `LeagueNotFound`。"""
        ...

    def teams(self) -> list[TeamSetupRow]:
        """範囲のチームを、リーグの表示順・チームの表示順に。"""
        ...

    def team(self, team_id: int) -> TeamSetupRow:
        """範囲の外は `TeamNotFound`。"""
        ...

    def stadiums(self) -> list[StadiumSetupRow]:
        """範囲（ワークスペース）の球場を名前の順に。"""
        ...

    def stadium(self, stadium_id: int) -> StadiumSetupRow:
        """範囲の外は `StadiumNotFound`。"""
        ...
