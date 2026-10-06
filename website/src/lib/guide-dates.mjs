import { latestLastmod } from './sitemap-lastmod.mjs'

// Content changes and owner review are distinct events. Neither date takes
// precedence over a later event when describing the published page.
/** @param {{ publishedAt?: string, updated?: string, reviewedAt?: string }} guide */
export function guideModifiedDate({ publishedAt, updated, reviewedAt }) {
  const modified = latestLastmod(publishedAt, updated, reviewedAt)
  if (modified === undefined) throw new TypeError('A guide must have an editorial date')
  return modified
}

/** @param {{ slug: string, legalReviewStatus: string, publishedAt?: string, updated?: string, reviewedAt?: string }} guide */
export function guideReviewWarning(guide) {
  if (guide.legalReviewStatus !== 'approved') return undefined
  guideModifiedDate(guide)
  if (!guide.reviewedAt) {
    return `/guides/${guide.slug}: published guide has no recorded owner review date`
  }
  if (guide.updated && guide.updated > guide.reviewedAt) {
    return `/guides/${guide.slug}: owner re-review pending (updated ${guide.updated}; last owner review ${guide.reviewedAt})`
  }
  return undefined
}
