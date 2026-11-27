// Unit test for src/lib/lengthenDefault.ts: Lengthen's minutes start at the
// chosen part's own length, not the whole story's target.
//
// Usage:  node --experimental-strip-types frontend/tests/lengthen_default.mjs

import assert from 'node:assert/strict';
import { lengthenDefault } from '../src/lib/lengthenDefault.ts';

const chapters = [{ line: 1, est_seconds: 290 }, { line: 20, est_seconds: 61 }];
assert.equal(lengthenDefault('chapter', chapters, 1, 1800, 90), 5);
assert.equal(lengthenDefault('chapter', chapters, 20, 1800, 90), 2);
assert.equal(lengthenDefault('chapter', chapters, 999, 1800, 90), 5); // stale choice: the first chapter
assert.equal(lengthenDefault('chapter', [], undefined, 1800, 90), null);
assert.equal(lengthenDefault('story', chapters, 1, 1800, 90), 30);
assert.equal(lengthenDefault('story', chapters, 1, 99999, 90), 90); // capped
assert.equal(lengthenDefault('story', [], undefined, 0, 90), 1);
assert.equal(lengthenDefault('selection', chapters, 1, 1800, 90), null); // the server measures the selection
console.log('lengthen_default: ok');
