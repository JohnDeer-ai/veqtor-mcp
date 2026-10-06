import assert from 'node:assert/strict'
import { test } from 'node:test'

import { guideModifiedDate, guideReviewWarning } from '../src/lib/guide-dates.mjs'

const reviewedGuide = {
  slug: 'example',
  legalReviewStatus: 'approved',
  publishedAt: '2026-05-31',
  updated: '2026-06-14',
  reviewedAt: '2026-06-14',
}

test('a later edit updates the page date without recording an owner review', () => {
  const edited = Object.freeze({ ...reviewedGuide, updated: '2026-10-06' })
  assert.equal(guideModifiedDate(edited), '2026-10-06')
  assert.match(guideReviewWarning(edited), /owner re-review pending.*updated 2026-10-06; last owner review 2026-06-14/)
  assert.equal(edited.reviewedAt, '2026-06-14')
})

test('a later owner review updates the date and clears the pending review warning', () => {
  const reviewed = { ...reviewedGuide, reviewedAt: '2026-10-06' }
  assert.equal(guideModifiedDate(reviewed), '2026-10-06')
  assert.equal(guideReviewWarning(reviewed), undefined)
  assert.equal(guideReviewWarning(reviewedGuide), undefined)
})

test('publication is a fallback and a lower bound for the modified date', () => {
  assert.equal(guideModifiedDate({ publishedAt: '2026-05-31' }), '2026-05-31')
  assert.equal(guideModifiedDate({ ...reviewedGuide, publishedAt: '2026-07-01' }), '2026-07-01')
  assert.equal(guideModifiedDate({ updated: '2026-07-01' }), '2026-07-01')
})

test('invalid dates cannot hide behind a valid later date', () => {
  for (const field of ['publishedAt', 'updated', 'reviewedAt']) {
    for (const value of ['2026-02-30', '2026-2-03', '', null]) {
      assert.throws(() => guideModifiedDate({ ...reviewedGuide, [field]: value }), /ISO calendar date/)
    }
  }
  assert.throws(() => guideModifiedDate({}), /must have an editorial date/)
  assert.equal(guideModifiedDate({ publishedAt: '2024-02-29' }), '2024-02-29')
})

test('missing owner review is reported for published guides without publishing drafts', () => {
  const withoutReview = { ...reviewedGuide, reviewedAt: undefined }
  assert.match(guideReviewWarning(withoutReview), /no recorded owner review date/)
  assert.equal(guideReviewWarning({ ...withoutReview, legalReviewStatus: 'owner_review_required' }), undefined)
})
