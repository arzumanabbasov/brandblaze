import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("the UI contains no fake generated-product placeholders", async () => {
  const studio = await readFile(new URL("../app/studio/page.tsx", import.meta.url), "utf8");
  assert.doesNotMatch(studio, /ProductSilhouette|96% MATCH|AERA ONE|Genblaze online|B2 vault connected/);
  assert.match(studio, /Backend offline/);
  assert.match(studio, /variant\.url/);
  assert.match(studio, /variant\.score/);
});

test("the product form starts without a misleading demo identity", async () => {
  const studio = await readFile(new URL("../app/studio/page.tsx", import.meta.url), "utf8");
  assert.match(studio, /useState\(""\)/);
  assert.match(studio, /Add the product name and a concise identity lock/);
  assert.match(studio, /Math\.min\(.+12\)/s);
});

test("the public landing page sends users into a separate studio", async () => {
  const landing = await readFile(new URL("../app/page.tsx", import.meta.url), "utf8");
  assert.match(landing, /href="\/studio"/);
  assert.match(landing, /One product\./);
  assert.doesNotMatch(landing, /type="file"|Generate Campaign/);
});
