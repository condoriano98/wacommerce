import { readFileSync } from "node:fs";
import { join } from "node:path";
import { expect, test, type Page } from "@playwright/test";

// Real dashboard → real OMS → PostgreSQL (seeded by oms/tests/e2e_seed.py). Remediation E1.
type Seed = { users: Record<string, { email: string; password: string }>; order_number: string };
const seed = (): Seed => JSON.parse(readFileSync(join(__dirname, ".seed.json"), "utf8"));
// Next.js renders an empty role="alert" route announcer; application errors are the non-empty alerts.
const errors = (page: Page) => page.getByRole("alert").filter({ hasText: /\S/ });
const b64 = (o: object) => Buffer.from(JSON.stringify(o)).toString("base64url");

async function signIn(page: Page, who: string, password?: string) {
  const u = seed().users[who];
  await page.goto("/login");
  await page.getByLabel("Email").fill(u.email);
  await page.getByLabel("Password").fill(password ?? u.password);
  await page.getByRole("button", { name: "Sign in" }).click();
}

test("wrong password is rejected and no session is created", async ({ page }) => {
  await signIn(page, "finance", "not-the-password");
  await expect(page.getByRole("status")).toContainText("invalid credentials");
  await expect(page).toHaveURL(/\/login/);
  expect((await page.context().cookies()).map((c) => c.name)).not.toContain("oms_session");
});

test("finance signs in and reads real ledger KPIs; the token never reaches page scripts", async ({ page }) => {
  await signIn(page, "finance");
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByTestId("kpi-net-sales")).toBeVisible();
  await expect(page.getByTestId("kpi-net-sales")).not.toHaveText("–");
  await expect(errors(page)).toHaveCount(0);
  const cookie = (await page.context().cookies()).find((c) => c.name === "oms_session")!;
  expect(cookie.httpOnly).toBe(true);
  expect(cookie.sameSite).toBe("Strict");
  expect(await page.evaluate(() => document.cookie)).not.toContain("oms_session");
  expect(await page.evaluate(() => Object.keys(window.localStorage))).toEqual([]);
});

test("protected pages redirect to sign-in and come back after signing in", async ({ page }) => {
  await page.goto("/orders");
  await expect(page).toHaveURL(/\/login\?next=%2Forders/);
  const u = seed().users.finance;
  await page.getByLabel("Email").fill(u.email);
  await page.getByLabel("Password").fill(u.password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL(/\/orders$/);
  await expect(page.getByRole("cell", { name: seed().order_number })).toBeVisible();
});

test("an expired session is cleared and the user signs in again", async ({ page, context }) => {
  const expired = `${b64({ alg: "HS256", typ: "JWT" })}.${b64({ sub: "x", org: "y", exp: 1 })}.sig`;
  await context.addCookies([{ name: "oms_session", value: expired, url: "http://localhost:3200" }]);
  await page.goto("/fpa");
  await expect(page).toHaveURL(/\/login\?expired=1/);
  await expect(page.getByRole("status")).toContainText("session has expired");
});

test("a forged token is rejected by the OMS and the session is dropped", async ({ page, context }) => {
  const forged = `${b64({ alg: "HS256", typ: "JWT" })}.${b64({ sub: "x", org: "y",
    exp: Math.floor(Date.now() / 1000) + 600 })}.forged`;
  await context.addCookies([{ name: "oms_session", value: forged, url: "http://localhost:3200" }]);
  await page.goto("/orders");
  await expect(page).toHaveURL(/\/login\?expired=1/);
  expect((await context.cookies()).find((c) => c.name === "oms_session")?.value ?? "").toBe("");
});

test("sign out ends the session", async ({ page }) => {
  await signIn(page, "finance");
  await expect(page).toHaveURL(/\/$/);
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page).toHaveURL(/\/login/);
  await page.goto("/orders");
  await expect(page).toHaveURL(/\/login\?next=/);
});

test("portfolio managers see only their own lines and cannot open consolidated finance", async ({ page }) => {
  await signIn(page, "pm_a");
  await expect(page).toHaveURL(/\/$/);
  await page.goto("/orders");
  const row = page.getByRole("row").filter({ hasText: seed().order_number });
  await expect(row).toContainText("Rp100.000"); // A1 only; B1's Rp80.000 line is not visible
  await expect(row).not.toContainText("Rp180.000");
  await page.goto("/fpa");
  await expect(errors(page)).toContainText("missing permission fpa.read");
});

test("API failures are reported: CSRF header, blocked routes and server errors", async ({ page }) => {
  await signIn(page, "finance");
  await expect(page).toHaveURL(/\/$/);
  const noHeader = await page.request.post("/api/oms/ops/invariants/check");
  expect(noHeader.status()).toBe(403);
  const webhook = await page.request.post("/api/oms/webhooks/doku", { headers: { "x-oms-dashboard": "1" } });
  expect(webhook.status()).toBe(404);
  const missing = await page.request.get("/api/oms/no-such-endpoint", { headers: { "x-oms-dashboard": "1" } });
  expect(missing.status()).toBe(404);
  const ok = await page.request.get("/api/oms/orders", { headers: { "x-oms-dashboard": "1" } });
  expect(ok.status()).toBe(200);
});
