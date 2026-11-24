import test from 'node:test';
import assert from 'node:assert';
import { nativeEventKey, nativeInputOptions } from '../www/html5/m81_input_options.js';
import { buildScoreboardModel } from '../www/html5/battle_scoreboard.js';

test('nativeInputOptions correctly maps keycode 71 (G) to action 71', () => {
  const options = nativeInputOptions({});
  assert.strictEqual(options.byKey.get(71), 71, 'Key 71 (G) must be mapped to action 71');
});

test('buildScoreboardModel aggregates players by camp with KDA and damage', () => {
  const mockState = {
    selfObjectId: 101,
    scores: { camp1: 13, camp2: 0 },
    characters: [
      { accountId: 1, name: 'PlayerBlue1', camp: 1, tankId: 10000001, tankLevel: 5 },
      { accountId: 2, name: 'PlayerRed1', camp: 2, tankId: 10000073, tankLevel: 6 },
    ],
    actors: {
      '101': { objectId: 101, accountId: 1, name: 'PlayerBlue1', camp: 1, tankId: 10000001, tankLevel: 5 },
      '102': { objectId: 102, accountId: 2, name: 'PlayerRed1', camp: 2, tankId: 10000073, tankLevel: 6 },
    },
    stats: {
      '1': {
        accountId: 1,
        battleLevel: 5,
        stats: [4, 24, 24, 0, 0, 0, 15800, 0, 0, 0, 0, 0, 0, 4200], // deaths=4, kills=24, assists=0, dmg=15800
      },
      '2': {
        accountId: 2,
        battleLevel: 6,
        stats: [13, 8, 8, 0, 0, 0, 9200, 0, 0, 0, 0, 0, 0, 18500], // deaths=13, kills=8, assists=0, dmg=9200
      }
    }
  };

  const mockCatalog = {
    tanks: {
      '10000001': { name: 'Iron Man' },
      '10000073': { name: 'Baymax' },
    }
  };

  const model = buildScoreboardModel(mockState, mockCatalog);
  assert.strictEqual(model.score1, 13);
  assert.strictEqual(model.score2, 0);
  assert.strictEqual(model.camp1.length, 1);
  assert.strictEqual(model.camp2.length, 1);

  const p1 = model.camp1[0];
  assert.strictEqual(p1.name, 'PlayerBlue1');
  assert.strictEqual(p1.tankName, 'Iron Man');
  assert.strictEqual(p1.tankLevel, 5);
  assert.strictEqual(p1.kills, 24);
  assert.strictEqual(p1.deaths, 4);
  assert.strictEqual(p1.assists, 0);
  assert.strictEqual(p1.damage, 15800);
  assert.strictEqual(p1.isSelf, true);

  const p2 = model.camp2[0];
  assert.strictEqual(p2.name, 'PlayerRed1');
  assert.strictEqual(p2.tankName, 'Baymax');
  assert.strictEqual(p2.tankLevel, 6);
  assert.strictEqual(p2.kills, 8);
  assert.strictEqual(p2.deaths, 13);
  assert.strictEqual(p2.damage, 9200);
  assert.strictEqual(p2.isSelf, false);
});

test('battle_input.js wires keycode 71/KeyG and Escape to onToggleScoreboard', async () => {
  const fs = await import('node:fs');
  const path = await import('node:path');
  const inputCode = fs.readFileSync(path.resolve('www/html5/battle_input.js'), 'utf-8');
  assert.match(inputCode, /action === 71 \|\| code === 'KeyG'/, 'battle_input.js must detect action 71 or KeyG');
  assert.match(inputCode, /this\.onToggleScoreboard\?\.\(\)/, 'battle_input.js must trigger onToggleScoreboard');
  assert.match(inputCode, /this\.onToggleScoreboard\?\.\(\s*false\s*\)/, 'battle_input.js must close scoreboard on Escape');
});

test('battle_view.js initializes BattleScoreboard and hooks updates into drawHud', async () => {
  const fs = await import('node:fs');
  const path = await import('node:path');
  const viewCode = fs.readFileSync(path.resolve('www/html5/battle_view.js'), 'utf-8');
  assert.match(viewCode, /import.*BattleScoreboard.*from '\.\/battle_scoreboard\.js'/, 'battle_view.js must import BattleScoreboard');
  assert.match(viewCode, /this\.scoreboard = new BattleScoreboard/, 'battle_view.js must instantiate BattleScoreboard');
  assert.match(viewCode, /this\.input\.onToggleScoreboard =/, 'battle_view.js must wire up onToggleScoreboard handler');
  assert.match(viewCode, /this\.scoreboard\?\.visible\s*\)\s*\{\s*this\.scoreboard\.update/, 'drawHud must update scoreboard when visible');
});

test('battle.css defines scoreboard styles matching esport two-team modal', async () => {
  const fs = await import('node:fs');
  const path = await import('node:path');
  const css = fs.readFileSync(path.resolve('www/html5/battle.css'), 'utf-8');
  assert.match(css, /\.m8-scoreboard-overlay/, 'CSS must style .m8-scoreboard-overlay');
  assert.match(css, /\.m8-scoreboard-modal/, 'CSS must style .m8-scoreboard-modal');
  assert.match(css, /\.m8-scoreboard-camp\.camp1/, 'CSS must style .m8-scoreboard-camp.camp1 (Phe Xanh)');
  assert.match(css, /\.m8-scoreboard-camp\.camp2/, 'CSS must style .m8-scoreboard-camp.camp2 (Phe Đỏ)');
  assert.match(css, /\.is-self/, 'CSS must style .is-self highlight for current player');
});
