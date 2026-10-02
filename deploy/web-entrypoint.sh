#!/bin/sh
# Runs from the nginx image's /docker-entrypoint.d before nginx starts.
# Publishes this release's frontend into the persistent /srv/www volume: hashed
# assets accumulate (open tabs may still lazy-load older chunks), index.html last.
set -eu
src=/opt/quill-dist
dst=/srv/www
mkdir -p "$dst"
(cd "$src" && find . -type f ! -name index.html) | while read -r f; do
  mkdir -p "$dst/$(dirname "$f")"
  cp -f "$src/$f" "$dst/$f"
done
cp -f "$src/index.html" "$dst/index.html.next"
mv -f "$dst/index.html.next" "$dst/index.html"
# Assets not shipped by any release for 30 days are gone from every cached index.html.
find "$dst/assets" -type f -mtime +30 -delete 2>/dev/null || true
echo "quill-web: published $(find "$src" -type f | wc -l) files into $dst"
