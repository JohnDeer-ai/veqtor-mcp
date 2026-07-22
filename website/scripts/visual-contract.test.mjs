import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'

const websiteDir = join(dirname(fileURLToPath(import.meta.url)), '..')
const readSource = (...parts) => readFileSync(join(websiteDir, 'src', ...parts), 'utf8')

const globalCss = readSource('styles', 'global.css')
const baseLayout = readSource('layouts', 'BaseLayout.astro')
const header = readSource('components', 'Header.astro')
const setup = readSource('pages', 'setup.astro')
const guideArticle = readSource('pages', 'guides', '[slug].astro')
const guideShellPages = [
  ['guide index', readSource('pages', 'guides', 'index.astro')],
  ['guide topic', readSource('pages', 'guides', 'topics', '[topic].astro')],
  ['guide search', readSource('pages', 'guides', 'search.astro')],
  ['guide article', guideArticle],
]

test('guide headers inherit the global shell while only the article grid runs wide', () => {
  for (const [label, source] of guideShellPages) {
    assert.doesNotMatch(
      source,
      /(?:^|\n)\s*\.shell(?:\s*,|\s*\{)/m,
      `${label} must not override the global shell`,
    )
  }
  assert.match(globalCss, /--shell:\s*1240px;/)
  assert.match(globalCss, /--shell-wide:\s*1800px;/)
  assert.match(
    guideArticle,
    /\.article-shell\.shell\s*\{[^}]*width:\s*min\(var\(--shell-wide\),\s*calc\(100% - 2\.5rem\)\);[^}]*max-width:\s*none;/s,
  )
  assert.match(
    guideArticle,
    /@media \(max-width:\s*1140px\)[\s\S]*?\.article-shell\.shell\s*\{[^}]*width:\s*100%;/,
  )
  assert.match(guideArticle, /\.article-body\s*\{[^}]*min-width:\s*0;/s)
  assert.match(guideArticle, /\.table-wrap\)\s*\{[^}]*overflow-x:\s*auto;/s)
})

test('Inter ships a real italic face for rendered emphasis', () => {
  assert.match(baseLayout, /@fontsource-variable\/inter\/wght\.css/)
  assert.match(baseLayout, /@fontsource-variable\/inter\/wght-italic\.css/)
})

test('main navigation exposes and styles the current section', () => {
  assert.match(header, /aria-current=\{currentValue\(item\.href\)\}/)
  assert.match(header, /return 'location'/)
  assert.match(globalCss, /\.site-nav a\[aria-current\]/)
})

test('micro typography has a shared readable minimum', () => {
  assert.match(globalCss, /--type-micro:\s*11px;/)
  assert.match(setup, /\.setup-nav strong\s*\{[^}]*font-size:\s*var\(--type-micro\);/s)
  assert.match(guideArticle, /\.toc a span,[\s\S]*?font-size:\s*var\(--type-micro\);/)
  assert.match(guideArticle, /\.table-wrap th\)\s*\{[^}]*font-size:\s*0\.7rem;/s)
})
