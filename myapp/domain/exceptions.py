"""ドメイン層の例外。

Django の ValidationError を使わないことで、ドメイン層が Web フレームワークから
独立していることを保証する。プレゼンテーション層でこれを捕捉して画面表示に変換する。
"""


class DomainError(Exception):
    """業務ルール違反を表す基底例外。"""


class InvalidJerseyNumber(DomainError):
    """背番号として許されない値。"""


class DuplicateJerseyNumber(DomainError):
    """同一チーム内で背番号が重複している。"""


class InvalidPosition(DomainError):
    """守備位置として認識できない値。"""


class InvalidInningsPitched(DomainError):
    """投球回として許されない値。"""


class InvalidStatValue(DomainError):
    """成績の数値として許されない値（負数など）。"""


class InvalidSeason(DomainError):
    """シーズン（年）として許されない値。"""


class InvalidGame(DomainError):
    """試合として成立しない内容（同一チーム同士、負の得点など）。"""


class InvalidPlateAppearance(DomainError):
    """打席の記録として成立しない内容。

    打順が1〜9でない、打者の進塁が結果と食い違う、同じ塁に2人の走者がいる、
    1つの半回にアウトが4つある、など。スコアブックとして読めない記録を弾く。
    """


class GameNotFound(DomainError):
    """指定された試合が存在しない。"""


class LeagueNotFound(DomainError):
    """指定されたリーグが存在しない。"""


class WorldNotFound(DomainError):
    """指定された世界が存在しない。"""


class InvalidWorld(DomainError):
    """世界として成立しない内容（名前が空、シードが範囲外など）。"""


class InvalidWorkspace(DomainError):
    """ワークスペースとして成立しない内容（名前が空、key の形が違うなど）。"""


class WorkspaceNotFound(DomainError):
    """ワークスペースが無い（書き込み先を決められない）。"""


class InvalidProfile(DomainError):
    """プロフィールの値が不正（現実的でない身長など）。"""


class InvalidDraftPick(InvalidProfile):
    """ドラフトの指名として成立しない（0位、育成ドラフトの入団なのに本指名など）。"""


class InvalidStint(DomainError):
    """在籍期間として成立しない（退団年が加入年より前など）。"""


class TeamNotFound(DomainError):
    """指定されたチームが存在しない。"""


class StadiumNotFound(DomainError):
    """指定された球場が範囲（ワークスペース）に存在しない。ほかのワークスペースの球場も、ここでは「無い」。"""


class PlayerNotFound(DomainError):
    """指定された選手が存在しない。"""


class DuplicateCaptain(DomainError):
    """同一チーム内で主将が重複している。"""


class PlayerNotEligibleForCaptaincy(DomainError):
    """在籍していない選手（退団済み・他チーム所属）を主将にしようとした。"""


class InvalidCaptaincy(DomainError):
    """主将在任期間として成立しない（退任年が就任年より前など）。"""


class ForeignPlayerQuotaExceeded(DomainError):
    """外国人選手の人数が外国人枠（協約第82条の2。1軍登録・試合の出場者）を超えている。"""


class InvalidRatings(DomainError):
    """能力値として許されない値（1〜100 の整数でない、など）。"""


class InvalidRoster(DomainError):
    """シミュレーションを行えないロスター（打順を組める野手がいない、投手がいない、など）。"""


class InvalidClubPlan(DomainError):
    """球団の編成（1軍登録・オーダー・ローテーション・抑え）として成立しない上書き。"""


class InvalidReleasePlan(DomainError):
    """戦力外の指名として成立しない（支配下の在籍選手でない・位置の最低人数を割る・試合を組めなくなる）。"""


class InvalidAward(DomainError):
    """表彰として保存できない内容（同じ年の表彰を2度書く、範囲の外の選手など）。"""


class InvalidChampionship(DomainError):
    """日本一の記録として保存できない内容（同じ年を2度書く、範囲の外の球団など）。"""


class AlreadyAdvanced(DomainError):
    """画面を開いたときから世界が進んでいる（二重送信や、別の画面で先に進めた場合）。"""


class InvalidSchedule(DomainError):
    """日程が組めない（球団数が奇数・リーグが1つ・規則が成立しないなど）、または日程の指定が不正。"""


class InvalidPostseason(DomainError):
    """ポストシーズンの試合が、結果から導いた勝ち上がりと食い違う（決着したシリーズの試合・同じ日の二重の試合・
    出場していない球団・導いた日と違う日の試合など）。"""


class SeasonNotFinished(DomainError):
    """シーズンがまだ終わっていない（未消化の対戦が残っている、または試合を一度もしていない）ので締められない。"""


class PostseasonNotFinished(DomainError):
    """ポストシーズンがまだ終わっていない（始まっていない・進行中）ので、シーズンを締められない。"""


class PostseasonFormatLocked(DomainError):
    """ポストシーズンの方式を変えられない（いまの年度のポストシーズンが始まっている・世界が終了している）。

    理由の文言は domain の `world.postseason_format_change_blocker` が出典。
    """


class DraftOrderRuleLocked(DomainError):
    """ドラフトの指名順の規則を変えられない（世界が終了している）。

    理由の文言は domain の `world.draft_order_rule_change_blocker` が出典。
    """


class InvalidDraftAction(DomainError):
    """ドラフト会議の行動として成立しない（自分の番でない・候補にいない・既に指名された・人数の幅を超える・
    最少に届く前の選択終了・位置の最低人数を割る・会議が終わっている）。"""


class AlreadyClosed(DomainError):
    """そのシーズンは既に締めている（二重送信や、別の画面で先に締めた場合）。

    `year` は、締めていると確かめられた年（画面がオフの結果へ案内する行き先）。確かめられないときは None。
    """

    def __init__(self, message: str, *, year: int | None = None) -> None:
        super().__init__(message)
        self.year = year


class SeasonLimitReached(DomainError):
    """世界のシーズン数の上限に達していて、これ以上締められない。"""


class InvalidContract(DomainError):
    """契約区分（支配下／育成）として成立しない内容。

    区分と背番号の食い違い、昇格の年が在籍期間の外、育成でない選手の昇格など。
    """


class InvalidAcquisition(DomainError):
    """入団の経路として成立しない内容（育成ドラフトなのに支配下で加入、FA なのに宣言が無いなど）。"""


class InvalidFreeAgentDeclaration(DomainError):
    """FA 宣言として成立しない内容（同じ年に2回、宣言した年にどこにも在籍していないなど）。"""


class RegisteredPlayerLimitExceeded(DomainError):
    """支配下選手の人数が上限を超えている。"""


class InvalidName(DomainError):
    """リーグ・チーム・球場の名前として許されない（空・長すぎる）。"""


class DuplicateName(DomainError):
    """同じ範囲（ワークスペースの実データ・ペナントの世界）に同じ名前のリーグ・チーム・球場がある。"""


class NotDeletable(DomainError):
    """消せない状態のリーグ・チーム・球場を消そうとした。

    理由の文言は domain の `setup.league_deletion_blocker` などが出典。
    """


class LeagueChangeLocked(DomainError):
    """チームの所属リーグを変えられない（試合がある）。理由の文言は domain の `setup.league_change_blocker` が出典。"""
