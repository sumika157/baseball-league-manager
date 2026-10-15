"""CSRF の検査に落ちたときの応答。

保存 API（`/api/`）は React から `fetch` で呼ばれ、応答を JSON として読む。Django 既定の HTML の 403 を返すと
画面には「HTTP 403」としか出ないので、他の API と同じ形（`{"ok": false, "error": "日本語"}`）で返す。
それ以外のパスは通常の画面なので、`403.html` を描く。
"""

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render

API_PATH_PREFIX = "/api/"

API_ERROR_MESSAGE = "セッションの有効期限が切れました。ページを再読み込みしてからもう一度お試しください。"


def csrf_failure(request: HttpRequest, reason: str = "") -> HttpResponse:
    """`settings.CSRF_FAILURE_VIEW`。`reason` は Django が渡す失敗の理由（利用者には見せない）。"""
    if request.path.startswith(API_PATH_PREFIX):
        return JsonResponse({"ok": False, "error": API_ERROR_MESSAGE}, status=403)
    return render(request, "403.html", status=403)
