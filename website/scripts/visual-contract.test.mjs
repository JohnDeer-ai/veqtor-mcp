import assert from 'node:assert/strict'
import { readdirSync, readFileSync } from 'node:fs'
import { dirname, join, relative } from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'

const websiteDir = join(dirname(fileURLToPath(import.meta.url)), '..')
const readSource = (...parts) => readFileSync(join(websiteDir, 'src', ...parts), 'utf8')

const globalCss = readSource('styles', 'global.css')
const baseLayout = readSource('layouts', 'BaseLayout.astro')
const header = readSource('components', 'Header.astro')
const setup = readSource('pages', 'setup.astro')
const authorPage = readSource('pages', 'author', 'ilya-shilov.astro')
const guideSource = readSource('data', 'guides-source.json')
const guideArticle = readSource('pages', 'guides', '[slug].astro')
const guideShellPages = [
  ['guide index', readSource('pages', 'guides', 'index.astro')],
  ['guide topic', readSource('pages', 'guides', 'topics', '[topic].astro')],
  ['guide search', readSource('pages', 'guides', 'search.astro')],
  ['guide article', guideArticle],
]

const sourceFiles = (directory) => readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
  const path = join(directory, entry.name)
  if (entry.isDirectory()) return sourceFiles(path)
  return /\.(?:astro|css)$/.test(entry.name) ? [path] : []
})

const cssVariables = new Map(
  [...globalCss.matchAll(/(--[a-z0-9-]+)\s*:\s*([^;]+);/gi)].map((match) => [match[1], match[2].trim()]),
)

const provableFontFloor = (value, seen = new Set()) => {
  const normalized = value.trim()
  const absolute = normalized.match(/^(-?(?:\d+(?:\.\d*)?|\.\d+))(px|rem)$/)
  if (absolute) return Number(absolute[1]) * (absolute[2] === 'rem' ? 16 : 1)

  const variable = normalized.match(/^var\((--[a-z0-9-]+)\)$/i)?.[1]
  if (variable) {
    if (seen.has(variable) || !cssVariables.has(variable)) return null
    return provableFontFloor(cssVariables.get(variable), new Set([...seen, variable]))
  }

  const clampFloor = normalized.match(/^clamp\(\s*([^,]+),/i)?.[1]
  if (clampFloor) return provableFontFloor(clampFloor, seen)

  return null
}

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

test('micro typography uses the shared 11px minimum across the site', () => {
  assert.match(globalCss, /--type-micro:\s*11px;/)
  assert.equal(provableFontFloor('var(--type-micro)'), 11)
  assert.equal(provableFontFloor('0.7rem'), 11.2)
  assert.equal(provableFontFloor('clamp(0.7rem, 0.86em, 1em)'), 11.2)
  for (const contextualValue of ['0.7em', '50%', '1vw', 'calc(24px - 14px)', 'var(--unknown-size)']) {
    assert.equal(provableFontFloor(contextualValue), null)
  }

  const violations = []
  for (const path of sourceFiles(join(websiteDir, 'src'))) {
    const source = readFileSync(path, 'utf8')
    for (const declaration of source.matchAll(/font-size\s*:\s*([^;}]+)/g)) {
      const value = declaration[1].trim()
      const floor = provableFontFloor(value)
      if (floor === null || floor < 11 || (floor === 11 && value !== 'var(--type-micro)')) {
        const line = source.slice(0, declaration.index ?? 0).split('\n').length
        violations.push(`${relative(websiteDir, path)}:${line}: ${declaration[0].trim()}`)
      }
    }
  }

  assert.deepEqual(violations, [], 'every font-size needs a provable 11px floor; use var(--type-micro) at the minimum')
  assert.match(setup, /\.setup-nav strong\s*\{[^}]*font-size:\s*var\(--type-micro\);/s)
  assert.match(guideArticle, /\.toc a span,[\s\S]*?font-size:\s*var\(--type-micro\);/)
  assert.match(guideArticle, /\.table-wrap th\)\s*\{[^}]*font-size:\s*0\.7rem;/s)
})

test('author credentials and social profiles keep their concise card treatment', () => {
  const guideData = JSON.parse(guideSource)
  assert.equal(guideData.author.facts.at(-1), 'LL.M., International Law')
  assert.match(authorPage, /social-link social-link--linkedin/)
  assert.match(authorPage, /social-link social-link--telegram/)
  assert.match(authorPage, /figcaption\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\);/s)
  assert.match(authorPage, /\.social-link\s*\{[^}]*grid-template-columns:[^}]*border-radius:/s)
  assert.match(authorPage, /\.social-link:focus-visible/)
})
