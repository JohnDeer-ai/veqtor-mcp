import assert from 'node:assert/strict'
import { test } from 'node:test'

import { latestLastmod } from '../src/lib/sitemap-lastmod.mjs'

test('bridge update wins when it is newer than the editorial date', () => {
  assert.equal(latestLastmod('2026-06-14', '2026-07-23'), '2026-07-23')
})

test('a future editorial update wins over the bridge date', () => {
  assert.equal(latestLastmod('2026-08-04', '2026-07-23'), '2026-08-04')
})

test('lastmod selection rejects invalid calendar dates', () => {
  assert.throws(
    () => latestLastmod('2026-02-30'),
    /Expected an ISO calendar date/,
  )
})
