import guideSourceJson from '../data/guides-source.json'

export type GuideKind = 'pillar' | 'spoke'
export type LegalReviewStatus = 'approved' | 'owner_review_required'

// A body entry is a paragraph string (the historical format) or a typed block.
// Inline markup inside any string is limited to [label](href), **bold**, *italic*.
export type GuideBlock =
  | string
  | { type: 'list'; ordered?: boolean; items: string[] }
  | { type: 'quote'; body: string[] }
  | { type: 'table'; header: string[]; rows: string[][] }

export interface GuideSection {
  title: string
  body: GuideBlock[]
}

interface GuideListing {
  label: string
  title: string
  body: string
  homeTitle?: string
  homeBody?: string
  authorBody?: string
}

interface RelatedGuideReference {
  slug?: string
  href?: string
  label?: string
  title?: string
  body?: string
}

interface SourceGuide {
  slug: string
  cluster: string
  kind: GuideKind
  targetQuery: string
  metaTitle: string
  metaDescription: string
  twitterTitle?: string
  breadcrumb: string
  eyebrow: string
  h1: string
  lede: string
  fallbackIntro?: string
  updated: string
  publishedAt: string
  sourceIssue: string
  legalReviewStatus: LegalReviewStatus
  reviewedAt?: string
  listing: GuideListing
  sections: GuideSection[]
  shellSections: GuideSection[]
  checklist: string[]
  sources?: string[]
  related: RelatedGuideReference[]
  productBridge?: GuideProductBridge
}

interface SourceCluster {
  id: string
  label: string
  metaDescription: string
  pillarSlug: string
  spokeSlugs: string[]
  productBridge: GuideProductBridge
}

export interface GuideProductBridge {
  href: string
  label: string
  heading: string
  title: string
  body: string
}

interface AuthorContent {
  path: string
  schemaId: string
  imageUrl: string
  metaTitle: string
  metaDescription: string
  twitterTitle: string
  breadcrumb: string
  eyebrow: string
  h1: string
  lede: string
  fallbackIntro: string
  facts: string[]
  sections: GuideSection[]
  expertise: string[]
  sameAs: string[]
  schema: Record<string, unknown>
}

interface GuideIndexContent {
  path: string
  metaTitle: string
  metaDescription: string
  twitterTitle: string
  breadcrumb: string
  eyebrow: string
  h1: string
  lede: string
  sideTitle: string
  sideBody: string
}

interface GuideSource {
  siteUrl: string
  author: AuthorContent
  guideIndex: GuideIndexContent
  clusters: SourceCluster[]
  guides: SourceGuide[]
}

export interface Guide {
  slug: string
  path: string
  topicPath: string
  cluster: string
  kind: GuideKind
  targetQuery: string
  metaTitle: string
  metaDescription: string
  twitterTitle: string
  breadcrumb: string
  eyebrow: string
  h1: string
  lede: string
  updated: string
  publishedAt: string
  modifiedAt: string
  sourceIssue: string
  listing: GuideListing
  sections: GuideSection[]
  checklist: string[]
  sources: string[]
  relatedSlugs: string[]
  productBridge?: GuideProductBridge
  readingTime: number
  searchText: string
}

export interface GuideStage {
  id: string
  label: string
  description: string
  clusterIds: string[]
}

export interface GuideTopic {
  id: string
  path: string
  label: string
  pillar: Guide
  spokes: Guide[]
  guides: Guide[]
  description: string
  twitterTitle: string
  productBridge: GuideProductBridge
  stage: GuideStage
}

export interface LearningPath {
  id: string
  label: string
  audience: string
  description: string
  steps: Array<{ slug: string; why: string }>
}

const source = guideSourceJson as unknown as GuideSource

export const SITE_URL = 'https://veqtor.pro'
export const AUTHOR = source.author
export const GUIDE_INDEX = source.guideIndex

export const GUIDE_STAGES: GuideStage[] = [
  {
    id: 'formation',
    label: 'Formation and identity',
    description: 'Who is bound, how the contract forms, and what the words are allowed to mean.',
    clusterIds: [
      'contract-formation',
      'parties-to-a-contract',
      'definitions-and-recitals',
      'ai-agent-contracting',
    ],
  },
  {
    id: 'performance',
    label: 'Scope and performance',
    description: 'What must be delivered, when obligations mature, and how acceptance is tested.',
    clusterIds: ['scope-of-work', 'when-obligations-become-due'],
  },
  {
    id: 'risk-allocation',
    label: 'Allocating risk',
    description: 'Caps, warranties, indemnities, agreed damages, and the mechanics that decide recovery.',
    clusterIds: [
      'limitation-of-liability',
      'warranties-and-representations',
      'indemnities',
      'liquidated-damages',
    ],
  },
  {
    id: 'exit-and-enforcement',
    label: 'Exit and enforcement',
    description: 'Termination, disputes, and changed circumstances that decide leverage.',
    clusterIds: [
      'terminating-a-contract',
      'dispute-resolution-clauses',
      'force-majeure-frustration-and-hardship',
    ],
  },
  {
    id: 'assets-and-control',
    label: 'Assets and control',
    description: 'IP, assignment, confidentiality, and control points that survive the immediate deal.',
    clusterIds: [
      'intellectual-property-in-commercial-contracts',
      'assignment-novation-and-change-of-control',
      'confidentiality-and-ndas',
    ],
  },
  {
    id: 'operating-risk',
    label: 'Operating risk',
    description: 'Sanctions, export controls, and boilerplate clauses that quietly move risk.',
    clusterIds: ['sanctions-and-export-controls', 'boilerplate-clauses'],
  },
]

export const GUIDE_LEARNING_PATHS: LearningPath[] = [
  {
    id: 'first-review',
    label: 'First full contract review',
    audience: 'For a general commercial review',
    description: 'Start with enforceability, then move through parties, scope, risk allocation, and disputes.',
    steps: [
      { slug: 'contract-formation', why: 'Confirm when the deal becomes binding.' },
      { slug: 'parties-to-a-contract', why: 'Check the legal entities and authority.' },
      { slug: 'scope-of-work', why: 'Define what is actually being bought and sold.' },
      { slug: 'limitation-of-liability', why: 'Find the economic ceiling before debating wording.' },
      { slug: 'dispute-resolution-clauses', why: 'Know where and how a claim would be enforced.' },
    ],
  },
  {
    id: 'risk-allocation',
    label: 'Risk allocation clauses',
    audience: 'For liability-heavy negotiations',
    description: 'Read the clauses that decide what can be recovered, excluded, capped, or paid without proof.',
    steps: [
      { slug: 'limitation-of-liability', why: 'Map caps, exclusions, and mandatory carve-outs.' },
      { slug: 'warranties-and-representations', why: 'Separate statements, remedies, and reliance.' },
      { slug: 'indemnities', why: 'Test whether the indemnity actually improves recovery.' },
      { slug: 'liquidated-damages', why: 'Check whether pre-agreed sums hold up.' },
      { slug: 'terminating-a-contract', why: 'Tie the remedy to the right exit route.' },
    ],
  },
  {
    id: 'technology-deals',
    label: 'Technology and IP deals',
    audience: 'For software, licensing, data, and AI work',
    description: 'Follow ownership, data, confidentiality, change-of-control, and regulatory transfer risk.',
    steps: [
      { slug: 'definitions-and-recitals', why: 'Lock down defined terms before reading obligations.' },
      { slug: 'intellectual-property-in-commercial-contracts', why: 'Decide who owns the output.' },
      { slug: 'confidentiality-and-ndas', why: 'Protect data, know-how, and review-room information.' },
      { slug: 'assignment-novation-and-change-of-control', why: 'Control transfers and corporate changes.' },
      { slug: 'sanctions-and-export-controls', why: 'Catch payment and export restrictions before signing.' },
    ],
  },
]

function invariant(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(`[guides] ${message}`)
}

function assertUnique(values: string[], label: string): void {
  const seen = new Set<string>()
  for (const value of values) {
    invariant(!seen.has(value), `Duplicate ${label}: ${value}`)
    seen.add(value)
  }
}

assertUnique(source.guides.map((guide) => guide.slug), 'guide slug')
assertUnique(source.clusters.map((cluster) => cluster.id), 'topic id')

const sourceClusterIds = new Set(source.clusters.map((cluster) => cluster.id))
const approvedSourceGuides = source.guides.filter(
  (guide) => guide.legalReviewStatus === 'approved',
)
const approvedSlugs = new Set(approvedSourceGuides.map((guide) => guide.slug))

for (const guide of approvedSourceGuides) {
  invariant(sourceClusterIds.has(guide.cluster), `Unknown topic '${guide.cluster}' on ${guide.slug}`)
  invariant(guide.sections.length > 0, `No full sections on ${guide.slug}`)
  invariant(guide.checklist.length > 0, `No checklist on ${guide.slug}`)
  for (const related of guide.related) {
    if (related.slug && approvedSlugs.has(related.slug)) {
      invariant(related.slug !== guide.slug, `Guide ${guide.slug} links to itself`)
    }
  }
}

function wordCount(value: string): number {
  return value.trim().split(/\s+/).filter(Boolean).length
}

function blockText(block: GuideBlock): string {
  if (typeof block === 'string') return block
  if (block.type === 'list') return block.items.join(' ')
  if (block.type === 'quote') return block.body.join(' ')
  return [...block.header, ...block.rows.flat()].join(' ')
}

// Reduces inline markup to its visible text for counting and search indexing.
function inlinePlainText(value: string): string {
  return value.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, '$1').replaceAll('*', '')
}

export { renderInline } from './render-inline.mjs'

function readingTimeForGuide(guide: SourceGuide): number {
  const text = [
    guide.h1,
    guide.lede,
    ...guide.sections.flatMap((section) => [section.title, ...section.body.map(blockText)]),
    ...guide.checklist,
    ...(guide.sources ?? []),
  ].map(inlinePlainText).join(' ')
  return Math.max(4, Math.ceil(wordCount(text) / 220))
}

function searchTextForGuide(guide: SourceGuide): string {
  return [
    guide.h1,
    guide.lede,
    guide.breadcrumb,
    guide.eyebrow,
    guide.targetQuery,
    guide.metaDescription,
    ...guide.sections.flatMap((section) => [section.title, ...section.body.map(blockText)]),
    ...guide.checklist,
    ...(guide.sources ?? []),
  ].map(inlinePlainText).join(' ').toLocaleLowerCase('en')
}

export const GUIDES: Guide[] = approvedSourceGuides.map((guide) => ({
  slug: guide.slug,
  path: `/guides/${guide.slug}`,
  topicPath: `/guides/topics/${guide.cluster}`,
  cluster: guide.cluster,
  kind: guide.kind,
  targetQuery: guide.targetQuery,
  metaTitle: guide.metaTitle,
  metaDescription: guide.metaDescription,
  twitterTitle: guide.twitterTitle ?? guide.metaTitle,
  breadcrumb: guide.breadcrumb,
  eyebrow: guide.eyebrow,
  h1: guide.h1,
  lede: guide.lede,
  updated: guide.updated,
  publishedAt: guide.publishedAt,
  modifiedAt: guide.reviewedAt ?? guide.updated ?? guide.publishedAt,
  sourceIssue: guide.sourceIssue,
  listing: guide.listing,
  // Full editorial sections are the page body. shellSections are deliberately not imported.
  sections: guide.sections,
  checklist: guide.checklist,
  sources: guide.sources ?? [],
  // Old href-based product CTAs are deliberately stripped. Only approved guide links survive.
  relatedSlugs: guide.related
    .flatMap((related) => related.slug ? [related.slug] : [])
    .filter((slug) => approvedSlugs.has(slug)),
  productBridge: guide.productBridge,
  readingTime: readingTimeForGuide(guide),
  searchText: searchTextForGuide(guide),
}))

export const GUIDE_BY_SLUG = new Map(GUIDES.map((guide) => [guide.slug, guide]))

const stageByClusterId = new Map(
  GUIDE_STAGES.flatMap((stage) => stage.clusterIds.map((id) => [id, stage] as const)),
)

export const TOPICS: GuideTopic[] = source.clusters.flatMap((cluster) => {
  const pillar = GUIDE_BY_SLUG.get(cluster.pillarSlug)
  const spokes = cluster.spokeSlugs
    .filter((slug) => approvedSlugs.has(slug))
    .map((slug) => GUIDE_BY_SLUG.get(slug))
    .filter((guide): guide is Guide => Boolean(guide))
  const guides = [pillar, ...spokes].filter((guide): guide is Guide => Boolean(guide))
  if (!guides.length) return []
  invariant(pillar, `Published topic '${cluster.id}' has no approved pillar guide`)
  const stage = stageByClusterId.get(cluster.id) ?? GUIDE_STAGES[0]
  return [{
    id: cluster.id,
    path: `/guides/topics/${cluster.id}`,
    label: cluster.label,
    pillar,
    spokes,
    guides,
    description: cluster.metaDescription,
    twitterTitle: `${cluster.label} Guides`,
    productBridge: cluster.productBridge,
    stage,
  }]
})

export const TOPIC_BY_ID = new Map(TOPICS.map((topic) => [topic.id, topic]))

export const MCP_GUIDE_BRIDGE = {
  href: '/product',
  label: 'Product overview',
  title: 'See what Veqtor can do',
  body: 'See how Veqtor lets Claude compare Word negotiation drafts, check exact wording, and create a separate document with proposed tracked changes.',
} as const

export function productBridgeForGuide(guide: Guide): GuideProductBridge {
  return guide.productBridge ?? topicForGuide(guide).productBridge
}

export function canonicalUrl(path: string): string {
  return path === '/' ? `${SITE_URL}/` : `${SITE_URL}${path}`
}

export function guideBySlug(slug: string): Guide | undefined {
  return GUIDE_BY_SLUG.get(slug)
}

export function topicById(id: string): GuideTopic | undefined {
  return TOPIC_BY_ID.get(id)
}

export function topicForGuide(guide: Guide): GuideTopic {
  const topic = TOPIC_BY_ID.get(guide.cluster)
  invariant(topic, `No published topic for ${guide.slug}`)
  return topic
}

export function relatedGuides(guide: Guide): Guide[] {
  return guide.relatedSlugs
    .map((slug) => GUIDE_BY_SLUG.get(slug))
    .filter((related): related is Guide => Boolean(related))
}

export function guideAnchorId(title: string): string {
  return title
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
}

export function formatGuideDate(isoDate: string): string {
  return new Intl.DateTimeFormat('en-GB', {
    day: 'numeric',
    month: 'long',
    year: 'numeric',
    timeZone: 'UTC',
  }).format(new Date(`${isoDate}T00:00:00Z`))
}

export function indexStructuredData(): Record<string, unknown>[] {
  return [
    AUTHOR.schema,
    {
      '@type': 'CollectionPage',
      '@id': `${canonicalUrl('/guides')}#webpage`,
      url: canonicalUrl('/guides'),
      name: GUIDE_INDEX.metaTitle,
      description: GUIDE_INDEX.metaDescription,
      inLanguage: 'en',
      mainEntity: {
        '@type': 'ItemList',
        itemListElement: GUIDES.map((guide, index) => ({
          '@type': 'ListItem',
          position: index + 1,
          name: guide.listing.title,
          url: canonicalUrl(guide.path),
        })),
      },
    },
  ]
}

export function topicStructuredData(topic: GuideTopic): Record<string, unknown>[] {
  return [
    {
      '@type': 'CollectionPage',
      '@id': `${canonicalUrl(topic.path)}#webpage`,
      url: canonicalUrl(topic.path),
      name: `${topic.label} guides`,
      description: topic.description,
      inLanguage: 'en',
      mainEntity: {
        '@type': 'ItemList',
        itemListElement: topic.guides.map((guide, index) => ({
          '@type': 'ListItem',
          position: index + 1,
          name: guide.h1,
          url: canonicalUrl(guide.path),
        })),
      },
    },
    breadcrumbStructuredData([
      { name: 'Home', path: '/' },
      { name: 'Guides', path: '/guides' },
      { name: topic.label, path: topic.path },
    ], topic.path),
  ]
}

export function guideStructuredData(guide: Guide): Record<string, unknown>[] {
  const topic = topicForGuide(guide)
  return [
    AUTHOR.schema,
    {
      '@type': 'Article',
      '@id': `${canonicalUrl(guide.path)}#article`,
      headline: guide.h1,
      description: guide.metaDescription,
      articleSection: topic.label,
      datePublished: guide.publishedAt,
      dateModified: guide.modifiedAt,
      image: canonicalUrl('/og.png'),
      author: { '@id': AUTHOR.schemaId },
      publisher: { '@id': `${SITE_URL}/#organization` },
      mainEntityOfPage: canonicalUrl(guide.path),
      inLanguage: 'en',
    },
    breadcrumbStructuredData([
      { name: 'Home', path: '/' },
      { name: 'Guides', path: '/guides' },
      { name: topic.label, path: topic.path },
      { name: guide.breadcrumb, path: guide.path },
    ], guide.path),
  ]
}

function breadcrumbStructuredData(
  entries: Array<{ name: string; path: string }>,
  pagePath: string,
): Record<string, unknown> {
  return {
    '@type': 'BreadcrumbList',
    '@id': `${canonicalUrl(pagePath)}#breadcrumb`,
    itemListElement: entries.map((entry, index) => ({
      '@type': 'ListItem',
      position: index + 1,
      name: entry.name,
      item: canonicalUrl(entry.path),
    })),
  }
}
