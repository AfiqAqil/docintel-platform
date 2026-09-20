import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Local dev only. In every deployed environment nginx owns /api, same origin,
// no CORS (see ARCHITECTURE section 8). This proxy exists so `npm run dev`
// can talk to a backend running on localhost without CORS either.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://localhost:8000",
    },
  },
});
