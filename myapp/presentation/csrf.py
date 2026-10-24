"""CSRF の検査に落ちたときの応答。

保存 API（`/w/<key>/api/...`）は React から `fetch` で呼ばれ、応答を JSON として読む。Django 既定の HTML の 403 を
返すと画面には「HTTP 403」としか出ないので、他の API と同じ形（`{"ok": false, "error": "日本語"}`）で返す。
それ以外の画面は `403.html` を描く。API かどうかは、パスの形ではなく URL 名（`api_` で始まる）で決める
（URL の先頭にワークスペースの key が入っても判定が変わらない）。
"""

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.urls import Resolver404, resolve

API_URL_NAME_PREFIX = "api_"

API_ERROR_MESSAGE = "セッションの有効期限が切れました。ページを再読み込みしてからもう一度お試しください。"


def _is_api(request: HttpRequest) -> bool:
    """保存 API への要求か。CSRF の検査はビューの手前（ミドルウェア）なので、解決の結果が無ければ自分で引く。"""
    match = getattr(request, "resolver_match", None)
    if match is None:
        try:
            match = resolve(request.path_info)
        except Resolver404:
            return False
    name = match.url_name
    return name is not None and name.startswith(API_URL_NAME_PREFIX)


def csrf_failure(request: HttpRequest, reason: str = "") -> HttpResponse:
    """`settings.CSRF_FAILURE_VIEW`。`reason` は Django が渡す失敗の理由（利用者には見せない）。"""
    if _is_api(request):
        return JsonResponse({"ok": False, "error": API_ERROR_MESSAGE}, status=403)
    return render(request, "403.html", status=403)
