import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { dirname, extname, join, relative, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url))
const WEBSITE_DIR = resolve(SCRIPT_DIR, '..')
const SRC_DIR = join(WEBSITE_DIR, 'src')
const PUBLIC_DIR = join(WEBSITE_DIR, 'public')
const DIST_DIR = join(WEBSITE_DIR, 'dist')

const SOURCE_EXTENSIONS = new Set(['.astro', '.html', '.js', '.jsx', '.json', '.md', '.mdx', '.mjs', '.ts', '.tsx', '.txt'])
const DIST_COPY_EXTENSIONS = new Set(['.html', '.json', '.txt', '.vtt', '.webmanifest', '.xml'])

// These are claims and calls to action from the retired hosted application. They are not
// safe descriptions of the local open-source MCP product.
const OBSOLETE_PATTERNS = [
  ['hosted public beta', /\bpublic beta\b/i],
  ['hosted no-install claim', /\bno install,? no plugin\b/i],
  ['hosted sign-up claim', /\bno sign[ -]?up,? no card,? no demo call\b/i],
  ['hosted retention claim', /\b(?:documents?|sessions?) (?:auto(?:matically)?[- ]?delete|are automatically deleted) after 24 hours\b/i],
  ['hosted inactivity retention', /\b24[- ]hour(?:s)? (?:of )?inactivity\b/i],
  ['hosted training claim', /\b(?:uploaded )?documents? (?:are|is) not used to train models\b/i],
  ['hosted upload CTA', /\bupload (?:a|the|your) (?:contract|mark[ -]?up|word draft|\.docx)\b/i],
  ['hosted side-selection flow', /\b(?:pick|choose) (?:the|your) side(?: you represent)?\b/i],
  ['hosted review queue', /\brisk[- ]ranked (?:change |redline |issue )?queue\b/i],
  ['hosted inspector UI', /\bselected[- ]change inspector\b/i],
  ['hosted decision taxonomy', /\baccept, reject, counter, or ask[- ]client\b/i],
  ['hosted file limit', /\bpublic beta file limit\b/i],
  ['hosted upload authority', /\bauthority to upload\b/i],
  ['hosted anonymous session', /\banonymous session\b/i],
  ['hosted browser storage', /\bbrowser storage\b/i],
  ['hosted product category', /\bAI contract review workspace\b/i],
  ['hosted export wording', /\bworking[- ]file export\b/i],
  ['hosted Web application schema', /["']operatingSystem["']\s*:\s*["']Web["']/i],
  ['retired in-page upload anchor', /(?:href\s*=\s*["'])?\/#try\b/i],
  ['retired demo asset', /\bveqtor-demo-hd\.mp4\b/i],
  ['retired demo duration', /\b(?:two[- ]minute|2:15)\b/i],
  ['retired demo workflow', /\bcounter[- ]redline export\b/i],
]

// Timed captions are a primary accessible artifact. Keep these guards narrow to the
// three claims corrected after the v0.1.2 recording was produced.
const STALE_DEMO_CAPTION_PATTERNS = [
  ['unanchored quotation verification', /\bverif(?:y|ies|ied) quotations?\b/i],
  ['whole-file quotation verification', /\bchecks? (?:the )?quotation against (?:the )?(?:source )?file\b/i],
  ['saved-file recheck', /\bre[- ]?checks? (?:the )?saved file\b/i],
  ['generic one-command install', /\b(?:installs? with (?:a single|one) command|one[- ]command install(?:ation)?)\b/i],
]

// Technical precision belongs in a visibly marked technical layer, docs, setup, or limitations.
// Mark technical UI blocks with data-copy-layer="technical" so this check can distinguish them
// from the default plain-language story.
const FORBIDDEN_PLAIN_PATTERNS = [
  ['DOCX file extension', /(?:\.docx|\bDOCX\b)/i],
  ['provenance jargon', /\bprovenance\b/i],
  ['tamper-evident jargon', /\btamper[- ]evident\b/i],
  ['fingerprint jargon', /\bre[- ]checkable fingerprints?\b/i],
  ['hash jargon', /\bhash(?:es|ed|ing)?\b/i],
  ['stdio transport', /\bstdio\b/i],
  ['JSON format', /\bJSON\b/],
  ['preflight jargon', /\bpreflight\b/i],
  ['dry-run jargon', /\bdry[- ]run\b/i],
  ['fail-closed jargon', /\bfail[- ]closed\b/i],
  ['atomic batch jargon', /\batomic (?:edit )?batch\b/i],
  ['anchored-change jargon', /\banchored (?:change|edit|quote|wording)s?\b/i],
  ['bounded-context jargon', /\bbounded (?:context|surrounding text)\b/i],
  ['semantic-lineage jargon', /\bsemantic (?:clause )?lineage\b/i],
  ['structured-facts jargon', /\bstructured facts\b/i],
  ['supported-pipeline jargon', /\bsupported pipeline\b/i],
  ['revision-category jargon', /\bunsupported revision categor(?:y|ies)\b/i],
  ['change-unit jargon', /\beditable change units?\b/i],
  ['normalization jargon', /\blimited normalization\b/i],
  ['filename-order jargon', /\bfilename order\b/i],
  ['MCP tool name', /\b(?:list_rounds|extract_redlines|verify_quote|preflight_edits|apply_edits|export_decision_record)\b/],
]

const PLAIN_MARKETING_ROUTES = new Set([
  '/',
  '/product',
  '/how-it-works',
  '/security',
  '/demo',
  '/ai-contract-review',
  '/contract-redline-analysis',
  '/docx-track-changes-review',
  '/veqtor-vs-claude-for-word',
  '/terms',
  '/privacy',
])

const failures = []

function walkFiles(root) {
  if (!existsSync(root)) return []
  const files = []
  for (const entry of readdirSync(root, { withFileTypes: true })) {
    const path = join(root, entry.name)
    if (entry.isDirectory()) files.push(...walkFiles(path))
    else if (entry.isFile()) files.push(path)
  }
  return files
}

function normalizedRelative(path) {
  return relative(WEBSITE_DIR, path).split(sep).join('/')
}

function sourceShouldBeScanned(path) {
  const rel = normalizedRelative(path)
  if (!SOURCE_EXTENSIONS.has(extname(path).toLowerCase())) return false
  if (rel === 'scripts/check-copy.mjs') return false
  // The canonical guide data intentionally retains source material and is filtered/transformed
  // during rendering. The built, user-visible pages are checked below.
  if (rel === 'src/data/guides-source.json' || rel === 'src/data/guides.json') return false
  return true
}

function lineAndSnippet(text, index) {
  const line = text.slice(0, index).split('\n').length
  const start = Math.max(0, text.lastIndexOf('\n', index - 1) + 1)
  const endAt = text.indexOf('\n', index)
  const end = endAt < 0 ? text.length : endAt
  const snippet = text.slice(start, end).replace(/\s+/g, ' ').trim().slice(0, 180)
  return { line, snippet }
}

function scanPatterns(label, text, patterns, withLines = false) {
  for (const [description, pattern] of patterns) {
    const flags = pattern.flags.includes('g') ? pattern.flags : `${pattern.flags}g`
    const matcher = new RegExp(pattern.source, flags)
    for (const match of text.matchAll(matcher)) {
      const location = withLines ? lineAndSnippet(text, match.index ?? 0) : null
      failures.push(
        withLines
          ? `${label}:${location.line}: ${description}: ${JSON.stringify(location.snippet)}`
          : `${label}: ${description}: ${JSON.stringify(match[0])}`,
      )
    }
  }
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

function stripBlocks(html, tagNames) {
  let result = html
  for (const tag of tagNames) {
    result = result.replace(new RegExp(`<${tag}\\b[^>]*>[\\s\\S]*?<\\/${tag}>`, 'gi'), ' ')
  }
  return result
}

function stripMarkedTechnicalBlocks(html) {
  const marker = /(?:data-copy-layer\s*=\s*["']technical["']|data-technical(?:\s|=|>)|class\s*=\s*["'][^"']*(?:technical|tech-detail|tech-note|mono)[^"']*["'])/i
  const voidTags = new Set(['area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'])
  const tokenPattern = /<!--(?:[\s\S]*?)-->|<\/?([a-z][a-z0-9:-]*)\b[^>]*>/gi
  const stack = []
  let cursor = 0
  let output = ''

  for (const match of html.matchAll(tokenPattern)) {
    const token = match[0]
    const tag = (match[1] ?? '').toLowerCase()
    const closing = token.startsWith('</')
    const selfClosing = token.endsWith('/>') || voidTags.has(tag)

    if (stack.length) {
      if (closing) {
        if (stack.at(-1) === tag) stack.pop()
      } else if (!selfClosing && !token.startsWith('<!--')) {
        stack.push(tag)
      }
      cursor = (match.index ?? 0) + token.length
      continue
    }

    if (!closing && marker.test(token)) {
      output += html.slice(cursor, match.index ?? 0)
      cursor = (match.index ?? 0) + token.length
      if (!selfClosing) stack.push(tag)
    }
  }

  return `${output}${html.slice(cursor)}`
}

function visibleText(html, { omitTechnical = false } = {}) {
  const body = html.match(/<body\b[^>]*>([\s\S]*?)<\/body>/i)?.[1] ?? html
  let text = stripBlocks(body, ['script', 'style', 'template', 'noscript', 'svg'])
  if (omitTechnical) {
    text = stripMarkedTechnicalBlocks(text)
    text = stripBlocks(text, ['code', 'pre', 'kbd', 'samp'])
  }
  return decodeEntities(text.replace(/<!--([\s\S]*?)-->/g, ' ').replace(/<[^>]+>/g, ' '))
    .replace(/\s+/g, ' ')
    .trim()
}

function seoText(html) {
  const values = []
  for (const match of html.matchAll(/<title\b[^>]*>([\s\S]*?)<\/title>/gi)) values.push(match[1])
  for (const match of html.matchAll(/<meta\b[^>]*>/gi)) {
    const tag = match[0]
    if (!/(?:name|property)\s*=\s*["'](?:description|og:title|og:description|twitter:title|twitter:description)["']/i.test(tag)) continue
    const content = tag.match(/content\s*=\s*(?:"([^"]*)"|'([^']*)')/i)
    if (content) values.push(content[1] ?? content[2] ?? '')
  }
  for (const match of html.matchAll(/<script\b[^>]*type\s*=\s*["']application\/ld\+json["'][^>]*>([\s\S]*?)<\/script>/gi)) {
    values.push(match[1])
  }
  return decodeEntities(values.join(' ')).replace(/\s+/g, ' ').trim()
}

function routeForHtml(path) {
  const rel = relative(DIST_DIR, path).split(sep).join('/')
  if (rel === 'index.html') return '/'
  if (rel === '404.html') return '/404.html'
  if (rel.endsWith('/index.html')) return `/${rel.slice(0, -'/index.html'.length)}`
  if (rel.endsWith('.html')) return `/${rel.slice(0, -'.html'.length)}`
  return null
}

function scanSource() {
  for (const path of walkFiles(SRC_DIR).filter(sourceShouldBeScanned)) {
    scanPatterns(normalizedRelative(path), readFileSync(path, 'utf8'), OBSOLETE_PATTERNS, true)
  }
}

function scanPublicCaptions() {
  for (const path of walkFiles(PUBLIC_DIR)) {
    if (extname(path).toLowerCase() !== '.vtt') continue
    const rel = normalizedRelative(path)
    const raw = readFileSync(path, 'utf8')
    scanPatterns(rel, raw, OBSOLETE_PATTERNS, true)
    scanPatterns(rel, raw, STALE_DEMO_CAPTION_PATTERNS, true)
  }
}

function scanDist() {
  if (!existsSync(DIST_DIR)) {
    failures.push('dist is missing; run the website build before check-copy')
    return
  }
  for (const path of walkFiles(DIST_DIR)) {
    if (!DIST_COPY_EXTENSIONS.has(extname(path).toLowerCase())) continue
    const rel = normalizedRelative(path)
    const raw = readFileSync(path, 'utf8')
    if (extname(path).toLowerCase() === '.vtt') {
      scanPatterns(rel, raw, OBSOLETE_PATTERNS, true)
      scanPatterns(rel, raw, STALE_DEMO_CAPTION_PATTERNS, true)
      continue
    }
    if (extname(path).toLowerCase() !== '.html') {
      scanPatterns(rel, raw, OBSOLETE_PATTERNS)
      continue
    }

    const renderedCopy = `${seoText(raw)} ${visibleText(raw)}`
    scanPatterns(rel, renderedCopy, OBSOLETE_PATTERNS)

    const route = routeForHtml(path)
    if (route && PLAIN_MARKETING_ROUTES.has(route)) {
      const plainCopy = visibleText(raw, { omitTechnical: true })
      scanPatterns(`${rel} (plain layer)`, plainCopy, FORBIDDEN_PLAIN_PATTERNS)
    }
  }
}

scanSource()
scanPublicCaptions()
scanDist()

if (failures.length) {
  console.error(`Copy check failed with ${failures.length} issue${failures.length === 1 ? '' : 's'}:`)
  for (const failure of failures) console.error(`- ${failure}`)
  process.exitCode = 1
} else {
  console.log('Copy check passed: no retired hosted-product or stale demo-caption claims and no forbidden jargon in plain marketing copy.')
}
