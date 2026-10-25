"""選手のプロフィールの入力（`ProfileInput`）と domain の `Profile` の行き来。

入力から `Profile` を組み立てるときに、値の検査（身長・体重の上限、ドラフトの年・順位、
年の無い順位・育成）が domain で行われる。管理画面の選手の編集と同じ規則（どちらも domain が出典）。
"""

from __future__ import annotations

from ..domain.value_objects import DraftPick, Handedness, Profile
from .dto import ProfileInput


def profile_from_input(data: ProfileInput) -> Profile:
    """入力をプロフィールにする。不正な値は domain の例外（`InvalidProfile` など）になる。"""
    return Profile(
        birth_date=data.birth_date,
        throws=Handedness.from_label(data.throws),
        bats=Handedness.from_label(data.bats),
        height_cm=data.height_cm,
        weight_kg=data.weight_kg,
        birthplace=data.birthplace,
        draft=DraftPick.from_inputs(data.draft_year, data.draft_round, is_development=data.is_development_draft),
        high_school=data.high_school,
        university=data.university,
        corporate_team=data.corporate_team,
        nationality=data.nationality,
        name_kana=data.name_kana,
        back_name=data.back_name,
        is_foreign_player=data.is_foreign_player,
    )


def profile_input_of(profile: Profile) -> ProfileInput:
    """プロフィールを入力の形にする（編集画面の初期値）。`profile_from_input` の逆。"""
    draft = profile.draft
    return ProfileInput(
        name_kana=profile.name_kana,
        back_name=profile.back_name,
        birth_date=profile.birth_date,
        throws=profile.throws.label if profile.throws else "",
        bats=profile.bats.label if profile.bats else "",
        height_cm=profile.height_cm,
        weight_kg=profile.weight_kg,
        birthplace=profile.birthplace,
        draft_year=draft.year if draft else None,
        draft_round=draft.round if draft else None,
        is_development_draft=draft.is_development if draft else False,
        high_school=profile.high_school,
        university=profile.university,
        corporate_team=profile.corporate_team,
        nationality=profile.nationality,
        is_foreign_player=profile.is_foreign_player,
    )
