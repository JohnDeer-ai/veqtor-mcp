import type { APIRoute } from 'astro'
import { DEMO_VIDEO } from '../lib/demo-video'
import { GUIDES, TOPICS } from '../lib/guides'
import { LINK_ARCHITECTURE_LASTMOD, latestLastmod } from '../lib/sitemap-lastmod.mjs'

const SITE = 'https://veqtor.pro'
const UNCHANGED_TOPIC_BRIDGE = 'limitation-of-liability'
const STATIC_ROUTES = [
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
  '/author/ilya-shilov',
  '/guides',
  '/setup',
  '/docs',
  '/limitations',
]
const STATIC_ROUTE_LASTMOD = new Map<string, string>([
  ['/', '2026-07-23'],
  ['/product', '2026-07-23'],
  ['/how-it-works', '2026-07-23'],
  ['/security', '2026-07-23'],
  ['/demo', '2026-07-23'],
  ['/ai-contract-review', '2026-07-23'],
  ['/contract-redline-analysis', '2026-07-23'],
  ['/docx-track-changes-review', '2026-07-23'],
  ['/author/ilya-shilov', '2026-07-23'],
  ['/guides', '2026-07-23'],
  ['/setup', '2026-07-23'],
])

function escapeXml(value: string): string {
  return value
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&apos;')
}

function absolute(path: string): string {
  return path === '/' ? `${SITE}/` : `${SITE}${path}`
}

export const GET: APIRoute = () => {
  const entries: Array<{ path: string; lastmod?: string; video?: typeof DEMO_VIDEO }> = [
    ...STATIC_ROUTES.map((path) => ({
      path,
      lastmod: STATIC_ROUTE_LASTMOD.get(path),
      video: path === DEMO_VIDEO.pagePath ? DEMO_VIDEO : undefined,
    })),
    ...TOPICS.map((topic) => ({
      path: topic.path,
      lastmod: topic.id === UNCHANGED_TOPIC_BRIDGE ? undefined : LINK_ARCHITECTURE_LASTMOD,
    })),
    ...GUIDES.map((guide) => ({
      path: guide.path,
      lastmod: latestLastmod(
        guide.modifiedAt,
        guide.cluster === UNCHANGED_TOPIC_BRIDGE ? undefined : LINK_ARCHITECTURE_LASTMOD,
      ),
    })),
  ]

  const urls = entries.map(({ path, lastmod, video }) => [
    '  <url>',
    `    <loc>${escapeXml(absolute(path))}</loc>`,
    lastmod ? `    <lastmod>${escapeXml(lastmod)}</lastmod>` : null,
    ...(video ? [
      '    <video:video>',
      `      <video:thumbnail_loc>${escapeXml(video.thumbnailUrl)}</video:thumbnail_loc>`,
      `      <video:title>${escapeXml(video.name)}</video:title>`,
      `      <video:description>${escapeXml(video.description)}</video:description>`,
      `      <video:content_loc>${escapeXml(video.contentUrl)}</video:content_loc>`,
      `      <video:duration>${video.durationSeconds}</video:duration>`,
      `      <video:publication_date>${escapeXml(video.uploadDate)}</video:publication_date>`,
      '    </video:video>',
    ] : []),
    '  </url>',
  ].filter(Boolean).join('\n')).join('\n')

  return new Response(
    `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:video="http://www.google.com/schemas/sitemap-video/1.1">\n${urls}\n</urlset>\n`,
    { headers: { 'Content-Type': 'application/xml; charset=utf-8' } },
  )
}
