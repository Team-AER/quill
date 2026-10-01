import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { quillMock } from "./mock/plugin.ts";

// QUILL_API=http://127.0.0.1:8020 npm run dev  -> proxy /api to the real backend
// npm run dev                                   -> in-process mock (mock/plugin.ts)
const api = process.env.QUILL_API;

export default defineConfig({
  plugins: [react(), ...(api || process.env.VITEST ? [] : [quillMock()])],
  server: {
    port: Number(process.env.PORT ?? 5180),
    proxy: api
      ? {
          "/api": {
            target: api,
            changeOrigin: false,
            // SSE: don't buffer
            configure: (proxy) => {
              proxy.on("proxyRes", (res) => {
                if (String(res.headers["content-type"] ?? "").includes("text/event-stream")) {
                  res.headers["cache-control"] = "no-cache";
                }
              });
            },
          },
        }
      : undefined,
  },
  build: { outDir: "dist", sourcemap: false, chunkSizeWarningLimit: 900 },
  test: { environment: "node", include: ["src/**/*.test.ts"] },
});
