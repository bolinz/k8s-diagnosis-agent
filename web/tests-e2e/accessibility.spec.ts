import { expect, test } from "@playwright/test";

test.describe("accessibility smoke", () => {
  test("form controls expose accessible names", async ({ page }) => {
    await page.goto("/");
    const unlabeled = await page.evaluate(() => {
      const elements = Array.from(document.querySelectorAll("input, select, textarea, button"));
      function hasAccessibleName(el) {
        if (el.getAttribute("aria-label")) return true;
        if (el.getAttribute("aria-labelledby")) return true;
        if (el.id && document.querySelector(`label[for="${el.id}"]`)) return true;
        const wrappedLabel = el.closest("label");
        if (wrappedLabel && wrappedLabel.textContent && wrappedLabel.textContent.trim().length > 0) return true;
        if (el.textContent && el.textContent.trim().length > 0) return true;
        return false;
      }
      return elements
        .filter((el) => !el.hidden && !el.hasAttribute("disabled"))
        .filter((el) => !hasAccessibleName(el))
        .map((el) => `${el.tagName.toLowerCase()}.${el.className || "no-class"}`);
    });
    expect(unlabeled).toEqual([]);
  });
});
