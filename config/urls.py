"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/

ワークスペースの中の画面は `/w/<key>/` の下（W2 T4。#78）。key の形はパスコンバータ `wskey`
（`myapp/presentation/converters.py`。ドメインの `KEY_LENGTH`・`KEY_ALPHABET` から作る）が受け、形の合わない
key は DB を読まずに 404。`/` はワークスペースの外のトップ（`home`）。
"""

from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path, register_converter

from myapp.presentation.converters import WorkspaceKeyConverter
from myapp.urls import workspace_urlpatterns

register_converter(WorkspaceKeyConverter, "wskey")

urlpatterns = [
    path("admin/", admin.site.urls),  # 管理者画面（今見ているもの）
    path("w/<wskey:key>/", include(workspace_urlpatterns)),  # ワークスペースの中の画面
    path("", include("myapp.urls")),  # ワークスペースの外（`/`・新規登録）
    # django.contrib.auth.urls はまるごと読み込まない。パスワード再設定の画面まで公開されてしまうため
    # （メールを送らない方針で、送信を試みて 500 になる）。URL 名は auth.urls と同じにしてある
    path("accounts/login/", auth_views.LoginView.as_view(), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("accounts/password_change/", auth_views.PasswordChangeView.as_view(), name="password_change"),
    path(
        "accounts/password_change/done/",
        auth_views.PasswordChangeDoneView.as_view(),
        name="password_change_done",
    ),
]
