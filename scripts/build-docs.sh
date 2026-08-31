#!/usr/bin/env bash
# Build the published KubeIntellect documentation site into ./site.
#
# The site at https://mskazemi.com/kubeintellect/ is NOT a single mkdocs config: it is
# v2 at the root plus v1 published underneath at /v1/. That assembly used to live only in
# whoever ran it — the docs workflow was lost when the repo was restructured into v1..v4,
# the gh-pages branch was deleted, and GitHub Pages kept serving a frozen artifact that no
# tree in this repo could reproduce. This script is that assembly, written down.
#
# v3 and v4 are NOT published: v3 builds nothing, and v4 is ~two versions ahead of what is
# public. Publishing v4 is a deliberate decision, not a side effect of running this.
#
# Pages on this repo is build_type "workflow": it serves the artifact the docs workflow
# uploads, not a gh-pages branch. Do not swap this for `mkdocs gh-deploy` — that would push
# a branch nothing reads and the site would silently stop updating.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
out="${1:-$repo/site}"

rm -rf "$out" "$out.v1"
mkdocs build -f "$repo/v2/mkdocs.yml" -d "$out"        --clean --strict
mkdocs build -f "$repo/v1/mkdocs.yml" -d "$out.v1"     --clean
mv "$out.v1" "$out/v1"

# v1 is a nested sub-site, not a site of its own: only the domain root's robots.txt is ever
# read, and a second sitemap under /v1/ is declared nowhere, so both are dead weight. Its
# URLs go into the root sitemap instead, which is what mskazemi.com/robots.txt declares.
v1map="$(mktemp)"; trap 'rm -f "$v1map"' EXIT
cp "$out/v1/sitemap.xml" "$v1map"
rm -f "$out/v1/sitemap.xml" "$out/v1/sitemap.xml.gz" "$out/v1/robots.txt"

python3 - "$out" "$v1map" <<'PY'
import gzip, pathlib, re, sys
out = pathlib.Path(sys.argv[1])
root = out / "sitemap.xml"
merged = root.read_text().replace(
    "</urlset>",
    "".join(re.findall(r"<url>.*?</url>", pathlib.Path(sys.argv[2]).read_text(), re.S))
    + "</urlset>",
)
root.write_text(merged)
with gzip.GzipFile(filename="", mode="wb", fileobj=open(out / "sitemap.xml.gz", "wb"), mtime=0) as g:
    g.write(merged.encode())
print(f"sitemap: {merged.count('<loc>')} URLs")
PY

touch "$out/.nojekyll"

# Gate: every canonical and every <loc> must name mskazemi.com. mskazemi.github.io
# 301-redirects here, so naming it declares a canonical URL that serves no content —
# Search Console's "Page with redirect" / "Alternate page with proper canonical tag".
# Note the tolerant match: mkdocs-material's minifier strips attribute quotes, so a
# quotes-only grep passes blind over an unminified v1 page.
if grep -rq 'mskazemi\.github\.io' "$out"; then
  echo "FAIL: the built site still names mskazemi.github.io:" >&2
  grep -rl 'mskazemi\.github\.io' "$out" >&2
  exit 1
fi
canon=$(grep -rhoi '<link[^>]*canonical[^>]*>' "$out" --include='*.html' | wc -l)
locs=$(grep -c '<loc>' "$out/sitemap.xml")
echo "OK: $canon canonical tags, $locs sitemap URLs, all on mskazemi.com"
