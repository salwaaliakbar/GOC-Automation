import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// During development (npm run dev) API calls are proxied to the Python backend.
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": "http://127.0.0.1:8000" } },
});
