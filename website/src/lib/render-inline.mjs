// Limited inline markup for guide strings: [label](href), **bold**, *italic*.
// Input is HTML-escaped first and link targets are restricted to an allowlist
// (https, http, site-relative paths, fragments), so source text can never
// inject markup or a scriptable URI. Parentheses inside a URL must be
// percent-encoded (%28/%29); an unencoded ")" ends the link target.
// Rendered anchors are parked behind NUL-framed placeholders while the
// emphasis passes run, so characters like asterisks inside URLs survive.

// https/http, root-relative paths (but not // or /\, which browsers
// normalise to a protocol-relative external origin), and fragments.
const SAFE_HREF = /^(?:https?:\/\/|\/(?![/\\])|#)/i

// NUL never occurs in editorial text; it frames anchor placeholders and is
// stripped from input up front so a placeholder can never be forged.
const MARK = String.fromCharCode(0)
const RESTORE = new RegExp(`${MARK}(\\d+)${MARK}`, 'g')

function escapeHtml(value) {
  return String(value)
    .replaceAll(MARK, '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
}

function renderEmphasis(text) {
  return text
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\*([^*]+)\*/g, '<em>$1</em>')
}

export function renderInline(text) {
  const anchors = []
  const parked = escapeHtml(text).replace(
    /\[([^\]]+)\]\(([^)\s]+)\)/g,
    (_match, label, href) => {
      if (!SAFE_HREF.test(href)) return label
      anchors.push(`<a href="${href}">${renderEmphasis(label)}</a>`)
      return MARK + (anchors.length - 1) + MARK
    },
  )
  return renderEmphasis(parked).replace(RESTORE, (_match, index) => anchors[Number(index)])
}
