# Veqtor website

The public site is an isolated, fully static Astro project. It does not import
the Python package, does not use server-side rendering or Pages Functions, and
does not need `@astrojs/cloudflare`. The generated `dist/` directory can be
served by any static host, including Cloudflare Pages. Astro telemetry is
disabled in the project scripts.

## v0.5.0 activation

The installation instructions target the published **v0.5.0 Alpha** release.
[Release run 39156615444, attempt 1](https://github.com/JohnDeer-ai/veqtor-mcp/actions/runs/39156615444)
completed successfully for commit `f4ef637690cf9e8c2b6927fba6b46c2577c1931a`.
GitHub reports immutable publication at `2026-10-19T10:18:45Z`. Public targets:

- [Immutable v0.5.0 release](https://github.com/JohnDeer-ai/veqtor-mcp/releases/tag/v0.5.0)
- [Exact PyPI v0.5.0 package](https://pypi.org/project/veqtor-mcp/0.5.0/)
- Verified public MCPB SHA-256: `8e29e12cc272ac168441e6affdeedd51199294db94ec6c2f206a0a810d587075`

The exact candidate's `.github/scripts/verify_published_pypi.py` and
`.github/scripts/verify_published_release.py` passed against the successful
attempt's artifacts on 19 October 2026, both in the release workflow and in
separate tokenless local checks. The public wheel, source distribution, MCPB
and checksum manifest match the approved bytes; the PyPI checks also verify
Trusted Publisher provenance. A separate installation of `veqtor-mcp==0.5.0`
from public PyPI passed the version and doctor checks and generated four demo
DOCX files in an isolated directory before this website activation.

Package publication and website deployment are separate. Retain the Cloudflare
deployment commit and fresh production checks with the activation evidence;
the release timestamp above does not establish a website deployment time.

`src/lib/public-release.ts` supplies the current release links, install pins
and checksum. `/setup` covers installation, upgrade, disposable-folder rollback
and a read-only connection check; `/docs` describes all fourteen tools,
paragraph history, projection-aware verification, own-author drafting and
ordinary anchored comments. The legacy counterproposal path creates a new
file. Author drafting can save to the working path with a required exact
backup and Word closed; its three tools do not append the legacy decision
journal. The v0.1.2 video stays historical.
The source update date is not deployment evidence: after production deployment,
check the live `/setup`, `/docs`, homepage structured data, `/llms.txt`, and all
release, download, checksum and PyPI links.

This is a separate website activation, not a new software release. Preserve the
tagged README's conditional version-selection rule, package inputs, release
assets and accepted evidence. Preserve the website's existing static hosting,
dependencies, guide URLs/SEO inventory and media. Subsequent build-dependency
maintenance is covered by the checks below.

## Local development

Use the Node version in `.node-version` (Node 22.23.2). The current Astro 7
dependency tree requires Node 22.19.0 or newer in the Node 22 line (including
`undici`, used by Astro's font tooling). This project sets Node 22.23.2 as its
minimum security baseline and pins that exact version so local, CI and
Cloudflare builds use the same runtime.

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
| Node version | `22.23.2` (read from `.node-version`) |

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
