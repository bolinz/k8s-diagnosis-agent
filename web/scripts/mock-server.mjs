import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const workspaceRoot = path.resolve(__dirname, "..", "..");
const distDir = path.resolve(workspaceRoot, "agent", "ui", "frontend_dist");
const dataPath = process.env.MOCK_DATA
  ? path.resolve(process.cwd(), process.env.MOCK_DATA)
  : path.resolve(__dirname, "..", "mocks", "reports.json");
const port = Number(process.env.PORT || 18084);

const mimeByExt = {
  ".html": "text/html; charset=utf-8",
  ".js": "application/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".ico": "image/x-icon",
  ".txt": "text/plain; charset=utf-8",
};

async function loadItems() {
  const payload = JSON.parse(await readFile(dataPath, "utf-8"));
  if (!payload || !Array.isArray(payload.items)) return [];
  return payload.items.filter((x) => x && typeof x === "object");
}

function sendJson(res, statusCode, payload) {
  const body = Buffer.from(JSON.stringify(payload), "utf-8");
  res.writeHead(statusCode, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": String(body.length),
  });
  res.end(body);
}

async function sendFile(res, filePath) {
  try {
    const data = await readFile(filePath);
    const ext = path.extname(filePath).toLowerCase();
    const contentType = mimeByExt[ext] || "application/octet-stream";
    res.writeHead(200, {
      "Content-Type": contentType,
      "Content-Length": String(data.length),
    });
    res.end(data);
  } catch {
    sendJson(res, 404, { error: "not found" });
  }
}

async function main() {
  const items = await loadItems();
  const byName = new Map(items.map((item) => [String(item.name || ""), item]));
  const indexPath = path.resolve(distDir, "index.html");

  const server = createServer(async (req, res) => {
    const url = new URL(req.url || "/", `http://${req.headers.host || "127.0.0.1"}`);
    const pathname = url.pathname;

    if (pathname === "/healthz") {
      sendJson(res, 200, { ok: true });
      return;
    }
    if (pathname === "/api/reports") {
      sendJson(res, 200, { items });
      return;
    }
    if (pathname.startsWith("/api/reports/")) {
      const name = decodeURIComponent(pathname.slice("/api/reports/".length));
      const item = byName.get(name);
      if (!item) {
        sendJson(res, 404, { error: "report not found" });
        return;
      }
      sendJson(res, 200, item);
      return;
    }

    if (pathname === "/") {
      await sendFile(res, indexPath);
      return;
    }

    const relative = pathname.replace(/^\/+/, "");
    const filePath = path.resolve(distDir, relative);
    if (!filePath.startsWith(path.resolve(distDir))) {
      sendJson(res, 404, { error: "not found" });
      return;
    }
    await sendFile(res, filePath);
  });

  server.listen(port, "127.0.0.1", () => {
    // eslint-disable-next-line no-console
    console.log(`[mock-server] listening on http://127.0.0.1:${port}`);
    // eslint-disable-next-line no-console
    console.log(`[mock-server] data: ${dataPath}`);
  });
}

main().catch((error) => {
  // eslint-disable-next-line no-console
  console.error(`[mock-server] failed to start: ${error?.message || error}`);
  process.exit(1);
});
