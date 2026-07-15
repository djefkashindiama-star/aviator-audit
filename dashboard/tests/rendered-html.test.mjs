import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

async function render() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);

  return worker.fetch(
    new Request("http://localhost/", { headers: { accept: "text/html" } }),
    { ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) } },
    { waitUntil() {}, passThroughOnException() {} },
  );
}

test("rend le tableau de bord Aviator", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.match(html, /<title>Aviator Audit — Observatoire continu<\/title>/i);
  assert.match(html, /Collecte de 20 jours prête/);
  assert.match(html, /Refuser les illusions/);
  assert.match(html, /Manches collectées/);
});

test("utilise une API relative et un relais local", async () => {
  const [dashboard, vite] = await Promise.all([
    readFile(new URL("../app/Dashboard.tsx", import.meta.url), "utf8"),
    readFile(new URL("../vite.config.ts", import.meta.url), "utf8"),
  ]);
  assert.match(dashboard, /const API = "\/api\/dashboard"/);
  assert.match(dashboard, /deployment_mode === "render-free"/);
  assert.match(vite, /"\/api": "http:\/\/127\.0\.0\.1:8765"/);
});
