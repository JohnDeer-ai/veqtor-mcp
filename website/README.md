# Veqtor website

The public site is an isolated, fully static Astro project. It does not import
the Python package, does not use server-side rendering or Pages Functions, and
does not need `@astrojs/cloudflare`. The generated `dist/` directory can be
served by any static host, including Cloudflare Pages. Astro telemetry is
disabled in the project scripts.

## Local development

Use the Node version in `.node-version` (Node 22.16.0). Astro 7 requires an
even-numbered Node release at or above 22.12.0; the exact version is pinned so
local and Cloudflare builds do not drift.

```sh
cd website
npm ci
npm run check
npm test
npm run build
npm run dev
```

Use `npm run preview` after a build to inspect the exact static production
output locally.

`npm run build` writes the deployable site to `website/dist/`, then verifies the
legacy URL/SEO inventory, internal links, real 404 behavior and plain-language
copy. The check pins all 145 legacy routes and the title, description, and
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
| Node version | `22.16.0` (read from `.node-version`) |

If the Pages dashboard does not expose a separate dependency-install field,
its npm lockfile installation runs before the build command; keep the build
command as `npm run build`. Preview deployments should be approved before the
custom domain is attached.

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
