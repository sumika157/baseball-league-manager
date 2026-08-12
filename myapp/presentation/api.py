"""試合編集画面（React）の保存 API。

画面の描画は views.game_edit が担い、保存だけがここに来る。検証は
presentation/forms.py のフォームを行単位で使い、業務ルール（打順の巡回・塁の再生・
勝敗の導出など）はドメイン層に任せる（検証・業務ルールの出典を増やさない）。

**受け取るのは打席の記録だけ。** 成績を手入力で受け取っていた古い API は、
入力画面をスコアブックに置き換えたときに消した。
"""

import json

from django.http import JsonResponse
from django.urls import reverse
from django.views.decorators.http import require_POST

from ..domain.exceptions import DomainError, GameNotFound
from ..infrastructure.queries import DjangoTeamPermissionQuery
from .forms import (
    FieldingErrorForm,
    LineupSlotForm,
    PlateAppearanceForm,
    RunnerAdvanceForm,
    ScorebookGameForm,
)
from .views import _first_error, build_recording_service, build_service

_MISSING = object()


def _as_row_list(body: dict, key: str) -> list | None:
    """body[key] がフォームの行データ（dict のリスト）として妥当なら返す。

    キー自体が無い場合は None を返す（不正なリクエストとして拒否する）。
    保存は「渡されなかった打席・打順は取り消す」仕様なので、キーの欠落を空リストと
    同じに扱うと、キーを1つ落としただけのリクエストで既存の記録が全消去されてしまう。
    """
    value = body.get(key, _MISSING)
    if value is _MISSING or not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        return None
    return value


def _collect_plate_appearances(rows: list) -> tuple[list, str | None]:
    """打席の行を検証してドメインの打席に組み立てる。

    進塁と失策は打席の中に入れ子で送られてくる。**行は位置ではなく打席ごとに
    束ねる**（並びが変わっても走者の動きが別の打席に付かない）。
    最初に見つけた誤りだけを返す（フォームと同じ扱い）。

    **組み立ての途中でもドメインが弾く**（進塁の理由と到達の食い違いなど）。
    ここで捕まえないと 500 になってしまうので、フォームの誤りと同じ扱いにする。
    """
    built = []
    for row in rows:
        form = PlateAppearanceForm(row)
        advance_rows = row.get("advances")
        error_rows = row.get("errors", [])
        if not isinstance(advance_rows, list) or not isinstance(error_rows, list):
            return [], "リクエストの形式が不正です。"

        advance_forms = [RunnerAdvanceForm(entry) for entry in advance_rows]
        error_forms = [FieldingErrorForm(entry) for entry in error_rows]
        for each in (form, *advance_forms, *error_forms):
            if not each.is_valid():
                return [], _first_error(each)

        try:
            built.append(
                form.to_plate_appearance(
                    [advance.to_advance() for advance in advance_forms],
                    [error.to_error() for error in error_forms],
                )
            )
        except DomainError as error:
            return [], str(error)
    return built, None


@require_POST
def game_scorebook(request, game_id):
    """スコアブック（打席の記録）を保存する。

    得点・イニングスコア・登板順・勝敗は受け取らない。すべて打席から導く
    （受け取ると「記録と食い違う得点」を保存できてしまう）。
    """
    if not request.user.is_authenticated:
        return JsonResponse({"ok": False, "error": "ログインが必要です。再度ログインしてください。"}, status=403)

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "error": "リクエストの形式が不正です。"}, status=400)
    if not isinstance(body, dict):
        return JsonResponse({"ok": False, "error": "リクエストの形式が不正です。"}, status=400)

    service = build_service()
    try:
        game = service.get_game_edit_data(game_id)["game"]
    except GameNotFound:
        return JsonResponse({"ok": False, "error": "試合が見つかりません。"}, status=404)

    if not DjangoTeamPermissionQuery().can_manage_any(request.user, (game.home_team_id, game.away_team_id)):
        return JsonResponse({"ok": False, "error": "このチームを編集する権限がありません。"}, status=403)

    lineup_data = _as_row_list(body, "lineup")
    plate_appearance_data = _as_row_list(body, "plate_appearances")
    if lineup_data is None or plate_appearance_data is None:
        return JsonResponse({"ok": False, "error": "リクエストの形式が不正です。"}, status=400)

    game_form = ScorebookGameForm(body)
    lineup_forms = [LineupSlotForm(row) for row in lineup_data]
    for form in (game_form, *lineup_forms):
        if not form.is_valid():
            return JsonResponse({"ok": False, "error": _first_error(form)}, status=400)

    plate_appearances, error = _collect_plate_appearances(plate_appearance_data)
    if error is not None:
        return JsonResponse({"ok": False, "error": error}, status=400)

    try:
        build_recording_service().record_scorebook(
            game_id,
            year=game_form.cleaned_data["year"],
            played_on=game_form.cleaned_data["played_on"],
            home_team_id=game_form.cleaned_data["home_team"],
            away_team_id=game_form.cleaned_data["away_team"],
            lineup=[form.to_slot() for form in lineup_forms],
            plate_appearances=plate_appearances,
        )
    except DomainError as error_raised:
        return JsonResponse({"ok": False, "error": str(error_raised)}, status=400)

    return JsonResponse({"ok": True, "redirect_url": reverse("game_detail", args=[game_id])})
