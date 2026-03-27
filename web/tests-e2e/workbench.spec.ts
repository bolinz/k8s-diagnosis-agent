import { expect, test } from "@playwright/test";

test.describe("diagnosis workbench", () => {
  test("renders summary, list and detail sections", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "K8s Diagnosis Workbench" })).toBeVisible();
    await expect(page.getByText("Framework baseline: list triage, detail analysis, and timeline evidence.")).toBeVisible();
    await expect(page.getByText("Workload Context")).toBeVisible();
    await expect(page.getByText("Key Signals")).toBeVisible();
    await expect(page.getByText("Related Objects Graph")).toBeVisible();
    await expect(page.getByRole("button", { name: /Pod\/checkout-abc/i })).toBeVisible();
  });

  test("filters list by symptom and recovers", async ({ page }) => {
    await page.goto("/");
    const symptomInput = page.getByRole("textbox", { name: "Symptom" });
    await symptomInput.fill("oomkilled");
    await expect(page.getByText("No reports match current filters.")).toBeVisible();
    await symptomInput.fill("failedmount");
    await expect(page.getByRole("button", { name: /Pod\/checkout-abc/i })).toBeVisible();
  });

  test("supports timezone switch and relation panel visibility", async ({ page }) => {
    await page.goto("/");

    const timezone = page.getByRole("combobox", { name: "timezone" });
    await timezone.selectOption("UTC");
    await expect(timezone).toHaveValue("UTC");

    await expect(page.getByText("Hover to inspect, click to lock and show object names.")).toBeVisible();
    await expect(page.locator("svg.relation-svg")).toBeVisible();
    await expect(page.getByRole("heading", { name: "Related Objects Graph" })).toBeVisible();
    await expect(page).toHaveURL(/tz=UTC/);
  });

  test("restores UI state from query string", async ({ page }) => {
    await page.goto("/?tz=UTC&sym=failedmount&report=preview-1");
    await expect(page.getByRole("combobox", { name: "timezone" })).toHaveValue("UTC");
    await expect(page.getByRole("textbox", { name: "Symptom" })).toHaveValue("failedmount");
    await expect(page.getByRole("heading", { name: "Pod/checkout-abc" })).toBeVisible();
  });

  test("quick symptom filter switches list across scenarios", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByRole("button", { name: /Deployment\/billing-api/i })).toBeVisible();
    await page.getByRole("button", { name: "ImagePullBackOff (1)" }).click();
    await expect(page.getByRole("button", { name: /Pod\/checkout-web-5dfb7d6f8b-k9v6p/i })).toBeVisible();
    await page.getByRole("button", { name: "FailedMount (1)" }).click();
    await expect(page.getByRole("button", { name: /Pod\/checkout-abc/i })).toBeVisible();
  });

  test("supports keyboard shortcut for symptom search", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("heading", { name: "K8s Diagnosis Workbench" }).click();
    await page.keyboard.press("/");
    await expect(page.getByRole("textbox", { name: "Symptom" })).toBeFocused();
  });

  test("shows attribution empty state when role filter excludes all objects", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("button", { name: /Deployment\/billing-api/i }).click();
    await expect(page.getByRole("heading", { name: "Deployment/billing-api" })).toBeVisible();
    await page.getByRole("button", { name: "attribution" }).click();
    await page.getByRole("combobox", { name: "Role" }).selectOption("owner");
    await expect(page.getByRole("button", { name: /Deployment\/billing-api/i })).toBeVisible();
    await page.getByRole("combobox", { name: "Role" }).selectOption("upstream-suspect");
    await expect(page.getByText("No related objects in current role filter.")).toBeVisible();
  });

  test("keeps observability bar non-sticky to avoid content overlap", async ({ page }) => {
    await page.goto("/");
    const position = await page.locator(".bottom-ops-bar").evaluate((node) => getComputedStyle(node).position);
    expect(position).toBe("static");
  });

  test("keeps relation graph readable under high-related-object density", async ({ page }) => {
    await page.goto("/?report=preview-5");
    await expect(page.getByRole("heading", { name: "Deployment/graph-stress-demo" })).toBeVisible();

    const graphCheck = await page.locator("svg.relation-svg").evaluate((svg) => {
      const viewBox = (svg.getAttribute("viewBox") || "").split(/\s+/).map(Number);
      const viewBoxWidth = viewBox[2] || 0;
      const viewBoxHeight = viewBox[3] || 0;
      const nodeCircles = Array.from(svg.querySelectorAll("circle.relation-node")).map((el) => ({
        cx: Number(el.getAttribute("cx") || "0"),
        cy: Number(el.getAttribute("cy") || "0"),
        r: Number(el.getAttribute("r") || "0"),
      }));
      const tokenTexts = Array.from(svg.querySelectorAll("text.relation-kind-token"));
      const outsideTexts = Array.from(svg.querySelectorAll("text.relation-kind-outside, text.relation-name")).map((el) => {
        const bb = el.getBBox();
        const out = bb.x < 0 || bb.y < 0 || bb.x + bb.width > viewBoxWidth || bb.y + bb.height > viewBoxHeight;
        return out ? { text: el.textContent || "", x: bb.x, y: bb.y, w: bb.width, h: bb.height } : null;
      }).filter(Boolean);
      const edgePaths = Array.from(svg.querySelectorAll("path.relation-edge"));

      let nodeOverlapCount = 0;
      for (let i = 0; i < nodeCircles.length; i += 1) {
        for (let j = i + 1; j < nodeCircles.length; j += 1) {
          const a = nodeCircles[i];
          const b = nodeCircles[j];
          const d = Math.hypot(a.cx - b.cx, a.cy - b.cy);
          if (d < a.r + b.r + 2) nodeOverlapCount += 1;
        }
      }

      let tokenClippedCount = 0;
      tokenTexts.forEach((textEl) => {
        const bb = textEl.getBBox();
        const centerX = bb.x + bb.width / 2;
        const centerY = bb.y + bb.height / 2;
        const nearest = nodeCircles.reduce(
          (best, node) => {
            const d = Math.hypot(centerX - node.cx, centerY - node.cy);
            if (d < best.distance) return { distance: d, node };
            return best;
          },
          { distance: Number.POSITIVE_INFINITY, node: null as null | { cx: number; cy: number; r: number } },
        );
        if (!nearest.node) return;
        const boxInsideBounds =
          bb.x >= nearest.node.cx - nearest.node.r + 2 &&
          bb.x + bb.width <= nearest.node.cx + nearest.node.r - 2 &&
          bb.y >= nearest.node.cy - nearest.node.r + 2 &&
          bb.y + bb.height <= nearest.node.cy + nearest.node.r - 2;
        if (!boxInsideBounds) tokenClippedCount += 1;
      });

      const pointInCircle = (x: number, y: number, node: { cx: number; cy: number; r: number }) => {
        const keepOut = node.r * 0.72;
        return (x - node.cx) ** 2 + (y - node.cy) ** 2 <= keepOut ** 2;
      };
      const samplePath = (path: SVGPathElement, n = 120) => {
        const length = path.getTotalLength();
        const points: Array<{ x: number; y: number }> = [];
        for (let i = 8; i <= n - 8; i += 1) {
          const p = path.getPointAtLength((length * i) / n);
          points.push({ x: p.x, y: p.y });
        }
        return points;
      };
      let pathThroughNodeCount = 0;
      edgePaths.forEach((pathEl) => {
        const points = samplePath(pathEl as SVGPathElement);
        nodeCircles.forEach((node) => {
          if (points.some((p) => pointInCircle(p.x, p.y, node))) pathThroughNodeCount += 1;
        });
      });

      return {
        nodeCount: nodeCircles.length,
        tokenCount: tokenTexts.length,
        edgeCount: edgePaths.length,
        nodeOverlapCount,
        tokenClippedCount,
        pathThroughNodeCount,
        outsideTextCount: outsideTexts.length,
      };
    });
    const paneHeights = await page.evaluate(() => {
      const list = document.querySelector(".list")?.getBoundingClientRect();
      const detail = document.querySelector(".detail")?.getBoundingClientRect();
      return {
        listHeight: list ? Math.round(list.height) : null,
        detailHeight: detail ? Math.round(detail.height) : null,
        diff: list && detail ? Math.abs(Math.round(list.height - detail.height)) : null,
      };
    });

    expect(graphCheck.nodeCount).toBeGreaterThanOrEqual(8);
    expect(graphCheck.tokenCount).toBe(graphCheck.nodeCount);
    expect(graphCheck.nodeOverlapCount).toBe(0);
    expect(graphCheck.tokenClippedCount).toBe(0);
    expect(graphCheck.pathThroughNodeCount).toBeLessThanOrEqual(2);
    expect(graphCheck.outsideTextCount).toBe(0);
    expect(paneHeights.diff).not.toBeNull();
    expect(paneHeights.diff).toBeLessThanOrEqual(1);
  });

  test("shows timeline density strip and supports prev/next focus navigation", async ({ page }) => {
    await page.goto("/?report=preview-4");
    await expect(page.getByRole("heading", { name: "Deployment/timeline-stretch-demo" })).toBeVisible();
    await expect(page.getByLabel("timeline-density")).toBeVisible();
    const barCount = await page.locator(".timeline-density-bar").count();
    expect(barCount).toBeGreaterThan(2);

    await page.getByRole("button", { name: /Show Event Navigator/i }).click();
    await expect(page.locator(".timeline-group-toggle").first()).toBeVisible();
    await expect(page.getByRole("combobox", { name: "Group Sort" })).toBeVisible();
    await page.getByRole("combobox", { name: "Group Sort" }).selectOption("time");
    await expect(page.getByRole("combobox", { name: "Group Sort" })).toHaveValue("time");
    const rowsBeforeCollapse = await page.locator(".timeline-row").count();
    await page.locator(".timeline-group-toggle").first().click();
    const rowsAfterCollapse = await page.locator(".timeline-row").count();
    expect(rowsAfterCollapse).toBeLessThan(rowsBeforeCollapse);
    await page.locator(".timeline-group-toggle").first().click();
    const showMoreCount = await page.getByRole("button", { name: /Show more/i }).count();
    if (showMoreCount > 0) {
      const showMore = page.getByRole("button", { name: /Show more/i }).first();
      const rowsBeforeExpand = await page.locator(".timeline-row").count();
      await showMore.click();
      const rowsAfterExpand = await page.locator(".timeline-row").count();
      expect(rowsAfterExpand).toBeGreaterThan(rowsBeforeExpand);
    }
    await page.locator(".timeline-row").first().click();
    await expect(page.locator(".timeline-group-active")).toBeVisible();
    const prevBtn = page.getByRole("button", { name: "Prev" });
    const nextBtn = page.getByRole("button", { name: "Next" });
    await expect(prevBtn).toBeVisible();
    await expect(nextBtn).toBeVisible();

    const focusedBefore = await page.locator(".timeline-focus-bar span").first().innerText();
    const canGoNext = await nextBtn.isEnabled();
    const canGoPrev = await prevBtn.isEnabled();
    if (canGoNext) {
      await page.keyboard.press(".");
      await expect(page.locator(".timeline-focus-bar span").first()).not.toHaveText(focusedBefore);
    } else if (canGoPrev) {
      await page.keyboard.press(",");
      await expect(page.locator(".timeline-focus-bar span").first()).not.toHaveText(focusedBefore);
    } else {
      await expect(page.locator(".timeline-focus-bar span").first()).toHaveText(focusedBefore);
    }
  });

  test("toggles shortcut help and remembers timeline group sort across reload", async ({ page }) => {
    await page.goto("/?report=preview-4");
    await page.getByRole("button", { name: "Shortcuts" }).click();
    await expect(page.getByLabel("shortcut-help")).toBeVisible();
    await page.keyboard.press("?");
    await expect(page.getByLabel("shortcut-help")).toBeHidden();

    await page.getByRole("button", { name: /Show Event Navigator/i }).click();
    const groupSort = page.getByRole("combobox", { name: "Group Sort" });
    await groupSort.selectOption("time");
    await expect(groupSort).toHaveValue("time");

    await page.reload();
    await expect(page.getByRole("combobox", { name: "Group Sort" })).toHaveValue("time");
  });
});
