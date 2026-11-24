import test from 'node:test';
import assert from 'node:assert';
import { canonicalShopIntent, validShopQuote, shopQuoteMatchesIntent } from '../www/html5/m81345_shop_protocol.js';

test('canonicalShopIntent handles open_box with default and explicit quantity', () => {
  const item = {
    container: 2,
    uuid: 'box-uuid-1',
    template_id: 2009302,
    count: 10,
    slot_no: 0
  };

  const intentDefault = canonicalShopIntent({
    kind: 'open_box',
    item
  });
  assert.strictEqual(intentDefault.kind, 'open_box');
  assert.strictEqual(intentDefault.quantity, 1);
  assert.strictEqual(intentDefault.item.template_id, 2009302);

  const intent5 = canonicalShopIntent({
    kind: 'open_box',
    item,
    quantity: 5
  });
  assert.strictEqual(intent5.kind, 'open_box');
  assert.strictEqual(intent5.quantity, 5);

  const intent10 = canonicalShopIntent({
    kind: 'open_box',
    item,
    quantity: 10
  });
  assert.strictEqual(intent10.quantity, 10);
});

test('validShopQuote and shopQuoteMatchesIntent validate open_box with quantity', () => {
  const quote = {
    kind: 'open_box',
    profile_id: 12345,
    policy: 'all_or_nothing',
    item_id: 2009302,
    name: 'Trứng Xe Bí Ngô',
    quantity: 5,
    box: {
      itemId: 2009302,
      name: 'Trứng Xe Bí Ngô',
      mode: 'random',
      main_percent: 10,
      material_percent: 90,
      duplicate_policy: 'reject_owned_unique_main',
      main_rewards: [{ kind: 'tank', id: 10000001, name: 'Bí Ngô', quantity: 1, category: 'Tank' }],
      material_rewards: [{ kind: 'item', id: 3000001, name: 'Sắt', quantity: 5, category: 'Nguyên liệu' }]
    }
  };

  assert.strictEqual(validShopQuote(quote), true);

  const item = {
    container: 2,
    uuid: 'box-uuid-1',
    template_id: 2009302,
    count: 10,
    slot_no: 0
  };
  const intent5 = canonicalShopIntent({ kind: 'open_box', item, quantity: 5 });
  assert.strictEqual(shopQuoteMatchesIntent(quote, intent5), true);

  const intent1 = canonicalShopIntent({ kind: 'open_box', item, quantity: 1 });
  assert.strictEqual(shopQuoteMatchesIntent(quote, intent1), false);
});
