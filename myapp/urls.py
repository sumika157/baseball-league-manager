from django.urls import path
from . import views

urlpatterns = [
    path('', views.team_list, name='team_list'),
    path('team/<int:team_id>/', views.player_list, name='player_list'),
    path('player/<int:player_id>/edit/', views.player_edit, name='player_edit'),
    path('accounts/signup/', views.SignUpView.as_view(), name='signup'),
]