# Veqtor website

The public site is an isolated, fully static Astro project. It does not import
the Python package, does not use server-side rendering or Pages Functions, and
does not need `@astrojs/cloudflare`. The generated `dist/` directory can be
served by any static host, including Cloudflare Pages. Astro telemetry is
disabled in the project scripts.

## Public v0.4.0 activation

The public installation instructions now select **v0.4.0 Alpha**, following
verified PyPI publication and the immutable GitHub Release published on
**2026-09-11 at 06:17:17 UTC**:

- [Immutable v0.4.0 release](https://github.com/JohnDeer-ai/veqtor-mcp/releases/tag/v0.4.0)
- [Exact PyPI v0.4.0 package](https://pypi.org/project/veqtor-mcp/0.4.0/)
- MCPB SHA-256: `44a75ee286c701f1a14a2c96fba531e8290240fb8d554eff886565b380b5bd2c`

`src/lib/public-release.ts` supplies the current release links, install pins
and checksum. `/setup` covers installation, upgrade, disposable-folder rollback
and a read-only connection check; `/docs` describes all nine tools, paragraph
history and projection-aware verification. The v0.1.2 video stays historical.
The source update date is not deployment evidence: after production deployment,
check the live `/setup`, `/docs`, homepage structured data, `/llms.txt`, and all
release, download, checksum and PyPI links.

This is a separate website activation, not a new software release. Preserve the
tagged README's conditional version-selection rule, package inputs, release
assets and accepted evidence. That activation preserved the website's existing
static deployment, dependencies, guide URLs/SEO inventory and media. Subsequent
build-dependency maintenance is covered by the checks below.

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
`pyproject.toml` and `scripts/release_contract.py`. The `website/` tree is not a
package input. Do not add it to those release allowlists and do not make the
website build part of the Python release workflow.
