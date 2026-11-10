import { expect, test, type Page } from "@playwright/test";

// The OMS API is mocked at the network layer: the dashboard is a pure client of it (no direct DB/provider access).
// The browser only talks to its own origin (/api/oms proxy, remediation E1); pages require a session cookie.
const API = "/api/oms";
const b64 = (o: object) => Buffer.from(JSON.stringify(o)).toString("base64url");
const fakeSession = `${b64({ alg: "HS256" })}.${b64({ exp: Math.floor(Date.now() / 1000) + 3600 })}.sig`;
const fixtures: Record<string, unknown> = {
  "/fpa/waterfall": { period: ["2026-11-01", "2026-11-30"], gross_sales: "205000.00", returns_allowances: "0",
    net_sales: "205000.00", cm1_costs: "-134000.00", cm1: "71000.00", marketing: "-100000.00", cm2: "-29000.00",
    cm3_costs: "0", cm3: "-29000.00", opex: "-50000.00", operating_profit: "-79000.00",
    unallocated: { cm2: "-10000.00", opex: "-50000.00" } },
  "/fpa/cash-forecast": { opening_cash: "1000000", minimum_closing_cash: "850000", excluded_unverified_transfers: "115000",
    weeks: [] },
  "/exceptions": [{ id: "1", type: "liquidity_breach", severity: "critical", status: "open", owner_role: "finance_manager",
    exposure: "40000000", sla_due_at: "2026-11-11T00:00:00Z", subject: "simulation_run:1" }],
  "/orders": [{ order_id: "o1", order_number: "WA-1001", channel: "whatsapp", status: "in_fulfillment", lines: 1,
    units: 1, visible_line_total: "100000", units_delivered: 0, placed_at: null }],
  "/integrations/capabilities": [{ account: "DOKU main", provider: "doku", capability: "*",
    implementation_status: "mock_tested", access_state: "mocked", operationally_ready: false }],
  "/fpa/simulations/stress": { S1_cpa_plus30: { run_id: "r1", breaches: [{ type: "liquidity_breach" }],
    consolidated: { cm2: "1000000", operating_profit: "-500000", min_cash: "9000000", min_cash_week: 7 } } },
};

async function mockApi(page: Page, status = 200) {
  await page.context().addCookies([{ name: "oms_session", value: fakeSession, url: "http://localhost:3100" }]);
  await page.route(`**${API}/**`, async (route) => {
    const path = new URL(route.request().url()).pathname.replace(API, "");
    if (status !== 200) return route.fulfill({ status, json: { detail: "invalid token" } });
    return route.fulfill({ status: 200, json: fixtures[path] ?? [] });
  });
}

test("overview shows ledger KPIs and exposure", async ({ page }) => {
  await mockApi(page);
  await page.goto("/");
  await expect(page.getByTestId("kpi-net-sales")).toContainText("205.000");
  await expect(page.getByTestId("kpi-cm3")).toContainText("-29.000");
  await expect(page.getByTestId("kpi-exposure")).toContainText("40.000.000");
  await expect(page.getByText("counted only after a bank match")).toBeVisible();
});

test("waterfall renders every level and unallocated lines separately", async ({ page }) => {
  await mockApi(page);
  await page.goto("/fpa");
  await expect(page.getByTestId("wf-operating_profit")).toContainText("-79.000");
  await expect(page.getByText(/Unallocated \(never spread\)/)).toBeVisible();
});

test("orders, exceptions and integrations pages list API rows", async ({ page }) => {
  await mockApi(page);
  await page.goto("/orders");
  await expect(page.getByRole("cell", { name: "WA-1001" })).toBeVisible();
  await page.goto("/exceptions");
  await expect(page.getByRole("cell", { name: "liquidity_breach" })).toBeVisible();
  await page.goto("/integrations");
  await expect(page.getByRole("cell", { name: "mocked" })).toBeVisible();
  await expect(page.getByRole("cell", { name: "no", exact: true })).toBeVisible();
});

test("simulator runs stress scenarios and shows alerts", async ({ page }) => {
  await mockApi(page);
  await page.goto("/simulator");
  await page.getByRole("button", { name: /Run S1/ }).click();
  await expect(page.getByRole("cell", { name: "S1_cpa_plus30" })).toBeVisible();
});

test("API authorization errors are shown, not hidden", async ({ page }) => {
  await mockApi(page, 403);
  await page.goto("/fpa");
  await expect(page.getByRole("alert").filter({ hasText: "invalid token" })).toBeVisible();
});

test("an expired or rejected session sends the user back to sign-in", async ({ page }) => {
  await mockApi(page, 401);
  await page.goto("/fpa");
  await expect(page).toHaveURL(/\/login\?expired=1/);
  await expect(page.getByRole("status")).toContainText("session has expired");
});

test("pages require a session; the browser never holds the OMS token", async ({ page }) => {
  await page.goto("/orders");
  await expect(page).toHaveURL(/\/login\?next=%2Forders/);
  expect(await page.evaluate(() => Object.keys(window.localStorage))).toEqual([]);
  expect(await page.evaluate(() => document.cookie)).not.toContain("oms_session");
});
