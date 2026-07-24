import assert from 'node:assert/strict'
import { test } from 'node:test'

import { validateRenderedBridge } from './lib/rendered-bridge.mjs'

const options = {
  className: 'product-bridge',
  pageUrl: 'https://veqtor.pro/guides/example',
  expectedTarget: '/contract-redline-analysis',
  siteOrigin: 'https://veqtor.pro',
}

test('bridge validation follows the outer section across nested sections', () => {
  assert.doesNotThrow(() => validateRenderedBridge({
    ...options,
    html: '<main><section class="product-bridge"><section><p>Nested content</p></section><a href="/contract-redline-analysis">Review</a></section></main>',
  }))
})

test('bridge validation rejects a bridge moved outside main', () => {
  assert.throws(
    () => validateRenderedBridge({
      ...options,
      html: '<main><p>Article</p></main><section class="product-bridge"><a href="/contract-redline-analysis">Review</a></section>',
    }),
    /expected one section\.product-bridge inside <main>, found 0/,
  )
})

test('bridge validation sees an unexpected anchor after a nested section', () => {
  assert.throws(
    () => validateRenderedBridge({
      ...options,
      html: '<main><section class="product-bridge"><section><a href="/contract-redline-analysis">Review</a></section><a href="/demo">Unexpected</a></section></main>',
    }),
    /exactly one descendant anchor, found 2/,
  )
})

test('bridge validation requires exactly one main element', () => {
  assert.throws(
    () => validateRenderedBridge({
      ...options,
      html: '<main></main><main><section class="product-bridge"><a href="/contract-redline-analysis">Review</a></section></main>',
    }),
    /expected one <main>, found 2/,
  )
})
