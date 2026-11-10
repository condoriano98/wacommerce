import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  timeout: 30_000,
  use: { baseURL: "http://localhost:3100", trace: "off" },
  webServer: { command: "npx next start -p 3100", url: "http://localhost:3100", reuseExistingServer: false, timeout: 60_000 },
});
