import { defineConfig } from "vite"
import react from "@vitejs/plugin-react"

// Every fetch in the app uses a bare relative /api path, so the dev server proxies
// to the backend rather than the browser talking to it cross-origin. The port is
// BACKEND_PORT from the repo .env; changing one without the other breaks dev only.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8088", changeOrigin: true }
    }
  }
})
