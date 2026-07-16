import type { APIRoute } from 'astro'
import { GUIDES, TOPICS } from '../lib/guides'

const SITE = 'https://veqtor.pro'
const STATIC_ROUTES = [
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
  '/setup',
  '/docs',
  '/limitations',
]

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
  const entries: Array<{ path: string; lastmod?: string }> = [
    ...STATIC_ROUTES.map((path) => ({ path })),
    ...TOPICS.map((topic) => ({ path: topic.path })),
    ...GUIDES.map((guide) => ({ path: guide.path, lastmod: guide.modifiedAt })),
  ]

  const urls = entries.map(({ path, lastmod }) => [
    '  <url>',
    `    <loc>${escapeXml(absolute(path))}</loc>`,
    lastmod ? `    <lastmod>${escapeXml(lastmod)}</lastmod>` : null,
    '  </url>',
  ].filter(Boolean).join('\n')).join('\n')

  return new Response(
    `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n${urls}\n</urlset>\n`,
    { headers: { 'Content-Type': 'application/xml; charset=utf-8' } },
  )
}
