import { expect, test } from "@playwright/test";
test("the runtime pages render without a backend", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator(".el-menu")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Devices" })).toBeVisible();
  await page.getByText("Tasks", { exact: true }).first().click();
  await expect(
    page.getByRole("heading", { name: "Collector Tasks" }),
  ).toBeVisible();
  await page.getByText("Sinks", { exact: true }).first().click();
  await expect(
    page.getByRole("heading", { name: "Collector Sinks" }),
  ).toBeVisible();
});
