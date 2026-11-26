// Unit test for src/lib/textOffsets.ts: the API's code-point offsets and the
// editor's UTF-16 offsets agree around characters outside the BMP (emoji).
//
// Usage:  node --experimental-strip-types frontend/tests/text_offsets.mjs
// Exits non-zero on any failure.

import assert from 'node:assert/strict';
import { spliceCodePoints, toCodePoints, toUnits } from '../src/lib/textOffsets.ts';

const s = '# Ch1\nNARRATOR: hi \u{1F600} there\n';
const cps = Array.from(s);
assert.equal(s.length, cps.length + 1);

// A selection of "there" after the emoji: what the server slices is the same.
const a = s.indexOf('there');
const b = a + 'there'.length;
assert.equal(cps.slice(toCodePoints(s, a), toCodePoints(s, b)).join(''), 'there');
for (let cp = 0; cp <= cps.length; cp++) assert.equal(toCodePoints(s, toUnits(s, cp)), cp);

// Whole story: [0, len in code points) replaced leaves nothing of the old text.
assert.equal(spliceCodePoints(s, 0, cps.length, 'NEW'), 'NEW');
// A range after the emoji is replaced exactly.
const pa = cps.indexOf('t');
assert.equal(spliceCodePoints(s, pa, pa + 5, 'THERE'), '# Ch1\nNARRATOR: hi \u{1F600} THERE\n');
// No lone surrogates come out.
assert.ok(spliceCodePoints(s, 0, cps.length - 1, 'X').isWellFormed());
console.log('text offsets: ok');
