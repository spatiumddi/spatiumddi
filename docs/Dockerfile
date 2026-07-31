# Local docs preview — Jekyll, matched to what GitHub Pages actually runs.
#
# The published site is built by GitHub Pages from this directory (see the
# "Documentation sites" note in CLAUDE.md). This image exists so the same
# tree can be rendered locally before publishing, because a docs change that
# only looks right on GitHub is a change nobody reviewed.
#
# Pinned to the plugin set declared in _config.yml — no theme, no bundler.
# Pages enables optional-front-matter and relative-links by default; the
# config declares them explicitly and so do we, or a local build silently
# diverges from the published one.
#
#   docker compose -f docker-compose.docs.yml up
#   → http://localhost:4000
FROM ruby:3.2-slim

# webrick is no longer a default gem on Ruby 3.x and jekyll serve needs it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && gem install --no-document \
         jekyll:4.3.4 \
         jekyll-optional-front-matter:0.3.2 \
         jekyll-relative-links:0.6.1 \
         webrick:1.8.1 \
    && apt-get purge -y build-essential \
    && apt-get autoremove -y

WORKDIR /srv/jekyll
EXPOSE 4000

# --force_polling: the source is a bind mount, and inotify does not fire
#   reliably across that boundary on every host, so edits would not trigger a
#   rebuild. Polling costs a little CPU and always works.
# --disable-disk-cache: the source is mounted read-only so a preview can never
#   mutate the working tree, and Jekyll otherwise insists on creating
#   .jekyll-cache/ next to the sources. Without this it aborts on EROFS.
CMD ["jekyll", "serve", \
     "--source", "/srv/jekyll", \
     "--destination", "/tmp/_site", \
     "--host", "0.0.0.0", \
     "--port", "4000", \
     "--force_polling", \
     "--disable-disk-cache", \
     "--livereload"]
