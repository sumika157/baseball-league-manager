"""試合編集画面（React）の保存 API。

画面の描画は views.game_edit が担い、保存だけがここに来る。検証は既存の
presentation/forms.py（GameForm・InningScoreForm・BattingEntryForm・
PitchingEntryForm）を行単位でそのまま再利用し、業務ルール（被本塁打が
被安打を超えない、勝敗・セーブ・ホールドの導出など）はドメイン層に任せる
（検証・業務ルールの出典を増やさない）。
"""

import json

from django.http import JsonResponse
from django.urls import reverse
from django.views.decorators.http import require_POST

from ..domain.exceptions import DomainError, GameNotFound
from ..domain.value_objects import BattingLine, InningsPitched, LineScore, PitchingLine
from ..infrastructure.queries import DjangoTeamPermissionQuery
from .forms import BattingEntryForm, GameForm, InningScoreForm, PitchingEntryForm
from .views import _first_error, _service


def _collect_line_score(inning_forms) -> LineScore:
    """イニングスコアを組み立てる。

    表・裏それぞれ、入力がある最後の回までを行われた回とし、途中の空欄は
    0とみなす（1回に得点が無ければ空欄のままにする人もいるため）。

    行の並びではなく **inning の番号**で値を配置する。行の欠落・重複・
    並び順の入れ替わりがあっても、回を取り違えて記録しない（例: 1・2・12回
    しか送られなかった場合に12回の得点が3回目として記録される、といった
    事故を避ける）。裏は表より長くならない（ホームが表より先に攻めることは
    無いため、表の記録が無い回の裏だけが入力されても切り落とす）。
    """
    by_inning = {form.cleaned_data["inning"]: form.cleaned_data for form in inning_forms}

    def _last_recorded(key: str) -> int:
        return max((inning for inning, data in by_inning.items() if data.get(key) is not None), default=0)

    away_through = _last_recorded("away")
    home_through = min(_last_recorded("home"), away_through)
    away = tuple((by_inning.get(inning, {}).get("away") or 0) for inning in range(1, away_through + 1))
    home = tuple((by_inning.get(inning, {}).get("home") or 0) for inning in range(1, home_through + 1))
    return LineScore(away=away, home=home)


def _collect_batting(forms) -> tuple[dict, dict]:
    """未入力の行は含めない。含めると出場していない選手の記録が残る。"""
    lines: dict = {}
    lineup: dict = {}
    for form in forms:
        if form.is_blank():
            continue
        player_id = form.cleaned_data["player_id"]
        lines[player_id] = BattingLine(**form.counts())
        lineup[player_id] = form.lineup()
    return lines, lineup


def _collect_pitching(forms) -> tuple[dict, dict]:
    """未入力の行は含めない。含めると出場していない選手の記録が残る。"""
    lines: dict = {}
    staff: dict = {}
    for form in forms:
        if form.is_blank():
            continue
        player_id = form.cleaned_data["player_id"]
        lines[player_id] = PitchingLine(innings=InningsPitched.from_notation(form.innings()), **form.counts())
        staff[player_id] = form.entered()
    return lines, staff


_MISSING = object()


def _as_row_list(body: dict, key: str) -> list | None:
    """body[key] がフォームの行データ（dict のリスト）として妥当なら返す。

    キー自体が無い場合は None を返す（不正なリクエストとして拒否する）。
    update_game は「渡されなかった選手の記録・イニングスコアは取り消す」
    仕様なので、キーの欠落を空リストと同じに扱うと、キーを1つ落としただけの
    リクエストで既存の成績が全消去されてしまう。
    """
    value = body.get(key, _MISSING)
    if value is _MISSING or not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        return None
    return value


@require_POST
def game_update(request, game_id):
    """試合の基本情報と成績を保存する。成功したら試合詳細への遷移先を返す。"""
    if not request.user.is_authenticated:
        return JsonResponse({"ok": False, "error": "ログインが必要です。再度ログインしてください。"}, status=403)

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "error": "リクエストの形式が不正です。"}, status=400)
    if not isinstance(body, dict):
        return JsonResponse({"ok": False, "error": "リクエストの形式が不正です。"}, status=400)

    service = _service()
    try:
        game = service.get_game_edit_data(game_id)["game"]
    except GameNotFound:
        return JsonResponse({"ok": False, "error": "試合が見つかりません。"}, status=404)

    if not DjangoTeamPermissionQuery().can_manage_any(request.user, (game.home_team_id, game.away_team_id)):
        return JsonResponse({"ok": False, "error": "このチームを編集する権限がありません。"}, status=403)

    innings_data = _as_row_list(body, "innings")
    batting_data = _as_row_list(body, "batting")
    pitching_data = _as_row_list(body, "pitching")
    if innings_data is None or batting_data is None or pitching_data is None:
        return JsonResponse({"ok": False, "error": "リクエストの形式が不正です。"}, status=400)

    game_form = GameForm(body)
    inning_forms = [InningScoreForm(row) for row in innings_data]
    batting_forms = [BattingEntryForm(row) for row in batting_data]
    pitching_forms = [PitchingEntryForm(row) for row in pitching_data]

    for form in (game_form, *inning_forms, *batting_forms, *pitching_forms):
        if not form.is_valid():
            return JsonResponse({"ok": False, "error": _first_error(form)}, status=400)

    try:
        batting, lineup = _collect_batting(batting_forms)
        pitching, staff = _collect_pitching(pitching_forms)
        service.update_game(
            game_id,
            year=game_form.cleaned_data["year"],
            played_on=game_form.cleaned_data["played_on"],
            home_team_id=game_form.cleaned_data["home_team"],
            away_team_id=game_form.cleaned_data["away_team"],
            home_score=game_form.cleaned_data["home_score"],
            away_score=game_form.cleaned_data["away_score"],
            batting=batting,
            pitching=pitching,
            lineup=lineup,
            staff=staff,
            line_score=_collect_line_score(inning_forms),
        )
    except DomainError as error:
        return JsonResponse({"ok": False, "error": str(error)}, status=400)

    return JsonResponse({"ok": True, "redirect_url": reverse("game_detail", args=[game_id])})
