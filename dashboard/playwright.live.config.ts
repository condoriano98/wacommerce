import { defineConfig } from "@playwright/test";

// Live end-to-end (remediation E1): the real dashboard (next start) talking to a real OMS (uvicorn) on a seeded,
// throw-away PostgreSQL database. No mocks between the browser and the database; providers stay mocked in the OMS.
const OMS_PORT = 8010;
const WEB_PORT = 3200;
const py = process.env.OMS_PY ?? "uv run python"; // CI: "python"
const pgAdmin = process.env.OMS_TEST_PG_ADMIN ?? "postgresql+psycopg://postgres@localhost:5432";
const db = "oms_e2e";
const role = (r: string) => `postgresql+psycopg://${r}:${r}@localhost:5432/${db}`;

export default defineConfig({
  testDir: "./tests-live",
  timeout: 60_000,
  workers: 1,
  use: { baseURL: `http://localhost:${WEB_PORT}`, trace: "off" },
  webServer: [
    {
      command: `cd ../oms && ${py} -m tests.e2e_seed ../dashboard/tests-live/.seed.json && ` +
        `${py} -m uvicorn app.main:app --host 127.0.0.1 --port ${OMS_PORT} --no-access-log`,
      url: `http://127.0.0.1:${OMS_PORT}/healthz`,
      reuseExistingServer: false,
      timeout: 180_000,
      env: {
        OMS_ENV: "dev", OMS_TEST_DB: db, OMS_TEST_PG_ADMIN: pgAdmin, OMS_DB_ROLE_MODE: "v31",
        OMS_DATABASE_URL: role("oms_app"), OMS_WORKER_DATABASE_URL: role("oms_worker"),
        OMS_WEBHOOK_DATABASE_URL: role("oms_webhook"), OMS_MIGRATION_DATABASE_URL: `${pgAdmin}/${db}`,
        OMS_JWT_SECRET: "e2e-only-not-a-secret", OMS_JWT_TTL_MINUTES: "30",
      },
    },
    {
      command: `npx next start -p ${WEB_PORT}`,
      url: `http://localhost:${WEB_PORT}/login`,
      reuseExistingServer: false,
      timeout: 120_000,
      env: { OMS_API_URL: `http://127.0.0.1:${OMS_PORT}` },
    },
  ],
});
