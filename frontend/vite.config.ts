import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// ビルド成果物は Django の静的ファイルとして配信する。
// dev server（HMR）は使わず `vite build --watch` で常時出力する方式
// （dev/prod の分岐や manifest 解析を持ち込まないため）。
// ファイル名をハッシュ無しの固定名にすることで、テンプレート側は
// {% static 'myapp/dist/<エントリ名>.js' %} で参照するだけで済む。
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../myapp/static/myapp/dist",
    emptyOutDir: true,
    sourcemap: true,
    rollupOptions: {
      input: {
        game_edit: "src/game_edit/main.tsx",
      },
      output: {
        entryFileNames: "[name].js",
        chunkFileNames: "[name].js",
        assetFileNames: "[name][extname]",
      },
    },
  },
});
