import { parse } from 'parse5'

function attribute(node, name) {
  return node.attrs?.find((entry) => entry.name === name)?.value
}

function collectElements(node, predicate, results = []) {
  if (node.tagName && predicate(node)) results.push(node)
  for (const child of node.childNodes ?? []) collectElements(child, predicate, results)
  return results
}

function hasClass(node, className) {
  return (attribute(node, 'class') ?? '').split(/\s+/).includes(className)
}

function normalizePathname(pathname) {
  if (!pathname || pathname === '/') return '/'
  return `/${pathname.replace(/^\/+|\/+$/g, '')}`
}

export function validateRenderedBridge({ html, className, pageUrl, expectedTarget, siteOrigin }) {
  const document = parse(html)
  const mains = collectElements(document, (node) => node.tagName === 'main')
  if (mains.length !== 1) {
    throw new Error(`expected one <main>, found ${mains.length}`)
  }

  const sections = collectElements(
    mains[0],
    (node) => node.tagName === 'section' && hasClass(node, className),
  )
  if (sections.length !== 1) {
    throw new Error(`expected one section.${className} inside <main>, found ${sections.length}`)
  }

  const anchors = collectElements(sections[0], (node) => node.tagName === 'a')
  if (anchors.length !== 1) {
    throw new Error(`section.${className} must contain exactly one descendant anchor, found ${anchors.length}`)
  }

  const href = attribute(anchors[0], 'href')?.trim()
  if (!href) {
    throw new Error(`section.${className} bridge anchor must have a non-empty href`)
  }

  let targetUrl
  try {
    targetUrl = new URL(href, pageUrl)
  } catch {
    throw new Error(`section.${className} has an invalid bridge link ${JSON.stringify(href)}`)
  }
  if (targetUrl.origin !== siteOrigin) {
    throw new Error(`section.${className} bridge link must stay on ${siteOrigin}, found ${targetUrl.origin}`)
  }

  const target = normalizePathname(targetUrl.pathname)
  if (target !== expectedTarget) {
    throw new Error(`section.${className} must link to ${expectedTarget}, found ${target}`)
  }
}
