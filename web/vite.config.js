import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { mockApiPlugin } from "./scripts/vite-mock-api.js";

export default defineConfig({
  plugins: [react(), mockApiPlugin()],
  server: {
    host: "127.0.0.1",
    port: 18084,
    strictPort: true,
  },
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.js",
    exclude: ["tests-e2e/**", "node_modules/**", "dist/**"],
  },
  build: {
    outDir: "../agent/ui/frontend_dist",
    emptyOutDir: true,
  },
});
