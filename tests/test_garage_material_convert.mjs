import test from 'node:test';
import assert from 'node:assert/strict';
import { MATERIAL_CONVERSIONS, REVERSE_CONVERSIONS } from '../www/html5/m8_garage.js';

test('MATERIAL_CONVERSIONS maps base materials to higher tier', () => {
  assert.equal(MATERIAL_CONVERSIONS[3000001].recipeId, 58);
  assert.equal(MATERIAL_CONVERSIONS[3000001].targetName, 'Thép');
  assert.equal(MATERIAL_CONVERSIONS[3000003].recipeId, 59);
  assert.equal(MATERIAL_CONVERSIONS[3000003].targetName, 'Carbon');
  assert.equal(MATERIAL_CONVERSIONS[3000051].recipeId, 62);
  assert.equal(MATERIAL_CONVERSIONS[3000051].targetName, 'Hồng ngọc');
  assert.equal(MATERIAL_CONVERSIONS[3000052].recipeId, 63);
  assert.equal(MATERIAL_CONVERSIONS[3000052].targetName, 'Lục ngọc');
});

test('REVERSE_CONVERSIONS maps higher tier to recipe that creates it', () => {
  // Thép (3000003) is created via recipe 58 from Mảnh Sắt
  assert.equal(REVERSE_CONVERSIONS[3000003].recipeId, 58);
  assert.equal(REVERSE_CONVERSIONS[3000003].sourceName, 'Mảnh Sắt');
  // Hồng ngọc (3000052) is created via recipe 62 from Topaz
  assert.equal(REVERSE_CONVERSIONS[3000052].recipeId, 62);
  assert.equal(REVERSE_CONVERSIONS[3000052].sourceName, 'Topaz');
});
