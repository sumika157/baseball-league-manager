// lib/api.ts
// CSRF 付き POST ヘルパー。保存 API はどのアイランドからも同じ形（
// 成功: {"ok": true, ...}／失敗: {"ok": false, "error": "日本語メッセージ"}）を返す前提。
// エラーメッセージの文言はここが唯一の出典（呼び出し側で個別に組み立てない）。

/** 保存 API の成功レスポンス。エントリごとに異なる付随データを型引数で足す。 */
export type ApiSuccess<T> = { ok: true } & T;

/** 保存 API の失敗レスポンス。 */
export interface ApiFailure {
  ok: false;
  error: string;
}

export type ApiResult<T> = ApiSuccess<T> | ApiFailure;

/**
 * CSRF トークン付きで JSON を POST する。
 *
 * fetch 自体が失敗した場合（オフライン等）と、応答が JSON として解釈できない場合
 * （サーバーエラーページが返るなど）はここで日本語メッセージに変換する。
 * それ以外（HTTP 400/403/404 でも JSON ボディが返る場合）は応答をそのまま返す。
 */
export async function postJson<T>(url: string, csrfToken: string, body: unknown): Promise<ApiResult<T>> {
  let response: Response;
  try {
    response = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": csrfToken,
      },
      body: JSON.stringify(body),
    });
  } catch {
    return { ok: false, error: "通信に失敗しました。接続を確認してもう一度お試しください。" };
  }

  let data: unknown;
  try {
    data = await response.json();
  } catch {
    return { ok: false, error: `保存に失敗しました（HTTP ${response.status}）。` };
  }

  if (isApiResult<T>(data)) {
    return data;
  }
  return { ok: false, error: `保存に失敗しました（HTTP ${response.status}）。` };
}

function isApiResult<T>(value: unknown): value is ApiResult<T> {
  if (typeof value !== "object" || value === null || !("ok" in value)) {
    return false;
  }
  const record = value as { ok: unknown };
  if (record.ok === false) {
    return "error" in value && typeof (value as { error: unknown }).error === "string";
  }
  return record.ok === true;
}
