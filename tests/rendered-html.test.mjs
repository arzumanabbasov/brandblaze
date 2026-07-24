import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("the UI contains no fake generated-product placeholders", async () => {
  const page = await readFile(new URL("../app/page.tsx", import.meta.url), "utf8");
  assert.doesNotMatch(page, /ProductSilhouette|96% MATCH|AERA ONE|Genblaze online|B2 vault connected/);
  assert.match(page, /Backend offline/);
  assert.match(page, /variant\.url/);
  assert.match(page, /variant\.score/);
});

test("the product form starts without a misleading demo identity", async () => {
  const page = await readFile(new URL("../app/page.tsx", import.meta.url), "utf8");
  assert.match(page, /useState\(""\)/);
  assert.match(page, /Add the product name and a concise identity lock/);
  assert.match(page, /Math\.min\(.+12\)/s);
});
