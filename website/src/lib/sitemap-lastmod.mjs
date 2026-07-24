const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/

export const LINK_ARCHITECTURE_LASTMOD = '2026-07-23'

function assertIsoDate(value) {
  const parsed = new Date(`${value}T00:00:00Z`)
  if (
    !ISO_DATE.test(value)
    || Number.isNaN(parsed.getTime())
    || parsed.toISOString().slice(0, 10) !== value
  ) {
    throw new TypeError(`Expected an ISO calendar date, received ${JSON.stringify(value)}`)
  }
}

export function latestLastmod(...dates) {
  let latest
  for (const date of dates) {
    if (date === undefined) continue
    assertIsoDate(date)
    if (latest === undefined || date > latest) latest = date
  }
  return latest
}
