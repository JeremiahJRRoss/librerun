import { defineConfig } from "vitest/config";

export default defineConfig({
  // JSX in the component tests (K4b). tsconfig.json keeps "jsx": "preserve"
  // because Next compiles the app itself, and Vite reads tsconfig per file,
  // so a .tsx test would reach the parser with its JSX untransformed. Vite's
  // own transform takes React's automatic runtime here instead: no plugin,
  // and tsconfig.json unchanged.
  oxc: {
    jsx: { runtime: "automatic" },
  },
  test: {
    environment: "jsdom",
    include: ["src/**/__tests__/**/*.test.{ts,tsx}"],
  },
});
