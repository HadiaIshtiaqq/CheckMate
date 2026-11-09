import { expect, test, type Page } from "@playwright/test";

const REQUEST =
  "Find wireless headphones under $110. They must have at least a one-year warranty, free returns, no subscription, and come from a reliable seller.";

async function signUp(page: Page) {
  const email = `e2e-${Date.now()}-${Math.floor(Math.random() * 1e6)}@example.com`;
  await page.goto("/login");
  await page.getByRole("tab", { name: "Create account" }).click();
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill("correct horse battery staple");
  await page.getByRole("button", { name: /Create account/ }).last().click();
  await expect(page.getByText(email)).toBeVisible();
  return email;
}

async function startWorkflow(page: Page) {
  await page.getByLabel("Shopping request").fill(REQUEST);
  await page.getByRole("button", { name: /Start shopping/ }).click();
  await expect(page.getByText("Review and approve your purchase contract")).toBeVisible();
}

test("unauthenticated visitors are sent to sign in", async ({ page }) => {
  await page.goto("/");
  await expect(page).toHaveURL(/\/login/);
});

test("safe purchase: approve, verify, pay with PayPal, signed passport", async ({ page }) => {
  await signUp(page);
  await startWorkflow(page);
  await page.getByRole("button", { name: /Approve contract v1/ }).first().click();
  await expect(page.getByText(/Approved by you/)).toBeVisible();

  await page.getByRole("button", { name: /Verify final checkout/ }).click();
  await expect(page.getByText(/PASS — payment eligible/)).toBeVisible();

  await page.getByRole("button", { name: /Pay with/ }).first().click();
  await expect(page.getByText("SIMULATED PAYPAL")).toBeVisible();
  await page.getByRole("button", { name: /Approve & continue/ }).click();

  await expect(page.getByText("Purchase complete and verified")).toBeVisible({ timeout: 30_000 });
  await page.getByRole("link", { name: /View Purchase Truth Passport/ }).click();
  await expect(page.getByText("Signature and audit chain verified")).toBeVisible();
  await expect(page.getByText("Reconciled — every record agrees")).toBeVisible();
});

test("manipulated checkout is blocked", async ({ page }) => {
  await signUp(page);
  await startWorkflow(page);
  await page.getByRole("button", { name: /Approve contract v1/ }).first().click();
  await page.getByRole("radio", { name: /Sneaky subscription/ }).click();
  await page.getByRole("button", { name: /Verify final checkout/ }).click();
  await expect(page.getByText(/FAIL — payment blocked/)).toBeVisible();
  await expect(page.getByText("Payment blocked to protect you")).toBeVisible();
  await expect(page.getByRole("button", { name: /Pay with/ })).toHaveCount(0);
});

test("accounts are isolated", async ({ page, browser }) => {
  await signUp(page);
  await startWorkflow(page);
  const url = page.url();
  const other = await browser.newPage();
  await signUp(other);
  await other.goto(url);
  await expect(other.getByText(/doesn't exist or belongs to another user/)).toBeVisible();
});

test("captured PayPal payment can be refunded", async ({ page }) => {
  await signUp(page);
  await startWorkflow(page);
  await page.getByRole("button", { name: /Approve contract v1/ }).first().click();
  await page.getByRole("button", { name: /Verify final checkout/ }).click();
  await page.getByRole("button", { name: /Pay with/ }).first().click();
  await page.getByRole("button", { name: /Approve & continue/ }).click();
  await expect(page.getByText("Purchase complete and verified")).toBeVisible({ timeout: 30_000 });
  page.once("dialog", (d) => d.accept());
  await page.getByRole("button", { name: /Refund with PayPal/ }).click();
  await expect(page.getByText(/^Refunded /)).toBeVisible();
  await expect(page.getByRole("button", { name: /Refund with PayPal/ })).toHaveCount(0);
});
