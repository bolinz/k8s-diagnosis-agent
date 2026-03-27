import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

function resolveMockDataPath() {
  if (process.env.MOCK_DATA) {
    return path.resolve(process.cwd(), process.env.MOCK_DATA);
  }
  return path.resolve(__dirname, "..", "mocks", "reports.json");
}

async function readItems() {
  const dataPath = resolveMockDataPath();
  const payload = JSON.parse(await readFile(dataPath, "utf-8"));
  if (!payload || !Array.isArray(payload.items)) return [];
  return payload.items.filter((x) => x && typeof x === "object");
}

function sendJson(res, code, payload) {
  const body = JSON.stringify(payload);
  res.statusCode = code;
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.end(body);
}

export function mockApiPlugin() {
  return {
    name: "k8s-diagnosis-mock-api",
    configureServer(server) {
      server.middlewares.use(async (req, res, next) => {
        if (!req.url) return next();
        const url = new URL(req.url, "http://127.0.0.1");
        if (url.pathname === "/healthz") {
          sendJson(res, 200, { ok: true });
          return;
        }
        if (url.pathname === "/api/reports") {
          try {
            const items = await readItems();
            sendJson(res, 200, { items });
          } catch (error) {
            sendJson(res, 500, { error: String(error?.message || error) });
          }
          return;
        }
        if (url.pathname.startsWith("/api/reports/")) {
          try {
            const name = decodeURIComponent(url.pathname.slice("/api/reports/".length));
            const items = await readItems();
            const item = items.find((x) => String(x.name || "") === name);
            if (!item) {
              sendJson(res, 404, { error: "report not found" });
              return;
            }
            sendJson(res, 200, item);
          } catch (error) {
            sendJson(res, 500, { error: String(error?.message || error) });
          }
          return;
        }
        next();
      });
    },
  };
}
