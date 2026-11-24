import test from 'node:test';
import assert from 'node:assert';
import fs from 'node:fs';
import path from 'node:path';

test('index.html contains battle music toggle buttons in toolbar and settings', () => {
  const html = fs.readFileSync(path.resolve('www/html5/index.html'), 'utf-8');
  assert.match(html, /id="battle-music"/, 'index.html should contain #battle-music button in battle options');
  assert.match(html, /id="robo-music"/, 'index.html should contain #robo-music button in robo settings');
});

test('battle_view.js integrates fight_bg.mp3 and music toggles', () => {
  const source = fs.readFileSync(path.resolve('www/html5/battle_view.js'), 'utf-8');
  assert.match(source, /fight_bg\.mp3/, 'battle_view.js should reference fight_bg.mp3 for battle background music');
  assert.match(source, /syncBattleMusic/, 'battle_view.js should define syncBattleMusic');
  assert.match(source, /battle-music/, 'battle_view.js should wire #battle-music');
  assert.match(source, /robo-music/, 'battle_view.js should wire #robo-music');
});
