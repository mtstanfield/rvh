# CPG host setup

rvh's CPG worker augmentation runs Joern inside Docker. You need
Docker on the host that runs `run_repo_vuln_hunt.py`; no host JVM or
host Joern binaries are required. The orchestrator uses Docker both to
build the CPG (`joern-parse`) and to host the query server
(`joern --server`, the sidecar).

## Install Docker and pull the Joern image

    docker pull ghcr.io/joernio/joern@sha256:869aa86c27609d78d1ca4a48b88304bcfd7681f85f4f7ac6557a7aff16a2960d

The image is pinned by digest (`JOERN_IMAGE` in `scripts/cpg_build.py`);
`:nightly` is a different image. The first CPG build pulls it if it's
missing. The official Joern image is on GitHub Container Registry, not
Docker Hub.
That's the only dependency. rvh uses Docker to run
`joern-parse` (build time) and `joern --server` (sidecar) — no host JVM
or host Joern binaries required.

## Preflight check

    docker image inspect ghcr.io/joernio/joern@sha256:869aa86c27609d78d1ca4a48b88304bcfd7681f85f4f7ac6557a7aff16a2960d >/dev/null && echo ok

Prints `ok` when Docker is up and the image is present. If the CPG build
or sidecar fails (docker not running, image can't be pulled), the run
continues in no-CPG mode with a warning. Force no-CPG explicitly via
`--no-cpg`.

## Resource notes

- **CPG build RAM:** 4–16 GB typical for medium C++ projects. 32+ GB for large
  (XRootD-scale) targets. The Joern JVM runs inside the container; give
  Docker Desktop enough memory (Settings → Resources on macOS/Windows) to
  cover this.
- **CPG artifact size:** 100 MB – 5 GB on disk. Stored at `.rvh-runs/cpg/` per
  repo (one-run-per-repo model). `rm -rf .rvh-runs/cpg/` to force a rebuild;
  rebuild also triggers automatically when the source tree hash or Joern
  version changes.
- **Sidecar RAM at query time:** roughly the CPG artifact size plus JVM overhead.
- **Query latency:** milliseconds to single-digit seconds per query. Concurrent
  worker dispatches may queue briefly — Joern's server is single-threaded for
  most operations.

## Troubleshooting

- **`docker not found on PATH`:** install Docker, or set `--no-cpg` to
  skip the CPG entirely. rvh will run in degraded-no-CPG mode; prompts
  omit the `rvh-cpg` tool references automatically.
- **`docker pull ghcr.io/joernio/joern@sha256:…` fails with `denied: denied`:**
  stale ghcr.io credentials on the host block anonymous pulls of public
  images. Run `docker logout ghcr.io` and retry. If that doesn't fix it, the
  Docker daemon may be down — `docker info` is a quick liveness check.
- **`docker pull` fails with network errors:** retry once you have
  connectivity.
- **joern-parse OOMs inside the container:** raise the JVM heap with
  `RVH_CPG_HEAP_GB` (default 16; it covers both the build and the sidecar)
  and keep Docker's memory limit above it (Desktop → Resources on
  macOS/Windows), or run on a host with more RAM.
- **Sidecar health check fails:** check `.rvh-runs/sidecar.port` for the
  host-side port, then `curl http://127.0.0.1:<port>/` to see what Joern
  returns. Use `docker ps` to verify the sidecar container is running and
  `docker logs <container-id>` (the id is in `.rvh-runs/sidecar.container-id`)
  to inspect JVM-side errors. If the sidecar crashed,
  `run_repo_vuln_hunt.py` will log the failure and continue in no-CPG mode.
- **Queries hang:** Joern server is single-threaded for most operations. If
  many workers dispatch at once, queries queue. Reduce `--parallelism` or
  accept the added latency.
- **Worker container can't reach sidecar:** workers reach it as
  `http://rvh-cpg:8080` on the run's internal Docker network
  (`rvh-<repo>-<hash>-net`). Check that the sidecar container joined that
  network: `docker network inspect <network>`.
- **Python import errors on sidecar lifecycle:** `run_repo_vuln_hunt.py` and
  its helpers import `requests` (via `cpg_sidecar`). If running outside
  the project venv, install `requests` system-wide or activate
  `.venv/bin/activate` before invoking the orchestrator.
