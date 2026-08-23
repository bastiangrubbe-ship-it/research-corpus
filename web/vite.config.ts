import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Port pinned to 5173 deliberately: the backend's CORS allowlist
// (src/corpus/web/app.py) names this exact origin. Changing it here needs a
// matching change there.
export default defineConfig({
  plugins: [react()],
  server: { port: 5173 },
});
