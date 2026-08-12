"""Django がアプリ読み込み時に必ずインポートするモジュール。

実体は infrastructure/orm_models.py にあり、ここでは再輸出のみを行う。
モデル定義をインフラ層へ寄せつつ、Django のモデル登録とマイグレーションを
従来どおり動作させるための橋渡し。
"""

from .infrastructure.orm_models import (  # noqa: F401
    POSITION_CHOICES,
    Game,
    GameBattingLine,
    GameFieldingError,
    GameInningScore,
    GamePitchingLine,
    GamePlateAppearance,
    GameRunnerAdvance,
    GameRunnerSubstitution,
    League,
    Player,
    PlayerStint,
    Stadium,
    Team,
)

__all__ = [
    "POSITION_CHOICES",
    "Game",
    "GameBattingLine",
    "GameFieldingError",
    "GameInningScore",
    "GamePitchingLine",
    "GamePlateAppearance",
    "GameRunnerAdvance",
    "GameRunnerSubstitution",
    "League",
    "Player",
    "PlayerStint",
    "Stadium",
    "Team",
]
