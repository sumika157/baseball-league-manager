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


class InvalidProfile(DomainError):
    """プロフィールの値が不正（現実的でない身長など）。"""


class InvalidStint(DomainError):
    """在籍期間として成立しない（退団年が加入年より前など）。"""


class TeamNotFound(DomainError):
    """指定されたチームが存在しない。"""


class PlayerNotFound(DomainError):
    """指定された選手が存在しない。"""


class DuplicateCaptain(DomainError):
    """同一チーム内で主将が重複している。"""


class PlayerNotEligibleForCaptaincy(DomainError):
    """在籍していない選手（退団済み・他チーム所属）を主将にしようとした。"""


class InvalidCaptaincy(DomainError):
    """主将在任期間として成立しない（退任年が就任年より前など）。"""


class ForeignPlayerQuotaExceeded(DomainError):
    """外国人選手の人数が上限を超えている（登録枠・試合出場枠のどちらにも使う）。"""
