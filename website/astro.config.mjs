import { defineConfig } from "astro/config";

export default defineConfig({
  site: "https://veqtor.pro",
  output: "static",
  trailingSlash: "never",
  build: {
    // Cloudflare Pages maps `page.html` to `/page`. This preserves the
    // extensionless, no-trailing-slash URLs already used by veqtor.pro.
    format: "file",
  },
});
