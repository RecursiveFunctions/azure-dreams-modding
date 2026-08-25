/* Azure Dreams Shop Patcher -- the default configuration and price rules.
 *
 * Shared by the interface and by tools/compare.mjs, so the browser, node and
 * (by hand-kept agreement) patch.py all start from the same defaults.
 */
import { ITEMS, CATEGORIES } from './items.js';

export const CAT_EGG = 0x12;
export const CAT_SAND = 0x0a;
export const CAT_BALL = 0x04;
export const PRICE_MAX = 0xffff;

export const key = (cat, id) => `${cat}:${id}`;
export const itemByKey = new Map(ITEMS.map((it) => [key(it.cat, it.id), it]));
export const categoryByCat = new Map(CATEGORIES.map((c) => [c.cat, c]));

/** Vanilla Barry plus the seven eggs the first version of this patch added. */
export const BARRY_DEFAULT_STOCK = [
  [0x0f, 0x02], [0x01, 0x01],
  [CAT_EGG, 0x02], [CAT_EGG, 0x16], [CAT_EGG, 0x10], [CAT_EGG, 0x0e],
  [CAT_EGG, 0x08], [CAT_EGG, 0x0c], [CAT_EGG, 0x03],
];

/**
 * Default prices: sell stays what the game says, buy is twice that. Sand is
 * the exception the patch was asked for -- 1000G to buy, 500G to sell. An
 * item that sells for nothing keeps its vanilla buy price rather than being
 * given away, and nothing exceeds the 16-bit field.
 */
export function defaultPrice(item) {
  if (item.cat === CAT_SAND) return { buy: 1000, sell: 500 };
  const sell = item.sell;
  const buy = sell === 0 ? item.buy : Math.min(sell * 2, PRICE_MAX);
  return { buy, sell };
}

/** Quality byte a shop hands out: balls need charges, everything else 0. */
export function defaultQuality(item) {
  return item.cat === CAT_BALL ? 5 : 0;
}

/**
 * What the game will actually quote for a stocked item. Balls and equipment
 * go through a price routine that adds base/10 per point of quality.
 */
export function quotedPrice(item, price, quality) {
  const c = categoryByCat.get(item.cat);
  if (!c || !c.scaled) return price;
  const p = price + Math.floor(price / 10) * quality;
  return Math.max(1, p);
}

export function defaultConfig() {
  const prices = {};
  const stockOf = (pairs) => pairs.map(([cat, id]) => {
    const it = itemByKey.get(key(cat, id));
    prices[key(cat, id)] = defaultPrice(it);
    return { cat, id, quality: defaultQuality(it) };
  });
  const barry = stockOf(BARRY_DEFAULT_STOCK);
  const eggs = ITEMS.filter((it) => it.cat === CAT_EGG).map((it) => [it.cat, it.id]);
  const monster = stockOf(eggs);
  return {
    barry: { enabled: true, stock: barry },
    monsterShop: { enabled: true, stock: monster },
    prices,
  };
}
