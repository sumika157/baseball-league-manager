// json_script（#game-edit-data）を読んで #game-edit-root にマウントする。
// GET 用 API は無く、初期データはテンプレート埋め込みの payload のみ。
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import type { GameEditPayload } from "./types";

const dataElement = document.getElementById("game-edit-data");
const root = document.getElementById("game-edit-root");

if (dataElement && root) {
  const payload = JSON.parse(dataElement.textContent ?? "null") as GameEditPayload | null;

  if (payload) {
    createRoot(root).render(
      <StrictMode>
        <App payload={payload} />
      </StrictMode>,
    );
  }
}
