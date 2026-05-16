# Adaptive Markdown — landing site

Static HTML for the project's home page. No build step; deploy as-is.

## Deploying

### GitHub Pages

In the repo's **Settings → Pages**, set:
- **Source:** Deploy from a branch
- **Branch:** `main`
- **Folder:** `/site`

GitHub will publish the contents of this directory at
`https://<owner>.github.io/<repo>/`. Add a custom domain (e.g.
`adaptivemarkdown.dev`) in the same Settings panel, and a `CNAME` file
will be written into `site/` automatically.

### Cloudflare Pages

Connect the repo, then in the deployment settings:
- **Build command:** *(none)*
- **Output directory:** `site`

Cloudflare detects the static files and serves them directly. Custom
domain configuration is in the Cloudflare dashboard.

### Anywhere else

It's a single HTML file plus an SVG favicon. Any static host works.
Drop both files at the web root.

## What's here

- **`index.html`** — the page. Single self-contained file. No
  external CSS, no JS framework. Embeds the demo videos from YouTube.
  Includes a small inline `<script>` that demonstrates the "embedded
  effect inside the page source" idea — hover the letters of the *Try
  it* paragraph and they fall.
- **`favicon.svg`** — copy of the project's favicon. Lives here too so
  the deployed site is self-contained (the repo root's `favicon.svg`
  is for the local viewer).

## Editing

Open `index.html` in a browser locally — `file://` works fine, no
server needed. Edit, save, refresh. The page has no build dependency.

Things you might want to change as the project evolves:

- The hero tagline (`<h1>Markdown that runs.</h1>`).
- The first demo-video embed (currently `H4MnFs8irm8` — the v0.1 demo).
- The four example prompts under "Things to try" — keep them honest;
  they describe what the agent actually does, not what we wish it did.
- The footer's copyright line.
