// ビルド基盤の動作確認用の仮実装。試合編集画面の React 化で置き換える。
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

const root = document.getElementById("game-edit-root");
if (root) {
  createRoot(root).render(
    <StrictMode>
      <p>読み込み中…</p>
    </StrictMode>,
  );
}
