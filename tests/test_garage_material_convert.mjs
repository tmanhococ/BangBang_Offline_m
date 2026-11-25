import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { MATERIAL_CONVERSIONS, REVERSE_CONVERSIONS } from '../www/html5/m8_garage.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

test('MATERIAL_CONVERSIONS maps base materials to higher tier', () => {
  assert.equal(MATERIAL_CONVERSIONS[3000001].recipeId, 58);
  assert.equal(MATERIAL_CONVERSIONS[3000001].targetName, 'Thép');
  assert.equal(MATERIAL_CONVERSIONS[3000003].recipeId, 59);
  assert.equal(MATERIAL_CONVERSIONS[3000003].targetName, 'Carbon');
  assert.equal(MATERIAL_CONVERSIONS[3000051].recipeId, 62);
  assert.equal(MATERIAL_CONVERSIONS[3000051].targetName, 'Hồng ngọc');
  assert.equal(MATERIAL_CONVERSIONS[3000052].recipeId, 63);
  assert.equal(MATERIAL_CONVERSIONS[3000052].targetName, 'Lục ngọc');
  assert.equal(MATERIAL_CONVERSIONS[3000053].recipeId, 64);
  assert.equal(MATERIAL_CONVERSIONS[3000053].targetName, 'Tử bảo');
  assert.equal(MATERIAL_CONVERSIONS[3000054].recipeId, 65);
  assert.equal(MATERIAL_CONVERSIONS[3000054].targetName, 'Lam bảo');
});

test('REVERSE_CONVERSIONS maps higher tier to recipe that creates it', () => {
  // Thép (3000003) is created via recipe 58 from Mảnh Sắt
  assert.equal(REVERSE_CONVERSIONS[3000003].recipeId, 58);
  assert.equal(REVERSE_CONVERSIONS[3000003].sourceName, 'Mảnh Sắt');
  // Hồng ngọc (3000052) is created via recipe 62 from Topaz
  assert.equal(REVERSE_CONVERSIONS[3000052].recipeId, 62);
  assert.equal(REVERSE_CONVERSIONS[3000052].sourceName, 'Topaz');
});

test('index.html uses updated cache buster string to prevent stale script caching', () => {
  const indexHtml = fs.readFileSync(path.join(__dirname, '../www/html5/index.html'), 'utf-8');
  assert.ok(!indexHtml.includes('m81345-inventory-click-fix-9e251171f0314e86641a'), 'Old stale cache query string must not exist in index.html');
  assert.ok(indexHtml.includes('?v=v2_balance_patch_20261124_02'), 'New cache buster string must be present in index.html');
});

test('app.py configures Cache-Control: no-cache, must-revalidate for static assets', () => {
  const appPy = fs.readFileSync(path.join(__dirname, '../server/app.py'), 'utf-8');
  assert.ok(appPy.includes("self.send_header('Cache-Control', 'no-cache, must-revalidate')"), 'app.py must set revalidate header');
});
