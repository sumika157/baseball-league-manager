"""URL の表。ワークスペースの中の画面（`workspace_urlpatterns`）と、外の画面（`/`・新規登録）に分ける。

ワークスペースの中の画面は `config/urls.py` が `/w/<key>/` の下に置く（W2 T4。#78）。登録は必ず `_w()`（ペナントの
世界の画面は `_pennant()`）を通し、`views.workspace_view` で包む: URL の key からワークスペースと権限を解決し、
公開範囲とログインを確かめてからビューを呼ぶ（包み忘れを形で防ぐ。`test_workspace_isolation.py` の
`EveryWorkspaceUrlTest` が全パターンを検査する）。
中の画面へのリンクは、テンプレートでは `{% workspace_url %}`・`{% scoped_url %}`、views では `_url(request, …)`、
テストでは `ws_url(…)` で組む（素の `{% url %}`・`reverse` で組まない。`test_templates.py` がテンプレートの素の
`{% url %}` を検査する）。古い URL（`/teams/` など）は 404（リダイレクトしない）。
"""

from django.urls import path

from .presentation import api, views


def _w(route: str, view, name: str, *, login: bool = False, json: bool = False):
    """ワークスペースの中の画面を1つ登録する。必ず `workspace_view` で包む。

    `login=True` は閲覧にもログインが要る画面（試合の登録・編集・選手の編集）、`json=True` は JSON を返す API
    （ログインへのリダイレクトや 404 の画面の代わりに、同じ形の JSON のエラーを返す）。
    """
    return path(route, views.workspace_view(view, login=login, json=json), name=name)


def _pennant(route: str, view, name: str):
    """ペナントの世界の範囲の画面。実データの同名の画面と同じビューを、世界の id つきで開く。

    URL 名は `pennant_<実データの名前>`。共有テンプレートは `{% scoped_url %}` がこの名前を引く。
    """
    return _w(f"pennant/<int:world_id>/{route}", view, f"pennant_{name}")


# ワークスペースの中の画面（実データとペナント）。`config/urls.py` が `/w/<key>/` の下に置く
workspace_urlpatterns = [
    _w("", views.dashboard, "dashboard"),
    _w("teams/", views.team_list, "team_list"),
    _w("players/", views.player_search, "player_search"),
    _w("standings/", views.standings, "standings"),
    _w("standings/<int:year>/", views.standings, "standings_by_year"),
    _w("league/<int:league_id>/", views.league_detail, "league_detail"),
    _w("league/<int:league_id>/<int:year>/", views.league_detail, "league_detail_by_year"),
    _w("league/<int:league_id>/titles/", views.league_titles, "league_titles"),
    _w("league/<int:league_id>/titles/<int:year>/", views.league_titles, "league_titles_by_year"),
    _w("league/<int:league_id>/stats/", views.league_stats, "league_stats"),
    _w("games/", views.game_list, "game_list"),
    _w("games/new/", views.game_create, "game_create", login=True),
    _w("games/<int:game_id>/", views.game_detail, "game_detail"),
    _w("games/<int:game_id>/edit/", views.game_edit, "game_edit", login=True),
    _w("api/games/<int:game_id>/scorebook/", api.game_scorebook, "api_game_scorebook", json=True),
    _w("analysis/", views.analysis_index, "analysis_index"),
    _w("team/<int:team_id>/", views.player_list, "player_list"),
    _w("team/<int:team_id>/analysis/", views.team_analysis, "team_analysis"),
    # 選手は Team 集約の内部エンティティなので、URL もチームの下に置く
    _w("team/<int:team_id>/player/<int:player_id>/", views.player_detail, "player_detail"),
    _w("team/<int:team_id>/player/<int:player_id>/edit/", views.player_edit, "player_edit", login=True),
    # --- ペナントの世界（参照はワークスペースの公開範囲に従う。作成は持ち主・編集者、進める・削除は世界の作成者。
    # 選手・試合の編集の URL は作らない） ---
    _w("pennant/", views.pennant_index, "pennant_index"),
    _w("pennant/<int:world_id>/", views.pennant_world, "pennant_world"),
    _pennant("advance/", views.pennant_advance, "advance"),
    _pennant("season/close/", views.pennant_close_season, "close_season"),
    _pennant("offseason/<int:year>/", views.pennant_offseason, "offseason"),
    _pennant("history/", views.pennant_history, "history"),
    _pennant("team/<int:team_id>/history/", views.pennant_team_history, "team_history"),
    _pennant("delete/", views.pennant_delete, "delete"),
    _pennant("club/", views.pennant_club, "club"),
    _pennant("standings/", views.standings, "standings"),
    _pennant("standings/<int:year>/", views.standings, "standings_by_year"),
    _pennant("league/<int:league_id>/", views.league_detail, "league_detail"),
    _pennant("league/<int:league_id>/<int:year>/", views.league_detail, "league_detail_by_year"),
    _pennant("league/<int:league_id>/titles/", views.league_titles, "league_titles"),
    _pennant("league/<int:league_id>/titles/<int:year>/", views.league_titles, "league_titles_by_year"),
    _pennant("league/<int:league_id>/stats/", views.league_stats, "league_stats"),
    _pennant("teams/", views.team_list, "team_list"),
    _pennant("games/", views.game_list, "game_list"),
    _pennant("games/<int:game_id>/", views.game_detail, "game_detail"),
    _pennant("team/<int:team_id>/", views.player_list, "player_list"),
    _pennant("analysis/", views.analysis_index, "analysis_index"),
    _pennant("team/<int:team_id>/analysis/", views.team_analysis, "team_analysis"),
    _pennant("team/<int:team_id>/player/<int:player_id>/", views.player_detail, "player_detail"),
]

# ワークスペースの外（`config/urls.py` が `""` の下に置く）
urlpatterns = [
    path("", views.home, name="home"),
    path("accounts/signup/", views.SignUpView.as_view(), name="signup"),
]
