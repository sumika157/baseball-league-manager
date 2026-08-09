from django.urls import path

from .presentation import views

urlpatterns = [
    path('', views.team_list, name='team_list'),
    path('team/<int:team_id>/', views.player_list, name='player_list'),
    # 選手は Team 集約の内部エンティティなので、URL もチームの下に置く
    path(
        'team/<int:team_id>/player/<int:player_id>/edit/',
        views.player_edit,
        name='player_edit',
    ),
    path('accounts/signup/', views.SignUpView.as_view(), name='signup'),
]
