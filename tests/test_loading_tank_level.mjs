import test from 'node:test';
import assert from 'node:assert';
import { loadingCardModel } from '../www/html5/m86_loading.js';
import fs from 'node:fs';
import path from 'node:path';

test('loadingCardModel preserves tank level for player and bot', () => {
  const playerChar = { accountId: 101, camp: 1, tankLevel: 5, tankId: 10000001, name: 'Player1' };
  const playerModel = loadingCardModel(playerChar, 1);
  assert.strictEqual(playerModel.tank.level, 5, 'Player tank level should be preserved in card model');

  const botChar = { accountId: 0, camp: 2, tankLevel: 4, tankId: 10000019, name: 'Bot1' };
  const botModel = loadingCardModel(botChar, 1);
  assert.strictEqual(botModel.tank.level, 4, 'Bot tank level should be preserved in card model');
});

test('m86_loading.js source contains tank level badge rendering in renderRoster', () => {
  const source = fs.readFileSync(path.resolve('www/html5/m86_loading.js'), 'utf-8');
  assert.match(source, /m86-loading-tank-level/, 'renderRoster should render an element with class m86-loading-tank-level');
  assert.match(source, /c\.tankLevel/, 'renderRoster should reference c.tankLevel for the level indicator');
});

test('battle.css contains styling for m86-loading-tank-level', () => {
  const css = fs.readFileSync(path.resolve('www/html5/battle.css'), 'utf-8');
  assert.match(css, /\.m86-loading-tank-level/, 'battle.css should define styles for .m86-loading-tank-level');
});
