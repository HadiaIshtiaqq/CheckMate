import { expect, test, type Page } from "@playwright/test";

// Captures the README screenshots. Skipped unless SHOTS=1:
//   SHOTS=1 npx playwright test screenshots
// Uses the demo store (no API credits). Images go to ../docs/screenshots.
const OUT = "../docs/screenshots";
const REQUEST =
  "Find wireless headphones under $110. They must have at least a one-year warranty, free returns, no subscription, and come from a reliable seller.";

test.skip(!process.env.SHOTS, "set SHOTS=1 to capture README screenshots");
test.use({ viewport: { width: 1440, height: 900 }, colorScheme: "light", deviceScaleFactor: 1 });

async function signUp(page: Page) {
  await page.goto("/login");
  await page.screenshot({ path: `${OUT}/login.png` });
  await page.getByRole("tab", { name: "Create account" }).click();
  await page.getByLabel("Email").fill(`shopper-${Date.now()}@example.com`);
  await page.getByLabel("Password").fill("correct horse battery staple");
  await page.getByRole("button", { name: /Create account/ }).last().click();
  await expect(page.getByLabel("Shopping request")).toBeVisible();
}

test("capture README screenshots", async ({ page }) => {
  test.setTimeout(180_000);
  await signUp(page);
  await page.getByLabel("Shopping request").fill(REQUEST);
  await page.screenshot({ path: `${OUT}/dashboard.png` });

  await page.getByRole("button", { name: /Start shopping/ }).click();
  await expect(page.getByText("Review and approve your purchase contract")).toBeVisible();
  await page.waitForTimeout(800);
  await page.screenshot({ path: `${OUT}/plan.png` });
  await page.screenshot({ path: `${OUT}/plan-full.png`, fullPage: true });

  await page.getByRole("button", { name: /Approve contract v1/ }).first().click();
  await expect(page.getByText(/Approved by you/)).toBeVisible();
  await page.getByRole("radio", { name: /Sneaky subscription/ }).click();
  await page.getByRole("button", { name: /Verify final checkout/ }).click();
  await expect(page.getByText("Payment blocked to protect you")).toBeVisible();
  await page.waitForTimeout(800);
  await page.screenshot({ path: `${OUT}/blocked.png` });

  // A clean run through to the passport.
  await page.goto("/");
  await page.getByLabel("Shopping request").fill(REQUEST);
  await page.getByRole("button", { name: /Start shopping/ }).click();
  await expect(page.getByText("Review and approve your purchase contract")).toBeVisible();
  await page.getByRole("button", { name: /Approve contract v1/ }).first().click();
  await page.getByRole("button", { name: /Verify final checkout/ }).click();
  await expect(page.getByText(/PASS — payment eligible/)).toBeVisible();
  await page.getByRole("button", { name: /Pay with/ }).first().click();
  await expect(page.getByText("SIMULATED PAYPAL")).toBeVisible();
  await page.getByRole("button", { name: /Approve & continue/ }).click();
  await expect(page.getByText("Purchase complete and verified")).toBeVisible({ timeout: 30_000 });
  await page.getByRole("link", { name: /View Purchase Truth Passport/ }).click();
  await expect(page.getByText("Signature and audit chain verified")).toBeVisible();
  await page.waitForTimeout(800);
  await page.screenshot({ path: `${OUT}/passport.png` });
});
