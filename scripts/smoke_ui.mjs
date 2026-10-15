#!/usr/bin/env node
// UI smoke test against a RUNNING frontend (used by scripts/integration.sh).
// Opens every dashboard tab in headless Chromium, fails on page/console errors or
// missing content, and records a short paper trade through the Trade log form.
// Needs the `playwright` package (resolved from the frontend, NODE_PATH, or
// PLAYWRIGHT_MODULE_DIR); exits 2 ("skipped") if it is not installed.
import { createRequire } from "node:module";
import path from "node:path";

const base = process.argv[2] ?? "http://127.0.0.1:3765";
const outDir = process.argv[3] ?? "/tmp";
const roots = [
  path.join(process.cwd(), "frontend", "node_modules"),
  path.join(process.cwd(), "node_modules"),
  process.env.PLAYWRIGHT_MODULE_DIR,
  ...(process.env.NODE_PATH ?? "").split(path.delimiter),
].filter(Boolean);
let chromium = null;
for (const r of roots) {
  try {
    ({ chromium } = createRequire(path.join(r, "noop.js"))("playwright"));
    break;
  } catch {
    /* try the next location */
  }
}
if (!chromium) {
  console.log("UI smoke: SKIPPED (playwright not installed; `npm i -D playwright` to enable)");
  process.exit(2);
}

const browser = await chromium.launch(
  process.env.PLAYWRIGHT_CHROMIUM_PATH ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_PATH } : {},
);
const page = await (await browser.newContext({ viewport: { width: 1440, height: 1000 } })).newPage();
const errors = [];
page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));
page.on("console", (m) => m.type() === "error" && errors.push(`console: ${m.text()}`));
const results = [];
const step = async (name, fn) => {
  const before = errors.length;
  try {
    await fn();
    if (errors.length > before) throw new Error(errors.slice(before).join(" | "));
    results.push([name, true, ""]);
  } catch (e) {
    const shot = path.join(outDir, `failed-${results.length + 1}.png`);
    await page.screenshot({ path: shot, fullPage: true }).catch(() => {});
    results.push([name, false, `${String(e.message ?? e).slice(0, 300)} (screenshot ${shot})`]);
  }
};
const tab = (name) => page.getByRole("tab", { name, exact: true });

await step("dashboard loads", async () => {
  await page.goto(base, { waitUntil: "networkidle" });
  await page.getByText("TradeScope").first().waitFor();
  await page.getByText(/KEY TECHNICAL LEVELS/i).first().waitFor({ timeout: 30000 });
});
await step("opportunity scanner", async () => {
  await tab("Opportunity scanner").click();
  await page.getByText(/bullish/i).first().waitFor({ timeout: 60000 });
  await page.getByText("Strategy evidence").first().waitFor({ timeout: 30000 });
  await page.screenshot({ path: path.join(outDir, "tradescope-scanner.png"), fullPage: true });
});
await step("market scanner tab", async () => {
  await tab("Market scanner").click();
  await page.getByText(/Eligible stocks/).first().waitFor({ timeout: 30000 });
  await page.screenshot({ path: path.join(outDir, "tradescope-market.png"), fullPage: true });
});
await step("performance tab", async () => {
  await tab("Performance").click();
  await page.getByText(/Realized net P&L/).first().waitFor({ timeout: 30000 });
  await page.screenshot({ path: path.join(outDir, "tradescope-performance.png"), fullPage: true });
});
await step("research tab", async () => {
  await tab("Research").click();
  await page.getByText(/Net expectancy per strategy/).first().waitFor({ timeout: 30000 });
  await page.getByText(/Negative evidence/).first().waitFor();
  await page.screenshot({ path: path.join(outDir, "tradescope-research.png"), fullPage: true });
});
await step("options tab", async () => {
  await tab("Options").click();
  await page.getByText(/OPTIONS DATA UNAVAILABLE|Stock-level risk/).first().waitFor({ timeout: 30000 });
});
await step("trade log: record a paper short with a partial exit", async () => {
  await tab("Trade log").click();
  await page.getByRole("tab", { name: "Paper trades" }).click();
  await page.getByLabel("Ticker").fill("QQQ");
  await page.getByLabel("Direction").selectOption("SHORT");
  await page.getByLabel("Quantity").fill("10");
  await page.getByLabel("Price").fill("400");
  await page.getByLabel("Planned stop").fill("404");
  await page.getByRole("button", { name: /Record paper trade/ }).click();
  const row = page.locator("tbody tr").filter({ hasText: "QQQ" }).first();
  await row.waitFor();
  await row.click();
  await page.getByLabel("Fill quantity").fill("4");
  await page.getByLabel("Fill price").fill("396");
  await page.getByRole("button", { name: "Add fill" }).click();
  await page.getByText("+$16.00").first().waitFor({ timeout: 10000 });
});
await step("backtesting tab + simulated-trade inspector chart", async () => {
  await tab("Backtesting").click();
  await page.getByText(/Evidence:/).first().waitFor({ timeout: 30000 });  // run from the API smoke
  await page.locator("table").filter({ hasText: "Exit reason" }).locator("tbody tr").first().click();
  const insp = page.locator("section", { hasText: "Simulated trade" }).first();
  await insp.waitFor({ timeout: 15000 });
  await insp.locator("canvas").first().waitFor({ timeout: 15000 });
});
await page.screenshot({ path: path.join(outDir, "tradescope-smoke.png"), fullPage: true });
await browser.close();

for (const [n, ok, d] of results) console.log(`  ${ok ? "PASS" : "FAIL"}  ${n}${d ? `  ${d}` : ""}`);
const failed = results.filter((r) => !r[1]).length;
console.log(`UI smoke: ${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
