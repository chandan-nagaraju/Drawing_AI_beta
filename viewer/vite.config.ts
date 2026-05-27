import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        // Do not rewrite PDF bodies; range/stream breaks PDF.js through dev proxy.
        configure: (proxy) => {
          proxy.on("proxyRes", (proxyRes, req) => {
            if (req.url?.includes("/file") && proxyRes.statusCode === 204) {
              proxyRes.statusCode = 502;
            }
          });
        },
      },
    },
  },
});
