import type { APIRoute } from 'astro'
import { GUIDES, TOPICS } from '../lib/guides'

const SITE = 'https://veqtor.pro'
const DEMO_VIDEO = {
  thumbnail: `${SITE}/media/veqtor-demo-v0.1.2-poster.jpg`,
  title: 'Veqtor demo: review Word redlines with Claude',
  description: 'See Claude compare contract drafts with Veqtor and create a separate Word document with proposed tracked changes.',
  content: `${SITE}/media/veqtor-demo-v0.1.2.mp4`,
  duration: 104,
}
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
    ...STATIC_ROUTES.map((path) => ({ path, video: path === '/demo' ? DEMO_VIDEO : undefined })),
    ...TOPICS.map((topic) => ({ path: topic.path })),
    ...GUIDES.map((guide) => ({ path: guide.path, lastmod: guide.modifiedAt })),
  ]

  const urls = entries.map(({ path, lastmod, video }) => [
    '  <url>',
    `    <loc>${escapeXml(absolute(path))}</loc>`,
    lastmod ? `    <lastmod>${escapeXml(lastmod)}</lastmod>` : null,
    ...(video ? [
      '    <video:video>',
      `      <video:thumbnail_loc>${escapeXml(video.thumbnail)}</video:thumbnail_loc>`,
      `      <video:title>${escapeXml(video.title)}</video:title>`,
      `      <video:description>${escapeXml(video.description)}</video:description>`,
      `      <video:content_loc>${escapeXml(video.content)}</video:content_loc>`,
      `      <video:duration>${video.duration}</video:duration>`,
      '    </video:video>',
    ] : []),
    '  </url>',
  ].filter(Boolean).join('\n')).join('\n')

  return new Response(
    `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:video="http://www.google.com/schemas/sitemap-video/1.1">\n${urls}\n</urlset>\n`,
    { headers: { 'Content-Type': 'application/xml; charset=utf-8' } },
  )
}
