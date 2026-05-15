import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Configura Vite para React y habilita el runtime JSX esperado por la app.
export default defineConfig({
  plugins: [react()],
});
