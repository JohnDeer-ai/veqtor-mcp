// Limited inline markup for guide strings: [label](href), **bold**, *italic*.
// Input is HTML-escaped first and link targets are restricted to an allowlist
// (https, http, site-relative paths, fragments), so source text can never
// inject markup or a scriptable URI. Parentheses inside a URL must be
// percent-encoded (%28/%29); an unencoded ")" ends the link target.

const SAFE_HREF = /^(?:https?:\/\/|\/(?!\/)|#)/i

function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
}

export function renderInline(text) {
  return escapeHtml(text)
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_match, label, href) =>
      SAFE_HREF.test(href) ? `<a href="${href}">${label}</a>` : label)
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\*([^*]+)\*/g, '<em>$1</em>')
}
