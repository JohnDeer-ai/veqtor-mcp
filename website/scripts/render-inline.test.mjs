import assert from 'node:assert/strict'
import { test } from 'node:test'
import { renderInline } from '../src/lib/render-inline.mjs'

test('renders safe links, bold and italics', () => {
  assert.equal(
    renderInline('See [the pillar](/guides/ai-agent-contracting) first.'),
    'See <a href="/guides/ai-agent-contracting">the pillar</a> first.',
  )
  assert.equal(
    renderInline('[UNCITRAL](https://uncitral.un.org/en/mlac)'),
    '<a href="https://uncitral.un.org/en/mlac">UNCITRAL</a>',
  )
  assert.equal(renderInline('[top](#sources-heading)'), '<a href="#sources-heading">top</a>')
  assert.equal(renderInline('**Bold.** Rest'), '<strong>Bold.</strong> Rest')
  assert.equal(renderInline('per *Longley* only'), 'per <em>Longley</em> only')
  assert.equal(
    renderInline('[*RTS v Muller*](https://example.org/case)'),
    '<a href="https://example.org/case"><em>RTS v Muller</em></a>',
  )
})

test('blocks scriptable and protocol-relative link targets', () => {
  const js = renderInline('[click](javascript:alert(1))')
  assert.ok(!js.includes('<a'), js)
  assert.ok(js.startsWith('click'), js)
  const data = renderInline('[click](data:text/html,evil)')
  assert.equal(data, 'click')
  const protocolRelative = renderInline('[click](//evil.example)')
  assert.equal(protocolRelative, 'click')
  const mailto = renderInline('[click](mailto:a@b.c)')
  assert.equal(mailto, 'click')
})

test('escapes quotes, ampersands and angle brackets everywhere', () => {
  assert.equal(
    renderInline('Freeman & Lockyer said "no" to <script>'),
    'Freeman &amp; Lockyer said &quot;no&quot; to &lt;script&gt;',
  )
  const attribute = renderInline('[x](/a"b)')
  assert.equal(attribute, '<a href="/a&quot;b">x</a>')
})

test('asterisks in labels and unpaired markers stay predictable', () => {
  assert.equal(renderInline('a * b'), 'a * b')
  assert.equal(renderInline('rate of 5*x* growth'), 'rate of 5<em>x</em> growth')
})

test('parenthesised URLs work when percent-encoded', () => {
  assert.equal(
    renderInline('[case](https://example.org/wiki/A_%28B%29)'),
    '<a href="https://example.org/wiki/A_%28B%29">case</a>',
  )
  const unencoded = renderInline('[case](https://example.org/path(1))')
  assert.ok(unencoded.includes('href="https://example.org/path(1"'), unencoded)
})
