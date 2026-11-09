import { defineConfig } from "@playwright/test";

// End-to-end tests drive the real UI in Microsoft Edge against a real backend.
// They use the fictional demo store and simulated PayPal, so they spend no API credits.
const API_PORT = 8100;
const WEB_PORT = 3100;
const isWin = process.platform === "win32";
const python = isWin ? "..\\backend\\.venv\\Scripts\\python" : "../backend/.venv/bin/python";

export default defineConfig({
  testDir: "./e2e",
  timeout: 90_000,
  expect: { timeout: 20_000 },
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: `http://localhost:${WEB_PORT}`,
    // Edge locally on Windows; the bundled Chromium in CI (or set PW_CHANNEL).
    channel: process.env.PW_CHANNEL || (process.env.CI ? undefined : "msedge"),
    headless: true,
    trace: "retain-on-failure",
  },
  webServer: [
    {
      command: `${python} -m uvicorn app.main:app --port ${API_PORT}`,
      cwd: "../backend",
      url: `http://localhost:${API_PORT}/health`,
      reuseExistingServer: false,
      timeout: 60_000,
      env: {
        DATABASE_URL: "sqlite:///./e2e.db",
        DEMO_CATALOG_ENABLED: "true",
        PAYPAL_MODE: "mock",
        PAYPAL_CLIENT_ID: "",
        PAYPAL_CLIENT_SECRET: "",
        SERPAPI_KEY: "",
        GEMINI_API_KEYS: "",
        OPENAI_API_KEY: "",
        CHECKMATE_DEMO_USERS: "",
        JOBS_ENABLED: "false",
        RATE_LIMIT_PER_MINUTE: "100000",
        FRONTEND_URL: `http://localhost:${WEB_PORT}`,
        CORS_ORIGINS: `http://localhost:${WEB_PORT}`,
        BACKEND_PUBLIC_URL: `http://localhost:${API_PORT}`,
      },
    },
    {
      command: `npx next start -p ${WEB_PORT}`,
      url: `http://localhost:${WEB_PORT}/login`,
      reuseExistingServer: false,
      timeout: 60_000,
      env: { BACKEND_URL: `http://localhost:${API_PORT}` },
    },
  ],
});
