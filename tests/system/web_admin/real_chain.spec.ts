import { readFileSync } from "node:fs";
import { join } from "node:path";

import { expect, test } from "@playwright/test";

// 真实后端 E2E：由 playwright.real.config.ts 的 webServer 托管
// scripts/acceptance.py（40 台模拟 PLC + Collector + Commander +
// Web Server + Vite dev server），覆盖完整业务链——设备列表、任务
// 启停、真实写入三态契约在浏览器中的呈现。
//
// 写操作 API token 由 acceptance 启动时生成，落盘 acceptance.json。
// Playwright 以配置目录（src/web_admin）为 cwd 运行测试进程。

const statePath = join(process.cwd(), "../../run/acceptance/acceptance.json");
const writeToken = (
  JSON.parse(readFileSync(statePath, "utf-8")) as { write_token: string }
).write_token;

const API_BASE = "http://127.0.0.1:8000";

test("devices page lists simulator devices and reports them connected", async ({
  page,
}) => {
  await page.goto("/");
  // 40 台模拟设备全部经真实协议栈连接。
  await expect(
    page.getByRole("cell", { name: "sim-mb-01", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("cell", { name: "sim-i104-01", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("cell", { name: "sim-ads-01", exact: true }),
  ).toBeVisible();
  // 无错误横幅；设备行显示已连接。
  await expect(page.locator(".el-alert--error")).toHaveCount(0);
  const row = page.getByRole("row").filter({
    has: page.getByRole("cell", { name: "sim-mb-01", exact: true }),
  });
  await expect(row.getByRole("cell").nth(2)).toContainText(/yes|true/i);
});

test("operator can start a collection task from the tasks page", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByText("Tasks", { exact: true }).first().click();
  await expect(
    page.getByRole("heading", { name: "Collector Tasks" }),
  ).toBeVisible();
  await page.getByPlaceholder(/API Key/i).fill(writeToken);
  const row = page
    .getByRole("row")
    .filter({
      has: page.getByRole("cell", { name: "sim-modbus-all", exact: true }),
    })
    .first();
  await expect(row).toBeVisible();
  // 任务可能已由其他用例启动；仅当 Start 可用时点击。
  if (await row.getByRole("button", { name: "Start" }).isEnabled()) {
    await row.getByRole("button", { name: "Start" }).click();
    await expect(row.getByText(/running/i).first()).toBeVisible({
      timeout: 15_000,
    });
  }
});

test("write command shows tri-state result with command_id", async ({
  page,
  request,
}) => {
  // 最新值表依赖采集运行；直接经 API 启动任务（UI 启动由上一个用例覆盖）。
  await request.post(
    `${API_BASE}/api/v1/collector/tasks/sim-modbus-all/start`,
    {
      headers: { "X-API-Key": writeToken },
    },
  );

  await page.goto("/");
  await page.getByRole("cell", { name: "sim-mb-01", exact: true }).click();
  await page.getByPlaceholder("Point ID").fill("power_setpoint");
  await page.getByPlaceholder("Value", { exact: true }).fill("777.7");
  await page.getByPlaceholder(/Control API Key/i).fill(writeToken);
  await page.getByRole("button", { name: "Write", exact: true }).click();
  // 成功三态展示：成功消息 + command_id 审计后缀。
  await expect(page.locator(".el-message").last()).toContainText(
    "Command succeeded",
    {
      timeout: 15_000,
    },
  );
  await expect(page.locator(".el-message").last()).toContainText("command_id");
  // 写入回读：最新值表显示 power_setpoint ≈ 777.7。
  const row = page.getByRole("row").filter({
    has: page.getByRole("cell", { name: "power_setpoint", exact: true }),
  });
  await expect(row.getByRole("cell").nth(1)).toContainText("777.7", {
    timeout: 15_000,
  });
});
