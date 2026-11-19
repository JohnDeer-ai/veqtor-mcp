# Veqtor website

The public site is an isolated, fully static Astro project. It does not import
the Python package, does not use server-side rendering or Pages Functions, and
does not need `@astrojs/cloudflare`. The generated `dist/` directory can be
served by any static host, including Cloudflare Pages. Astro telemetry is
disabled in the project scripts.

## Workspace website migration — 16 November 2026

The public home, installation instructions and product pages describe Veqtor
Workspace. All previously served page addresses and the 122-guide library remain
in place; no existing page is redirected or removed. Guide article bodies,
editorial metadata and review dates are unchanged. Shared navigation and product
calls to action now point readers to the Workspace workflow. The sitemap records
the navigation/product update separately from Article editorial dates.

`/setup` describes the current Claude Desktop and ChatGPT Desktop connections.
The Workspace repository is private and there is no verified public Workspace
installer linked here. The page states that availability and keeps its installer
button disabled. Do not substitute the earlier MCP download. When a public
Workspace release is ready, update setup, home structured data, `llms.txt`, and
`assertSetupCommandIntegrity` / `assertSchemaContracts` in the site check together.

`/veqtor-mcp` retains the published v0.5.0 extension, checksum and tagged
instructions. `/demo` and all video asset URLs retain the historical v0.1.2
recording, explicitly identified as the earlier product. The release pins in
`src/lib/public-release.ts` apply only to that earlier product.

The homepage visual examples contain a supplied Word excerpt and HTML document
illustrations. They do not establish a fresh end-to-end product acceptance run.
The website changes do not publish the Workspace source or change either Python
package. The release-bound `public/logo-512.png` is unchanged.

The migration also refreshes compatible transitive build dependencies to satisfy
the required high-severity audit gate. Astro remains pinned; the remaining
moderate advisory is reported by the audit rather than suppressed.

## Local development

Use Node.js 22.23.5 as pinned in `.node-version`. Astro 7.4.7 requires Node.js
22.23.4 or later. The site uses the 22.23.5 security release for local development,
GitHub Actions and Cloudflare Pages.

```sh
cd website
npm ci
npm run audit
npm run check
npm test
npm run build
npm run dev
```

Use `npm run preview` after a build to inspect the exact static production
output locally.

`npm run audit` checks the complete lockfile, including development and optional
dependencies for other platforms, against the npm advisory database. The website
CI runs it after the locked install and
fails on high or critical advisories, or an audit-service error. Lower-severity
findings are still reported. It does not suppress findings or rewrite the lockfile;
review dependency updates and rerun the site checks before merging them. This is
a build-dependency check, not a security guarantee for the published Python package
or a test for every possible vulnerability.

`npm run build` writes the deployable site to `website/dist/`, then verifies the
legacy URL/SEO inventory, internal links, real 404 behavior and plain-language
copy. The check pins all 153 legacy routes and the title, description, and
social title of the migrated Guides library, so a future slug or metadata
change requires an explicit migration decision. No build output or
`node_modules/` directory should be committed.

## Guide dates and owner review

`publishedAt` is the original publication date. `updated` records an actual
content change. `reviewedAt` records the last legal review by the owner; an AI
review, build, or publication does not advance it. Article `dateModified`, the
visible Updated date, and the editorial part of sitemap `lastmod` use the latest
valid date among these three fields. Sitemap dates may also include a later
page-level navigation change.

`legalReviewStatus: approved` controls publication. An already published guide
can retain that existing approval while later edits await owner re-review; it
does not certify that every later edit has received that review. The site check
prints a nonblocking warning for each published guide whose `updated` is later
than `reviewedAt`, or whose owner review date is missing. These warnings form the
owner's review queue. Advance `reviewedAt` only after the owner actually reviews
the current legal content. `owner_review_required` remains the unpublished
status for guides without publication approval.

## Cloudflare Pages configuration

Do not connect the production domain until a preview deployment has been
reviewed. When the repository is connected to Cloudflare Pages, use:

| Setting | Value |
| --- | --- |
| Production branch | `main` |
| Root directory | `website` |
| Dependency install | `npm ci` |
| Build command | `npm run build` |
| Build output directory | `dist` |
| Node version | `22.23.5` (read from `.node-version`) |

If the Pages dashboard does not expose a separate dependency-install field,
its npm lockfile installation runs before the build command; keep the build
command as `npm run build`. Preview deployments should be approved before the
custom domain is attached.

If a Cloudflare `NODE_VERSION` environment override is configured, keep it in sync
with `.node-version` or remove the override so the file selects the runtime.

After the preview is accepted:

1. Add `veqtor.pro` under **Workers & Pages -> project -> Custom domains**.
2. Copy and verify all existing DNS records before changing the apex-domain
   nameservers, especially MX, SPF, DKIM and DMARC records.
3. Verify the production domain, HTTPS, redirects, canonical URLs, sitemap and
   representative guide pages.
4. In the Cloudflare dashboard, create an account-level **Bulk Redirect** from
   the production `<project>.pages.dev` address to `https://veqtor.pro` with a
   `301`, query-string preservation, subpath matching and path-suffix
   preservation. A domain-level `pages.dev` redirect does not belong in the
   site's `_redirects` file.

Cloudflare should use a build-watch include path of `website/*` so Python-only
commits do not trigger a site deployment. Deployment and DNS changes are
operational steps and are intentionally not performed by this repository.

## Repository isolation

The Python wheel and source distribution use explicit allowlists in the root
`pyproject.toml` and `scripts/release_contract.py`. Website page sources are not
Python package inputs, but `website/public/logo-512.png` is a frozen MCPB input
and must not change in a website-only activation. Do not expand the release
allowlists or make the website build part of the Python release workflow.
