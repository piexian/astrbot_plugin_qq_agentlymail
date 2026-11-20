import { fileURLToPath } from "node:url";
import { defineConfig, lazyPlugins } from "vite-plus";
import vue from "@vitejs/plugin-vue";

// 构建产物直接输出到插件 views/mail，由 AstrBot 作为视图托管。
export default defineConfig({
  fmt: {},
  lint: {
    jsPlugins: [{ name: "vite-plus", specifier: "vite-plus/oxlint-plugin" }],
    rules: { "vite-plus/prefer-vite-plus-imports": "error" },
    options: { typeAware: true, typeCheck: true },
  },
  plugins: lazyPlugins(() => [vue()]),
  base: "./",
  build: {
    outDir: fileURLToPath(new URL("../views/mail", import.meta.url)),
    emptyOutDir: true,
    assetsDir: "assets",
    sourcemap: false,
    target: "es2020",
  },
});
