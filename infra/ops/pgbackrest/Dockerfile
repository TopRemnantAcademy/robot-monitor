# pgBackRest disaster-recovery sidecar image
#
# Why build our own rather than pull an existing image:
#   1. The official project does **not** publish a `pgbackrest/pgbackrest` image — verified
#      2026-10-11 query of Docker Hub: under `pgbackrest/*` only the `test` and `doc` repos exist,
#      `pgbackrest/pgbackrest` returns {"message":"object not found"}.
#      (The image name recorded in `external/SOURCES.md:325` earlier was **wrong**; corrected this time.)
#   2. Third-party images (woblerr/pgbackrest etc.) have unauditable internal builds, violating AGENTS.md "don't trust bare image names".
#   3. The official sole recommended install path is the **PGDG package repo** (apt.postgresql.org):
#      external/pgbackrest/doc/xml/user-guide.xml:586
#        "Ubuntu packages for pgBackRest are available at apt.postgresql.org"
#      Install method verified (same file :343-344): install postgresql-common first, then run the PGDG repo script.
#
# Base image: official Debian slim (not a postgres image).
# Why not postgres:16 — that image's ENTRYPOINT is docker-entrypoint.sh, which would try to
#   initialize a PG cluster, conflicting with the "pure backup client" purpose (we don't need a local PG).
# pgbackrest only needs runtime libs like libpq, so use debian:bookworm-slim + the PGDG package.
# PG major-version consistency is guaranteed by "connecting to PG16 + pgbackrest client protocol"; no local PG needed.
FROM debian:bookworm-slim

# Official install path: PGDG repo (the pgdg script from user-guide.xml:343-344)
# jq: verify.sh / postgres-exporter custom queries both parse `info --output=json`.
# The official user-guide.xml "Using jq" section recommends jq for extracting that JSON, so install it in the image
#   to avoid discovering a missing tool at runtime.
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-common ca-certificates gnupg curl lsb-release \
 && /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y \
 && apt-get install -y --no-install-recommends pgbackrest jq \
 # Verify the binary exists in the image at build time, otherwise the build fails (don't let "configured but not installed" fake success slip through)
 && pgbackrest version \
 && jq --version \
 && rm -rf /var/lib/apt/lists/*

# Directory layout required by the official docs (user-guide.xml:626-637): log dir / config dir
# Note: mkdir -p -m does not apply to intermediate dirs (hadolint SC2174), so create then chmod in two steps
# postgres user: usually created by postgresql-common; explicitly fall back here to ensure the USER instruction resolves
RUN id postgres >/dev/null 2>&1 || useradd --system --create-home --shell /bin/bash postgres \
 && mkdir -p /var/log/pgbackrest /etc/pgbackrest /var/lib/pgbackrest \
 && chmod 770 /var/log/pgbackrest

# Config is **not COPYed into the image** — mounted read-only by compose (see docker-compose.dr.yml).
# Benefit: changing config doesn't require rebuilding the image; and the image contains no passphrase injection point, reducing the risk of baking secrets in by mistake.
RUN chown postgres:postgres /etc/pgbackrest /var/log/pgbackrest /var/lib/pgbackrest

# Run as postgres (pgbackrest refuses to run non-restore commands as root, verified:
#   external/pgbackrest/doc/xml/reference.xml:164)
USER postgres

# Idle by default; actual command overridden by compose's command (see docker-compose.dr.yml)
CMD ["sleep", "infinity"]
