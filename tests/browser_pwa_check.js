const { chromium } = require("playwright");

const widths = [320, 375, 768, 1440];
const baseURL = process.env.PWA_TEST_URL || "http://127.0.0.1:8765";
const chromePath =
  process.env.CHROME_PATH || "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: chromePath });
  const inviteCode = process.env.PWA_INVITE_CODE;
  if (!inviteCode) throw new Error("PWA_INVITE_CODE is required");
  const authContext = await browser.newContext({ viewport: { width: 375, height: 900 } });
  const authPage = await authContext.newPage();
  await authPage.goto(baseURL, { waitUntil: "networkidle" });
  await authPage.waitForSelector("#splash.hidden");
  if (!(await authPage.locator("#auth-gate").isVisible())) throw new Error("invite gate missing");
  if (await authPage.locator(".stock-card").count()) throw new Error("protected data leaked before login");
  await authPage.locator("#invite-code").fill(inviteCode);
  await authPage.locator("#invite-form button[type=submit]").click();
  await authPage.waitForSelector("#app-shell:not([hidden])");
  const storageState = await authContext.storageState();
  await authContext.setOffline(true);
  await authPage.reload({ waitUntil: "domcontentloaded" });
  const offlineVisible = await authPage.locator("#offline-banner").isVisible();
  const offlineProtected = await authPage.locator("#auth-gate").isVisible();
  await authContext.setOffline(false);
  await authPage.evaluate(() => window.dispatchEvent(new Event("online")));
  await authPage.waitForSelector("#app-shell:not([hidden])");
  const recovered = !(await authPage.locator("#offline-banner").isVisible());
  await authContext.close();

  const results = [];
  for (const width of widths) {
    const context = await browser.newContext({ viewport: { width, height: 900 }, storageState });
    const page = await context.newPage();
    await page.goto(baseURL, { waitUntil: "networkidle" });
    await page.waitForSelector("#splash.hidden");
    const metrics = await page.evaluate(() => ({
      title: document.title,
      overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
      bottomNavVisible: getComputedStyle(document.querySelector(".bottom-nav")).display !== "none",
      cardCount: document.querySelectorAll(".stock-card").length,
      tableVisible: getComputedStyle(document.querySelector(".desktop-table-wrap")).display !== "none",
    }));
    await page.screenshot({ path: `output/pwa-${width}.png`, fullPage: true });
    results.push({ width, ...metrics });
    await context.close();
  }

  await browser.close();

  console.log(JSON.stringify({ results, offlineVisible, offlineProtected, recovered }, null, 2));
  if (results.some(item => item.overflow) || !offlineVisible || !offlineProtected || !recovered) process.exit(1);
})();
