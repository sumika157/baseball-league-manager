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


class DuplicateSeasonRecord(DomainError):
    """同一チームに同じシーズンの成績が二重に登録された。"""


class TeamNotFound(DomainError):
    """指定されたチームが存在しない。"""


class PlayerNotFound(DomainError):
    """指定された選手が存在しない。"""


class DuplicateTeamName(DomainError):
    """同一リーグ内でチーム名が重複している。"""
