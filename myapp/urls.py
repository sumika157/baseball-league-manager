from django.urls import path

from .presentation import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("teams/", views.team_list, name="team_list"),
    path("players/", views.player_search, name="player_search"),
    path("standings/", views.standings, name="standings"),
    path("standings/<int:year>/", views.standings, name="standings_by_year"),
    path("league/<int:league_id>/", views.league_detail, name="league_detail"),
    path(
        "league/<int:league_id>/<int:year>/",
        views.league_detail,
        name="league_detail_by_year",
    ),
    path(
        "league/<int:league_id>/titles/",
        views.league_titles,
        name="league_titles",
    ),
    path(
        "league/<int:league_id>/titles/<int:year>/",
        views.league_titles,
        name="league_titles_by_year",
    ),
    path(
        "league/<int:league_id>/stats/",
        views.league_stats,
        name="league_stats",
    ),
    path("games/", views.game_list, name="game_list"),
    path("games/new/", views.game_create, name="game_create"),
    path("games/<int:game_id>/", views.game_detail, name="game_detail"),
    path("games/<int:game_id>/edit/", views.game_edit, name="game_edit"),
    path("team/<int:team_id>/", views.player_list, name="player_list"),
    # 選手は Team 集約の内部エンティティなので、URL もチームの下に置く
    path(
        "team/<int:team_id>/player/<int:player_id>/",
        views.player_detail,
        name="player_detail",
    ),
    path(
        "team/<int:team_id>/player/<int:player_id>/edit/",
        views.player_edit,
        name="player_edit",
    ),
    path("accounts/signup/", views.SignUpView.as_view(), name="signup"),
]
