/*
 * Prove the browser patcher and patch.py emit the same bytes.
 *
 *   node tools/compare.mjs <vanilla.bin> <reference-patched.bin>
 *
 * The reference is whatever patch.py produced. Any divergence between the two
 * implementations shows up here rather than in someone's emulator.
 */
import { readFileSync } from 'node:fs';
import { patchImage } from '../web/patcher.js';

const [vanillaPath, referencePath] = process.argv.slice(2);
if (!vanillaPath || !referencePath) {
  console.error('usage: node tools/compare.mjs <vanilla.bin> <reference.bin>');
  process.exit(2);
}

const buf = new Uint8Array(readFileSync(vanillaPath));
console.log(`input      ${vanillaPath}  (${buf.length / 0x930} sectors)`);

const t0 = Date.now();
const { touched } = patchImage(buf, { barry: true, monsterShop: true },
                               (l) => console.log('  ' + l));
console.log(`patched in ${Date.now() - t0} ms`);

const ref = new Uint8Array(readFileSync(referencePath));
if (ref.length !== buf.length) {
  console.error(`\nSIZE MISMATCH: ${buf.length} vs ${ref.length}`);
  process.exit(1);
}

const diffs = [];
for (let i = 0; i < ref.length; i++) {
  if (ref[i] !== buf[i]) {
    diffs.push(i);
    if (diffs.length > 64) break;
  }
}

if (!diffs.length) {
  console.log(`\nMATCH: byte-identical to ${referencePath}`);
  console.log(`sectors rewritten: ${touched.join(', ')}`);
  process.exit(0);
}

console.error(`\nDIFFERS: at least ${diffs.length} bytes`);
for (const i of diffs.slice(0, 16)) {
  console.error(`  0x${i.toString(16).padStart(8, '0')} ` +
                `(sector ${Math.floor(i / 0x930)})  ` +
                `ref ${ref[i].toString(16).padStart(2, '0')} != ` +
                `got ${buf[i].toString(16).padStart(2, '0')}`);
}
process.exit(1);
