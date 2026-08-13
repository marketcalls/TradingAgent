import { defineConfig } from "vite"
import react from "@vitejs/plugin-react"
import tailwindcss from "@tailwindcss/vite"

// Tailwind 4 ships a first-party Vite plugin, so there is no postcss.config.js and no
// autoprefixer: the plugin handles both.
//
// Every fetch in the app uses a bare relative /api path, so the dev server proxies to
// the backend rather than the browser talking to it cross-origin. The port is
// BACKEND_PORT from the repo .env; changing one without the other breaks dev only.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8088", changeOrigin: true }
    }
  }
})
