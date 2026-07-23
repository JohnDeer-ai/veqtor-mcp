import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'

const websiteDir = join(dirname(fileURLToPath(import.meta.url)), '..')
const source = JSON.parse(readFileSync(join(websiteDir, 'src', 'data', 'guides-source.json'), 'utf8'))
const approved = source.guides.filter((guide) => guide.legalReviewStatus === 'approved')
const approvedSlugs = new Set(approved.map((guide) => guide.slug))

test('the migrated editorial inventory stays complete and publication-filtered', () => {
  assert.equal(source.guides.length, 123)
  assert.equal(approved.length, 122)
  assert.equal(source.clusters.length, 18)
  assert.equal(source.guides.filter((guide) => guide.legalReviewStatus !== 'approved').length, 1)
})

test('guide and topic identities are unique', () => {
  assert.equal(approvedSlugs.size, approved.length)
  assert.equal(new Set(source.clusters.map((topic) => topic.id)).size, source.clusters.length)
})

test('every published guide has complete article content and valid references', () => {
  const topicIds = new Set(source.clusters.map((topic) => topic.id))
  for (const guide of approved) {
    assert.ok(topicIds.has(guide.cluster), `${guide.slug} has an unknown topic`)
    assert.ok(guide.sections.length > 0, `${guide.slug} has no full sections`)
    assert.ok(guide.checklist.length > 0, `${guide.slug} has no checklist`)
    for (const related of guide.related) {
      if (related.slug) assert.ok(approvedSlugs.has(related.slug), `${guide.slug} links to unpublished ${related.slug}`)
    }
  }
})

test('all topic pillar and spoke references resolve to approved guides', () => {
  const topicDescriptions = new Set()
  const topicBridgeHeadings = new Set()
  for (const topic of source.clusters) {
    assert.ok(approvedSlugs.has(topic.pillarSlug), `${topic.id} has an unpublished pillar`)
    const pillar = approved.find((guide) => guide.slug === topic.pillarSlug)
    assert.ok(topic.metaDescription, `${topic.id} has no topic meta description`)
    assert.notEqual(
      topic.metaDescription,
      pillar.metaDescription,
      `${topic.id} reuses its pillar meta description`,
    )
    assert.ok(!topicDescriptions.has(topic.metaDescription), `${topic.id} has a duplicate topic meta description`)
    topicDescriptions.add(topic.metaDescription)
    const expectedBridge = topic.id === 'limitation-of-liability'
      ? '/demo'
      : '/contract-redline-analysis'
    assert.equal(topic.productBridge.href, expectedBridge, `${topic.id} has an unexpected product bridge target`)
    for (const field of ['label', 'heading', 'title', 'body']) {
      assert.ok(topic.productBridge[field], `${topic.id} product bridge has no ${field}`)
    }
    assert.ok(
      !topicBridgeHeadings.has(topic.productBridge.heading),
      `${topic.id} has a duplicate product bridge heading`,
    )
    topicBridgeHeadings.add(topic.productBridge.heading)
    if (expectedBridge !== '/demo') {
      assert.doesNotMatch(
        `${topic.productBridge.label} ${topic.productBridge.title} ${topic.productBridge.body}`,
        /\bdemo\b|\bwatch\b/i,
        `${topic.id} promises a demo but links to the redline-analysis page`,
      )
    }
    for (const slug of topic.spokeSlugs) {
      assert.ok(approvedSlugs.has(slug), `${topic.id} links to unpublished spoke ${slug}`)
    }
  }
})

test('retired product calls to action remain isolated from legal article bodies', () => {
  for (const guide of approved) {
    const hrefEntries = guide.related.filter((related) => related.href)
    assert.equal(hrefEntries.length, 1, `${guide.slug} source CTA inventory changed`)
    assert.ok(
      hrefEntries[0].href === '/#try' || hrefEntries[0].href === '/demo',
      `${guide.slug} has an unexpected source CTA`,
    )
    const legalBody = JSON.stringify([guide.sections, guide.checklist])
    assert.doesNotMatch(legalBody, /\/#try|risk-ranked|upload (?:a|your) contract/i)
  }
})
