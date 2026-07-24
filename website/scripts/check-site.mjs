import { createHash } from 'node:crypto'
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { LINK_ARCHITECTURE_LASTMOD, latestLastmod } from '../src/lib/sitemap-lastmod.mjs'
import { validateRenderedBridge } from './lib/rendered-bridge.mjs'

const SITE_ORIGIN = 'https://veqtor.pro'
const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url))
const WEBSITE_DIR = resolve(SCRIPT_DIR, '..')
const DIST_DIR = join(WEBSITE_DIR, 'dist')
const GUIDE_SOURCE_PATH = join(WEBSITE_DIR, 'src', 'data', 'guides-source.json')
const ALLOWED_NEW_ROUTES = ['/setup', '/docs', '/limitations', '/veqtor-vs-claude-for-word']
// Pinned from the approved guide inventory and reviewed editorial metadata.
// Updating either hash is an explicit SEO decision, not routine content editing.
const LEGACY_ROUTE_MANIFEST_SHA256 = '576d80131cb6b42902802646b727550ecf7080dd69f426a1060271ae8586dddc'
const EDITORIAL_SEO_SHA256 = '346f34f6940ab54f2b7729cbbe94ffda2a86617b143e405c2e166ea837b6c01d'

const STATIC_LEGACY_ROUTES = [
  '/',
  '/product',
  '/how-it-works',
  '/security',
  '/demo',
  '/ai-contract-review',
  '/contract-redline-analysis',
  '/docx-track-changes-review',
  '/terms',
  '/privacy',
  '/author/ilya-shilov',
  '/guides',
  '/guides/search',
]

const failures = []

function fail(message) {
  failures.push(message)
}

function readUtf8(path) {
  return readFileSync(path, 'utf8')
}

function normalizeRoute(pathname) {
  if (!pathname || pathname === '/') return '/'
  return `/${pathname.replace(/^\/+|\/+$/g, '')}`
}

function canonicalUrl(route) {
  return route === '/' ? `${SITE_ORIGIN}/` : `${SITE_ORIGIN}${route}`
}

function routeCandidates(route) {
  if (route === '/') return [join(DIST_DIR, 'index.html')]
  const clean = route.replace(/^\//, '')
  return [join(DIST_DIR, clean, 'index.html'), join(DIST_DIR, `${clean}.html`)]
}

function htmlOutputForRoute(route) {
  return routeCandidates(route).find((candidate) => existsSync(candidate) && statSync(candidate).isFile()) ?? null
}

function walkFiles(root) {
  const files = []
  for (const entry of readdirSync(root, { withFileTypes: true })) {
    const path = join(root, entry.name)
    if (entry.isDirectory()) files.push(...walkFiles(path))
    else if (entry.isFile()) files.push(path)
  }
  return files
}

function routeForHtmlOutput(path) {
  const rel = relative(DIST_DIR, path).split('\\').join('/')
  if (rel === 'index.html') return '/'
  if (rel === '404.html') return '/404.html'
  if (rel.endsWith('/index.html')) return `/${rel.slice(0, -'/index.html'.length)}`
  if (rel.endsWith('.html')) return `/${rel.slice(0, -'.html'.length)}`
  return null
}

function decodeEntities(value) {
  return String(value)
    .replaceAll('&amp;', '&')
    .replaceAll('&quot;', '"')
    .replaceAll('&#39;', "'")
    .replaceAll('&lt;', '<')
    .replaceAll('&gt;', '>')
    .replace(/&#(\d+);/g, (_match, decimal) => String.fromCodePoint(Number(decimal)))
    .replace(/&#x([0-9a-f]+);/gi, (_match, hex) => String.fromCodePoint(Number.parseInt(hex, 16)))
}

function normalizeText(value) {
  return decodeEntities(
    String(value)
      .replace(/<!--([\s\S]*?)-->/g, ' ')
      .replace(/<[^>]+>/g, ' '),
  )
    .replace(/\s+/g, ' ')
    .trim()
}

function attributesForTag(tag) {
  const body = tag.replace(/^<[^\s>]+/, '').replace(/\/?>$/, '')
  const attributes = new Map()
  const pattern = /([^\s=/>]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/g
  for (const match of body.matchAll(pattern)) {
    attributes.set(match[1].toLowerCase(), decodeEntities(match[2] ?? match[3] ?? match[4] ?? ''))
  }
  return attributes
}

function tags(html, name) {
  return [...html.matchAll(new RegExp(`<${name}\\b[^>]*>`, 'gi'))].map((match) => match[0])
}

function matchingTags(html, name, predicate) {
  return tags(html, name).filter((tag) => predicate(attributesForTag(tag)))
}

function pairedContents(html, name) {
  return [...html.matchAll(new RegExp(`<${name}\\b[^>]*>([\\s\\S]*?)<\\/${name}>`, 'gi'))].map(
    (match) => normalizeText(match[1]),
  )
}

function pageSignals(html) {
  const title = pairedContents(html, 'title')
  const h1 = pairedContents(html, 'h1')
  const canonical = matchingTags(
    html,
    'link',
    (attrs) => (attrs.get('rel') ?? '').toLowerCase().split(/\s+/).includes('canonical'),
  ).map((tag) => attributesForTag(tag).get('href') ?? '')
  const description = matchingTags(
    html,
    'meta',
    (attrs) => (attrs.get('name') ?? '').toLowerCase() === 'description',
  ).map((tag) => normalizeText(attributesForTag(tag).get('content') ?? ''))
  const twitterTitle = matchingTags(
    html,
    'meta',
    (attrs) => (attrs.get('name') ?? '').toLowerCase() === 'twitter:title',
  ).map((tag) => normalizeText(attributesForTag(tag).get('content') ?? ''))
  return { title, h1, canonical, description, twitterTitle }
}

function structuredDataNodes(route, html) {
  const nodes = []
  const scripts = [...html.matchAll(
    /<script\b[^>]*\btype=(?:"application\/ld\+json"|'application\/ld\+json')[^>]*>([\s\S]*?)<\/script>/gi,
  )]
  if (scripts.length === 0) {
    fail(`${route}: application/ld+json is missing`)
    return nodes
  }
  for (const script of scripts) {
    let value
    try {
      value = JSON.parse(script[1])
    } catch (error) {
      fail(`${route}: application/ld+json is invalid (${error.message})`)
      continue
    }
    if (Array.isArray(value)) nodes.push(...value)
    else if (Array.isArray(value?.['@graph'])) nodes.push(...value['@graph'])
    else nodes.push(value)
  }
  return nodes
}

function mainContent(route, html) {
  const content = html.match(/<main\b[^>]*>([\s\S]*?)<\/main>/i)?.[1]
  if (content === undefined) {
    fail(`${route}: <main> content is missing`)
    return ''
  }
  return content
}

function withoutBreadcrumbNavigation(html) {
  return html.replace(
    /<nav\b(?=[^>]*\baria-label=(?:"Breadcrumb"|'Breadcrumb'))[^>]*>[\s\S]*?<\/nav>/gi,
    '',
  )
}

function expectedMainNavigationCurrent(route) {
  if (route === '/product' || route === '/demo' || route === '/setup') {
    return { href: route, value: 'page' }
  }
  if (route === '/guides') return { href: '/guides', value: 'page' }
  if (route.startsWith('/guides/')) return { href: '/guides', value: 'location' }
  return null
}

function assertMainNavigationCurrent(route, html) {
  const navigation = html.match(/<nav\b[^>]*aria-label=(?:"Main navigation"|'Main navigation')[^>]*>[\s\S]*?<\/nav>/i)?.[0]
  if (!navigation) {
    fail(`${route}: main navigation is missing`)
    return
  }

  const current = matchingTags(
    navigation,
    'a',
    (attrs) => attrs.has('aria-current'),
  ).map((tag) => {
    const attrs = attributesForTag(tag)
    return { href: attrs.get('href') ?? '', value: (attrs.get('aria-current') ?? '').toLowerCase() }
  })
  const expected = expectedMainNavigationCurrent(route)

  if (expected && (current.length !== 1 || current[0].href !== expected.href || current[0].value !== expected.value)) {
    const found = current.map((item) => `${item.href} (${item.value})`).join(', ') || 'none'
    fail(`${route}: expected current main navigation link ${expected.href} (${expected.value}), found ${found}`)
  }
  if (!expected && current.length > 0) {
    fail(`${route}: unexpected current main navigation link ${current.map((item) => item.href).join(', ')}`)
  }
}

function sha256Json(value) {
  return createHash('sha256').update(JSON.stringify(value)).digest('hex')
}

function assertOneNonEmpty(route, label, values) {
  if (values.length !== 1) {
    fail(`${route}: expected exactly one ${label}, found ${values.length}`)
    return null
  }
  if (!values[0]) {
    fail(`${route}: ${label} is empty`)
    return null
  }
  return values[0]
}

function sitemapRoutes() {
  const sitemapPath = join(DIST_DIR, 'sitemap.xml')
  if (!existsSync(sitemapPath)) {
    fail('dist/sitemap.xml is missing')
    return null
  }
  const xml = readUtf8(sitemapPath)
  const locations = [...xml.matchAll(/<loc>\s*([^<]+?)\s*<\/loc>/gi)].map((match) =>
    decodeEntities(match[1].trim()),
  )
  const urlBlocks = [...xml.matchAll(/<url(?:\s[^>]*)?>[\s\S]*?<\/url>/gi)]
  if (urlBlocks.length !== locations.length) {
    fail(`sitemap: found ${urlBlocks.length} <url> blocks but ${locations.length} <loc> values`)
  }
  const routes = []
  for (const location of locations) {
    let url
    try {
      url = new URL(location)
    } catch {
      fail(`sitemap: invalid URL ${JSON.stringify(location)}`)
      continue
    }
    if (url.origin !== SITE_ORIGIN) {
      fail(`sitemap: URL uses unexpected origin: ${location}`)
      continue
    }
    if (url.search || url.hash) fail(`sitemap: URL must not contain query/hash: ${location}`)
    routes.push(normalizeRoute(url.pathname))
  }
  return routes
}

function staticFileForPath(pathname) {
  let decoded
  try {
    decoded = decodeURIComponent(pathname)
  } catch {
    return null
  }
  if (decoded.includes('\0') || decoded.split('/').includes('..')) return null
  const candidate = join(DIST_DIR, decoded.replace(/^\//, ''))
  if (existsSync(candidate) && statSync(candidate).isFile()) return candidate
  return null
}

function pageContainsFragment(html, fragment) {
  if (!fragment) return true
  let decoded
  try {
    decoded = decodeURIComponent(fragment)
  } catch {
    return false
  }
  return [...html.matchAll(/<[a-zA-Z][a-zA-Z0-9:-]*\b[^>]*>/g)]
    .map((match) => attributesForTag(match[0]))
    .some((attrs) => attrs.get('id') === decoded || attrs.get('name') === decoded)
}

function assertInternalLinks(pagesByRoute) {
  const missingTargets = new Map()
  for (const [sourceRoute, { html }] of pagesByRoute) {
    const sourceUrl = canonicalUrl(sourceRoute)
    const anchors = matchingTags(html, 'a', (attrs) => attrs.has('href'))
    for (const anchor of anchors) {
      const href = attributesForTag(anchor).get('href')?.trim() ?? ''
      if (!href || href === '#' || /^(mailto:|tel:|javascript:|data:)/i.test(href)) continue

      let targetUrl
      try {
        targetUrl = new URL(href, sourceUrl)
      } catch {
        fail(`${sourceRoute}: invalid href ${JSON.stringify(href)}`)
        continue
      }
      if (targetUrl.origin !== SITE_ORIGIN) continue

      const targetRoute = normalizeRoute(targetUrl.pathname)
      const targetPage = pagesByRoute.get(targetRoute)
      const targetStaticFile = staticFileForPath(targetUrl.pathname)
      const targetHtmlPath = targetPage?.path ?? htmlOutputForRoute(targetRoute)
      if (!targetHtmlPath && !targetStaticFile) {
        const key = `${targetUrl.pathname}${targetUrl.search}${targetUrl.hash}`
        const sources = missingTargets.get(key) ?? new Set()
        sources.add(sourceRoute)
        missingTargets.set(key, sources)
        continue
      }

      if (targetUrl.hash && targetHtmlPath) {
        const targetHtml = targetPage?.html ?? readUtf8(targetHtmlPath)
        const fragment = targetUrl.hash.slice(1)
        if (!pageContainsFragment(targetHtml, fragment)) {
          fail(`${sourceRoute}: href fragment does not exist: ${href}`)
        }
      }
    }
  }
  for (const [target, sources] of missingTargets) {
    const examples = [...sources].slice(0, 4)
    const suffix = sources.size > examples.length ? ` (+${sources.size - examples.length} more)` : ''
    fail(`internal href does not resolve: ${target}; linked from ${examples.join(', ')}${suffix}`)
  }
}

function assertIndexablePagesHaveInboundLinks(pagesByRoute, indexableRoutes) {
  const indexableSet = new Set(indexableRoutes)
  const inboundSources = new Map(
    [...indexableSet].map((route) => [route, new Set()]),
  )

  for (const [sourceRoute, { html }] of pagesByRoute) {
    if (!indexableSet.has(sourceRoute)) continue

    const sourceUrl = canonicalUrl(sourceRoute)
    const anchors = matchingTags(html, 'a', (attrs) => attrs.has('href'))
    for (const anchor of anchors) {
      const href = attributesForTag(anchor).get('href')?.trim() ?? ''
      if (!href || href === '#' || /^(mailto:|tel:|javascript:|data:)/i.test(href)) continue

      let targetUrl
      try {
        targetUrl = new URL(href, sourceUrl)
      } catch {
        continue
      }
      if (targetUrl.origin !== SITE_ORIGIN) continue

      const targetRoute = normalizeRoute(targetUrl.pathname)
      if (targetRoute === sourceRoute || !indexableSet.has(targetRoute)) continue
      inboundSources.get(targetRoute)?.add(sourceRoute)
    }
  }

  for (const [route, sources] of inboundSources) {
    if (route !== '/' && sources.size === 0) {
      fail(`${route}: indexable page has no inbound link from another indexable page`)
    }
  }
}

function assertPriorityPagesHaveContextualInboundLinks(pagesByRoute, indexableRoutes) {
  const minimumSources = new Map([
    ['/product', 4],
    ['/how-it-works', 3],
    ['/ai-contract-review', 3],
    ['/contract-redline-analysis', 3],
    ['/docx-track-changes-review', 3],
    ['/demo', 3],
  ])
  const indexableSet = new Set(indexableRoutes)
  const inboundSources = new Map(
    [...minimumSources].map(([route]) => [route, new Set()]),
  )

  for (const [sourceRoute, { html }] of pagesByRoute) {
    if (!indexableSet.has(sourceRoute)) continue
    const sourceUrl = canonicalUrl(sourceRoute)
    const contextualContent = withoutBreadcrumbNavigation(mainContent(sourceRoute, html))
    const anchors = matchingTags(contextualContent, 'a', (attrs) => attrs.has('href'))
    for (const anchor of anchors) {
      const href = attributesForTag(anchor).get('href')?.trim() ?? ''
      if (!href || href === '#' || /^(mailto:|tel:|javascript:|data:)/i.test(href)) continue
      let targetUrl
      try {
        targetUrl = new URL(href, sourceUrl)
      } catch {
        continue
      }
      if (targetUrl.origin !== SITE_ORIGIN) continue
      const targetRoute = normalizeRoute(targetUrl.pathname)
      if (targetRoute === sourceRoute || !minimumSources.has(targetRoute)) continue
      inboundSources.get(targetRoute)?.add(sourceRoute)
    }
  }

  for (const [route, minimum] of minimumSources) {
    const sources = inboundSources.get(route) ?? new Set()
    if (sources.size < minimum) {
      fail(`${route}: expected contextual inbound links from at least ${minimum} indexable pages, found ${sources.size}`)
    }
  }
}

function assertRenderedBridgeLink(route, html, className, expectedTarget) {
  try {
    validateRenderedBridge({
      html,
      className,
      pageUrl: canonicalUrl(route),
      expectedTarget,
      siteOrigin: SITE_ORIGIN,
    })
  } catch (error) {
    fail(`${route}: ${error.message}`)
  }
}

function assertRenderedBridgeCohorts(pagesByRoute, cohorts) {
  for (const cohort of cohorts) {
    if (cohort.routes.length !== cohort.expectedCount) {
      fail(`${cohort.label}: expected ${cohort.expectedCount} routes, found ${cohort.routes.length}`)
    }
    for (const route of cohort.routes) {
      const html = pagesByRoute.get(route)?.html ?? ''
      assertRenderedBridgeLink(route, html, cohort.sectionClass, cohort.target)
    }
  }
}

function assertUnique(label, valuesByRoute) {
  const routesByValue = new Map()
  for (const [route, value] of valuesByRoute) {
    const key = value.toLocaleLowerCase('en-US')
    const routes = routesByValue.get(key) ?? []
    routes.push(route)
    routesByValue.set(key, routes)
  }
  for (const [value, routes] of routesByValue) {
    if (routes.length > 1) {
      fail(`duplicate ${label} on ${routes.join(', ')}: ${JSON.stringify(value)}`)
    }
  }
}

function assertReal404(homeHtml) {
  const path = join(DIST_DIR, '404.html')
  if (!existsSync(path)) {
    fail('dist/404.html is missing; Cloudflare Pages would not have a real custom 404')
    return
  }
  const html = readUtf8(path)
  if (html === homeHtml) fail('dist/404.html is byte-identical to the homepage (soft 404)')
  const signals = pageSignals(html)
  const title = assertOneNonEmpty('/404.html', '<title>', signals.title)
  const h1 = assertOneNonEmpty('/404.html', '<h1>', signals.h1)
  if (title && !/(404|not found)/i.test(title)) fail('/404.html: title must identify the page as not found')
  if (h1 && !/(404|not found|does not exist|doesn['’]?t exist|cannot be found|isn['’]?t here)/i.test(h1)) {
    fail('/404.html: H1 must identify the page as not found')
  }
  const robots = matchingTags(
    html,
    'meta',
    (attrs) => (attrs.get('name') ?? '').toLowerCase() === 'robots',
  ).map((tag) => attributesForTag(tag).get('content') ?? '')
  if (!robots.some((value) => /(^|[,\s])noindex([,\s]|$)/i.test(value))) {
    fail('/404.html: expected a robots noindex directive')
  }
  if (signals.canonical.some((value) => value === `${SITE_ORIGIN}/`)) {
    fail('/404.html: must not canonicalize missing URLs to the homepage')
  }
  const redirectsPath = join(DIST_DIR, '_redirects')
  if (existsSync(redirectsPath)) {
    const wildcardHomeRewrite = readUtf8(redirectsPath)
      .split(/\r?\n/)
      .map((line) => line.replace(/#.*/, '').trim())
      .find((line) => /^\/\*\s+\/(?:index\.html)?(?:\s+200)?$/i.test(line))
    if (wildcardHomeRewrite) {
      fail(`dist/_redirects turns unknown URLs into the homepage instead of a 404: ${wildcardHomeRewrite}`)
    }
  }
}

function headerRuleBody(headers, routePattern) {
  const escaped = routePattern.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  return headers.match(new RegExp(`(?:^|\\n)${escaped}\\s*\\n((?:[ \\t]+[^\\n]+\\n?)*)`, 'i'))?.[1] ?? ''
}

function assertStaticDeploymentFiles() {
  const required = [
    '_headers',
    '_redirects',
    'robots.txt',
    'llms.txt',
    'favicon.svg',
    'favicon.ico',
    'favicon-48x48.png',
    'apple-touch-icon.png',
    'site.webmanifest',
    'web-app-manifest-192x192.png',
    'web-app-manifest-512x512.png',
    'og.png',
    'logo-512.png',
    'assets/og-veqtor.png',
    'assets/logo-512.png',
    'media/veqtor-demo-v0.1.2.mp4',
    'media/veqtor-demo-v0.1.2-poster.jpg',
    'media/veqtor-demo-v0.1.2-r2.en.vtt',
  ]
  for (const rel of required) {
    const path = join(DIST_DIR, rel)
    if (!existsSync(path) || !statSync(path).isFile() || statSync(path).size === 0) {
      fail(`required static deployment file is missing or empty: ${rel}`)
    }
  }

  const headersPath = join(DIST_DIR, '_headers')
  if (existsSync(headersPath)) {
    const headers = readUtf8(headersPath)
    const assetBlock = headerRuleBody(headers, '/assets/*')
    if (/\bimmutable\b/i.test(assetBlock)) {
      fail('dist/_headers gives immutable caching to stable, unhashed /assets/* filenames')
    }
    if (!/Content-Security-Policy:/i.test(headers)) fail('dist/_headers is missing Content-Security-Policy')

    const astroAssetBlock = headerRuleBody(headers, '/_astro/*')
    if (!/max-age=31536000/i.test(astroAssetBlock) || !/\bimmutable\b/i.test(astroAssetBlock)) {
      fail('dist/_headers must give one-year immutable caching to hashed /_astro/* assets')
    }

    for (const mediaPath of [
      '/media/veqtor-demo-v0.1.2.mp4',
      '/media/veqtor-demo-v0.1.2-poster.jpg',
      '/media/veqtor-demo-v0.1.2-r2.en.vtt',
    ]) {
      const mediaBlock = headerRuleBody(headers, mediaPath)
      if (!/max-age=31536000/i.test(mediaBlock) || !/\bimmutable\b/i.test(mediaBlock)) {
        fail(`dist/_headers must give one-year immutable caching to ${mediaPath}`)
      }
    }

    const setupBlock = headerRuleBody(headers, '/setup')
    if (!/Cache-Control:[^\n]*\bno-transform\b/i.test(setupBlock)) {
      fail('dist/_headers must protect /setup commands from Cloudflare Email Address Obfuscation with no-transform')
    }
  }

  const redirectsPath = join(DIST_DIR, '_redirects')
  if (existsSync(redirectsPath)) {
    const redirects = readUtf8(redirectsPath)
    const legacyDemoPaths = [
      '/og.svg',
      '/media/veqtor-demo.mp4',
      '/media/veqtor-demo-poster.jpg',
      '/media/veqtor-demo-v0.1.2.en.vtt',
      '/assets/veqtor-demo-hd.mp4',
      '/assets/veqtor-demo-poster-1200.jpg',
    ]
    for (const legacyPath of legacyDemoPaths) {
      if (!redirects.split(/\r?\n/).some((line) => line.trimStart().startsWith(`${legacyPath} `))) {
        fail(`dist/_redirects is missing the legacy demo redirect for ${legacyPath}`)
      }
    }
  }

  const robotsPath = join(DIST_DIR, 'robots.txt')
  if (existsSync(robotsPath) && !/Sitemap:\s*https:\/\/veqtor\.pro\/sitemap\.xml/i.test(readUtf8(robotsPath))) {
    fail('dist/robots.txt is missing the production sitemap URL')
  }

  const llmsPath = join(DIST_DIR, 'llms.txt')
  if (existsSync(llmsPath) && !/\[[^\]]+\]\(https:\/\/veqtor\.pro\//i.test(readUtf8(llmsPath))) {
    fail('dist/llms.txt is missing Markdown links to primary production pages')
  }

  const ogPath = join(DIST_DIR, 'assets', 'og-veqtor.png')
  if (existsSync(ogPath)) {
    const signature = readFileSync(ogPath).subarray(0, 8).toString('hex')
    if (signature !== '89504e470d0a1a0a') fail('assets/og-veqtor.png does not contain PNG data')
  }

  const sitemapPath = join(DIST_DIR, 'sitemap.xml')
  if (existsSync(sitemapPath)) {
    const sitemap = readUtf8(sitemapPath)
    if (!/xmlns:video="http:\/\/www\.google\.com\/schemas\/sitemap-video\/1\.1"/i.test(sitemap)) {
      fail('dist/sitemap.xml is missing the video sitemap namespace')
    }
    if (!/<video:content_loc>https:\/\/veqtor\.pro\/media\/veqtor-demo-v0\.1\.2\.mp4<\/video:content_loc>/i.test(sitemap)) {
      fail('dist/sitemap.xml is missing the versioned demo video entry')
    }
    if (!/<video:publication_date>2026-07-14T00:00:00\+03:00<\/video:publication_date>/i.test(sitemap)) {
      fail('dist/sitemap.xml is missing the demo video publication date')
    }
    const videoBlocks = [...sitemap.matchAll(/<url(?:\s[^>]*)?>[\s\S]*?<\/url>/gi)]
      .map((match) => match[0])
      .filter((block) => /<video:video>/i.test(block))
    if (videoBlocks.length !== 1 || !/<loc>https:\/\/veqtor\.pro\/demo<\/loc>/i.test(videoBlocks[0] ?? '')) {
      fail('dist/sitemap.xml must attach its single video entry only to /demo')
    }
    for (const match of sitemap.matchAll(/<lastmod>([^<]+)<\/lastmod>/gi)) {
      if (!/^\d{4}-\d{2}-\d{2}$/.test(match[1])) fail(`sitemap: invalid lastmod ${JSON.stringify(match[1])}`)
    }
  }
}

function assertSetupCommandIntegrity(pagesByRoute) {
  const html = pagesByRoute.get('/setup')?.html ?? ''
  const packageSpecifier = 'veqtor-mcp@0.1.2'
  const occurrences = html.split(packageSpecifier).length - 1
  if (occurrences !== 6) {
    fail(`/setup: expected six literal ${packageSpecifier} commands in built HTML, found ${occurrences}`)
  }
  for (const marker of ['/cdn-cgi/l/email-protection', 'data-cfemail', '__cf_email__', 'email-decode.min.js']) {
    if (html.includes(marker)) fail(`/setup: built HTML contains unexpected Cloudflare obfuscation marker ${marker}`)
  }
}

function assertSchemaContracts(schemasByRoute) {
  const nodesOfType = (route, type) => (schemasByRoute.get(route) ?? [])
    .filter((node) => node?.['@type'] === type)

  const homeSoftware = nodesOfType('/', 'SoftwareApplication')
  if (homeSoftware.length !== 1) fail(`/: expected one SoftwareApplication node, found ${homeSoftware.length}`)
  if (homeSoftware.some((node) => Object.hasOwn(node, 'codeRepository'))) {
    fail('/: SoftwareApplication must not use the SoftwareSourceCode-only codeRepository property')
  }
  const homeVideos = nodesOfType('/', 'VideoObject')
  if (homeVideos.length !== 0) fail(`/: homepage must not compete with /demo as a video watch page, found ${homeVideos.length} VideoObject node(s)`)

  const demoPages = nodesOfType('/demo', 'WebPage')
    .filter((node) => node?.['@id'] === 'https://veqtor.pro/demo#webpage')
  const demoVideos = nodesOfType('/demo', 'VideoObject')
    .filter((node) => node?.['@id'] === 'https://veqtor.pro/demo#video')
  if (demoPages.length !== 1) fail(`/demo: expected one watch-page WebPage node, found ${demoPages.length}`)
  if (demoVideos.length !== 1) fail(`/demo: expected one canonical VideoObject node, found ${demoVideos.length}`)
  if (demoPages[0]?.mainEntity?.['@id'] !== 'https://veqtor.pro/demo#video') {
    fail('/demo: WebPage mainEntity must reference the canonical video node')
  }
  if (demoVideos[0]?.mainEntityOfPage?.['@id'] !== 'https://veqtor.pro/demo#webpage') {
    fail('/demo: VideoObject mainEntityOfPage must reference the watch page')
  }
  if (demoVideos[0]?.contentUrl !== 'https://veqtor.pro/media/veqtor-demo-v0.1.2.mp4') {
    fail('/demo: VideoObject contentUrl does not match the versioned demo video')
  }

  const profilePages = nodesOfType('/author/ilya-shilov', 'ProfilePage')
  const people = nodesOfType('/author/ilya-shilov', 'Person')
    .filter((node) => node?.['@id'] === 'https://veqtor.pro/author/ilya-shilov#person')
  if (profilePages.length !== 1) fail(`/author/ilya-shilov: expected one ProfilePage node, found ${profilePages.length}`)
  if (people.length !== 1) fail(`/author/ilya-shilov: expected one canonical Person node, found ${people.length}`)
  if (profilePages[0]?.mainEntity?.['@id'] !== 'https://veqtor.pro/author/ilya-shilov#person') {
    fail('/author/ilya-shilov: ProfilePage mainEntity must reference the canonical Person')
  }

  for (const route of [
    '/product',
    '/how-it-works',
    '/security',
    '/demo',
    '/ai-contract-review',
    '/contract-redline-analysis',
    '/docx-track-changes-review',
  ]) {
    if (nodesOfType(route, 'BreadcrumbList').length !== 1) {
      fail(`${route}: expected one BreadcrumbList node matching the visible product hierarchy`)
    }
  }
}

function assertDemoWatchPageProminence(pagesByRoute) {
  const html = pagesByRoute.get('/demo')?.html ?? ''
  const heroTag = matchingTags(
    html,
    'section',
    (attrs) => (attrs.get('class') ?? '').split(/\s+/).includes('demo-hero'),
  )[0]
  const heroStart = heroTag ? html.indexOf(heroTag) : -1
  const heroEnd = heroStart >= 0 ? html.indexOf('</section>', heroStart) : -1
  const headingStart = heroStart >= 0 ? html.indexOf('<h1', heroStart) : -1
  const videoStart = heroStart >= 0 ? html.indexOf('<video', heroStart) : -1
  if (heroStart < 0 || heroEnd < 0 || headingStart < heroStart || videoStart < headingStart || videoStart > heroEnd) {
    fail('/demo: the canonical video must appear in the hero immediately after the primary heading')
  }
}

function assertPageLastmods(expectedLastmodsByRoute) {
  const sitemapPath = join(DIST_DIR, 'sitemap.xml')
  if (!existsSync(sitemapPath)) return
  const sitemap = readUtf8(sitemapPath)
  const blocksByRoute = new Map()
  for (const match of sitemap.matchAll(/<url(?:\s[^>]*)?>[\s\S]*?<\/url>/gi)) {
    const block = match[0]
    const location = block.match(/<loc>([^<]+)<\/loc>/i)?.[1]
    if (!location) continue
    const url = new URL(decodeEntities(location))
    blocksByRoute.set(normalizeRoute(url.pathname), block)
  }
  for (const [route, expectedDate] of expectedLastmodsByRoute) {
    const block = blocksByRoute.get(route) ?? ''
    const actualDate = block.match(/<lastmod>([^<]+)<\/lastmod>/i)?.[1]
    if (actualDate !== expectedDate) {
      fail(`sitemap: ${route} lastmod must be ${JSON.stringify(expectedDate)}, found ${JSON.stringify(actualDate)}`)
    }
  }
}

function main() {
  if (!existsSync(DIST_DIR)) {
    fail(`build output is missing: ${relative(process.cwd(), DIST_DIR) || DIST_DIR}`)
  }
  if (!existsSync(GUIDE_SOURCE_PATH)) {
    fail(`guide source is missing: ${relative(process.cwd(), GUIDE_SOURCE_PATH) || GUIDE_SOURCE_PATH}`)
  }
  if (failures.length) return

  assertStaticDeploymentFiles()

  const guideSource = JSON.parse(readUtf8(GUIDE_SOURCE_PATH))
  const approvedGuides = guideSource.guides.filter((guide) => guide.legalReviewStatus === 'approved')
  const draftGuides = guideSource.guides.filter((guide) => guide.legalReviewStatus !== 'approved')
  const topicRoutes = guideSource.clusters.map((cluster) => `/guides/topics/${cluster.id}`)
  const guideRoutes = approvedGuides.map((guide) => `/guides/${guide.slug}`)
  const bridgeChangedClusters = new Set(
    guideSource.clusters
      .filter((cluster) => cluster.id !== 'limitation-of-liability')
      .map((cluster) => cluster.id),
  )
  const bridgeChangedTopicRoutes = guideSource.clusters
    .filter((cluster) => bridgeChangedClusters.has(cluster.id))
    .map((cluster) => `/guides/topics/${cluster.id}`)
  const bridgeChangedGuideRoutes = approvedGuides
    .filter((guide) => bridgeChangedClusters.has(guide.cluster))
    .map((guide) => `/guides/${guide.slug}`)
  const demoBridgeTopicRoutes = guideSource.clusters
    .filter((cluster) => cluster.id === 'limitation-of-liability')
    .map((cluster) => `/guides/topics/${cluster.id}`)
  const demoBridgeGuideRoutes = approvedGuides
    .filter((guide) => guide.cluster === 'limitation-of-liability')
    .map((guide) => `/guides/${guide.slug}`)
  const legacyRoutes = [...STATIC_LEGACY_ROUTES, ...topicRoutes, ...guideRoutes]

  const pickEditorialMeta = (entry) => ({
    path: entry.path,
    title: entry.metaTitle,
    description: entry.metaDescription,
    twitterTitle: entry.twitterTitle,
  })
  const legacyEditorialSeo = {
    author: pickEditorialMeta(guideSource.author),
    guideIndex: pickEditorialMeta(guideSource.guideIndex),
    search: {
      path: '/guides/search',
      title: 'Search Veqtor Guides - Commercial Contract Review Library',
      description: 'Search Veqtor guides by commercial-contract topic, guide format, clause wording, and redline-review risk.',
      twitterTitle: 'Search Veqtor Guides',
    },
    topics: guideSource.clusters.map((cluster) => ({
      path: `/guides/topics/${cluster.id}`,
      title: `${cluster.label} Guides - Veqtor Contract Review Library`,
      description: cluster.metaDescription,
      twitterTitle: `${cluster.label} Guides`,
    })),
    guides: approvedGuides.map((guide) => ({
      path: `/guides/${guide.slug}`,
      title: guide.metaTitle,
      description: guide.metaDescription,
      twitterTitle: guide.twitterTitle ?? guide.metaTitle,
    })),
  }
  const expectedLegacySeoByRoute = new Map(
    [
      legacyEditorialSeo.author,
      legacyEditorialSeo.guideIndex,
      legacyEditorialSeo.search,
      ...legacyEditorialSeo.topics,
      ...legacyEditorialSeo.guides,
    ].map((entry) => [entry.path, entry]),
  )

  if (legacyRoutes.length !== 153) fail(`legacy manifest drift: expected 153 routes, found ${legacyRoutes.length}`)
  if (approvedGuides.length !== 122) fail(`approved guide count drift: expected 122, found ${approvedGuides.length}`)
  if (topicRoutes.length !== 18) fail(`guide topic count drift: expected 18, found ${topicRoutes.length}`)
  if (bridgeChangedTopicRoutes.length !== 17) fail(`bridge update drift: expected 17 topic routes, found ${bridgeChangedTopicRoutes.length}`)
  if (bridgeChangedGuideRoutes.length !== 115) fail(`bridge update drift: expected 115 guide routes, found ${bridgeChangedGuideRoutes.length}`)
  if (demoBridgeTopicRoutes.length !== 1) fail(`demo bridge drift: expected 1 topic route, found ${demoBridgeTopicRoutes.length}`)
  if (demoBridgeGuideRoutes.length !== 7) fail(`demo bridge drift: expected 7 guide routes, found ${demoBridgeGuideRoutes.length}`)
  if (new Set(legacyRoutes).size !== legacyRoutes.length) fail('legacy route manifest contains duplicates')
  if (sha256Json(legacyRoutes) !== LEGACY_ROUTE_MANIFEST_SHA256) {
    fail('legacy URL identity manifest changed; preserve old routes or add an explicit redirect plan before updating the pinned hash')
  }
  if (sha256Json(legacyEditorialSeo) !== EDITORIAL_SEO_SHA256) {
    fail('legacy guide SEO manifest changed; review title, description and social-title changes before updating the pinned hash')
  }

  const expectedRoutes = [...legacyRoutes, ...ALLOWED_NEW_ROUTES]
  const pagesByRoute = new Map()
  const schemasByRoute = new Map()
  const titles = new Map()
  const descriptions = new Map()
  const headings = new Map()

  for (const route of expectedRoutes) {
    const path = htmlOutputForRoute(route)
    if (!path) {
      fail(`${route}: missing HTML output (${routeCandidates(route).map((item) => relative(DIST_DIR, item)).join(' or ')})`)
      continue
    }
    const html = readUtf8(path)
    pagesByRoute.set(route, { path, html })
    schemasByRoute.set(route, structuredDataNodes(route, html))
    assertMainNavigationCurrent(route, html)
    const signals = pageSignals(html)
    const title = assertOneNonEmpty(route, '<title>', signals.title)
    const description = assertOneNonEmpty(route, 'meta description', signals.description)
    const twitterTitle = assertOneNonEmpty(route, 'twitter:title', signals.twitterTitle)
    const h1 = assertOneNonEmpty(route, '<h1>', signals.h1)
    const canonical = assertOneNonEmpty(route, 'canonical link', signals.canonical)
    if (canonical && canonical !== canonicalUrl(route)) {
      fail(`${route}: canonical is ${JSON.stringify(canonical)}, expected ${JSON.stringify(canonicalUrl(route))}`)
    }
    if (title) titles.set(route, title)
    if (description) descriptions.set(route, description)
    if (h1) headings.set(route, h1)

    const legacySeo = expectedLegacySeoByRoute.get(route)
    if (legacySeo) {
      if (title && title !== legacySeo.title) {
        fail(`${route}: legacy editorial title changed; expected ${JSON.stringify(legacySeo.title)}`)
      }
      if (description && description !== legacySeo.description) {
        fail(`${route}: legacy editorial meta description changed; expected ${JSON.stringify(legacySeo.description)}`)
      }
      if (twitterTitle && twitterTitle !== legacySeo.twitterTitle) {
        fail(`${route}: legacy editorial twitter:title changed; expected ${JSON.stringify(legacySeo.twitterTitle)}`)
      }
    }
  }

  const allowedHtmlRoutes = new Set([...expectedRoutes, '/404.html'])
  const emittedHtmlRoutes = walkFiles(DIST_DIR)
    .filter((path) => path.endsWith('.html'))
    .map(routeForHtmlOutput)
    .filter(Boolean)
  for (const route of emittedHtmlRoutes) {
    if (!allowedHtmlRoutes.has(route)) fail(`unexpected HTML route output: ${route}`)
  }
  if (new Set(emittedHtmlRoutes).size !== emittedHtmlRoutes.length) {
    fail('multiple HTML files resolve to the same public route')
  }

  assertUnique('title', titles)
  assertUnique('meta description', descriptions)
  assertUnique('H1', headings)

  const sitemap = sitemapRoutes()
  const expectedSitemapRoutes = [
    ...legacyRoutes.filter((route) => route !== '/guides/search'),
    ...ALLOWED_NEW_ROUTES,
  ]
  const actualSet = new Set(sitemap ?? [])
  if (sitemap) {
    const expectedSet = new Set(expectedSitemapRoutes)
    if (actualSet.size !== sitemap.length) fail('sitemap contains duplicate route entries')
    for (const route of expectedSet) if (!actualSet.has(route)) fail(`sitemap is missing ${route}`)
    for (const route of actualSet) if (!expectedSet.has(route)) fail(`sitemap contains unexpected route ${route}`)
    if (sitemap.length !== expectedSitemapRoutes.length) {
      fail(`sitemap route count is ${sitemap.length}, expected ${expectedSitemapRoutes.length}`)
    }
    if (actualSet.has('/guides/search')) fail('sitemap must exclude /guides/search')
  }

  for (const guide of draftGuides) {
    const route = `/guides/${guide.slug}`
    if (htmlOutputForRoute(route)) fail(`draft guide has public HTML output: ${route}`)
    if (sitemap && actualSet.has(route)) fail(`draft guide appears in sitemap: ${route}`)
  }

  assertInternalLinks(pagesByRoute)
  assertIndexablePagesHaveInboundLinks(pagesByRoute, expectedSitemapRoutes)
  assertPriorityPagesHaveContextualInboundLinks(pagesByRoute, expectedSitemapRoutes)
  assertRenderedBridgeCohorts(pagesByRoute, [
    {
      label: 'redline guide bridge cohort',
      routes: bridgeChangedGuideRoutes,
      expectedCount: 115,
      sectionClass: 'product-bridge',
      target: '/contract-redline-analysis',
    },
    {
      label: 'redline topic bridge cohort',
      routes: bridgeChangedTopicRoutes,
      expectedCount: 17,
      sectionClass: 'bridge',
      target: '/contract-redline-analysis',
    },
    {
      label: 'demo guide bridge cohort',
      routes: demoBridgeGuideRoutes,
      expectedCount: 7,
      sectionClass: 'product-bridge',
      target: '/demo',
    },
    {
      label: 'demo topic bridge cohort',
      routes: demoBridgeTopicRoutes,
      expectedCount: 1,
      sectionClass: 'bridge',
      target: '/demo',
    },
    {
      label: 'guide library product bridge',
      routes: ['/guides'],
      expectedCount: 1,
      sectionClass: 'bridge',
      target: '/product',
    },
    {
      label: 'author product bridge',
      routes: ['/author/ilya-shilov'],
      expectedCount: 1,
      sectionClass: 'bridge',
      target: '/product',
    },
  ])
  assertSetupCommandIntegrity(pagesByRoute)
  assertSchemaContracts(schemasByRoute)
  assertDemoWatchPageProminence(pagesByRoute)
  const expectedInventoryText = `${approvedGuides.length} guides across ${topicRoutes.length} topics`
  if (!normalizeText(pagesByRoute.get('/')?.html ?? '').includes(expectedInventoryText)) {
    fail(`/: guide-library summary must be dynamic and read ${JSON.stringify(expectedInventoryText)}`)
  }
  const expectedLastmodsByRoute = new Map([
    ...[
      '/',
      '/product',
      '/how-it-works',
      '/security',
      '/demo',
      '/ai-contract-review',
      '/contract-redline-analysis',
      '/docx-track-changes-review',
      '/author/ilya-shilov',
      '/guides',
      '/setup',
    ].map((route) => [route, LINK_ARCHITECTURE_LASTMOD]),
    ...guideSource.clusters.map((cluster) => [
      `/guides/topics/${cluster.id}`,
      cluster.id === 'limitation-of-liability' ? undefined : LINK_ARCHITECTURE_LASTMOD,
    ]),
    ...approvedGuides.map((guide) => {
      const editorialLastmod = guide.reviewedAt ?? guide.updated ?? guide.publishedAt
      return [
        `/guides/${guide.slug}`,
        latestLastmod(
          editorialLastmod,
          guide.cluster === 'limitation-of-liability' ? undefined : LINK_ARCHITECTURE_LASTMOD,
        ),
      ]
    }),
  ])
  assertPageLastmods(expectedLastmodsByRoute)
  assertReal404(pagesByRoute.get('/')?.html ?? '')

  if (!failures.length) {
    console.log(
      `Site check passed: ${legacyRoutes.length} legacy routes, ${ALLOWED_NEW_ROUTES.length} new routes, ` +
        `${expectedSitemapRoutes.length} sitemap URLs, ${approvedGuides.length} guides, real 404.`,
    )
  }
}

main()

if (failures.length) {
  console.error(`Site check failed with ${failures.length} issue${failures.length === 1 ? '' : 's'}:`)
  for (const failure of failures) console.error(`- ${failure}`)
  process.exitCode = 1
}
