# Hosting and sizing notes

Notes from a sizing review on 2026-11-24. Nothing here has been built yet; it records the options and when to
revisit them.

## Today

There are two deployments. Each runs on one EC2 instance in us-west-2 with the full Docker Compose stack:

| Deployment | Instance | Notes |
| --- | --- | --- |
| Main (vid) | t3.large (2 vCPU, 8 GB) | Also used for ambient video builds and release checks, which need the 8 GB (one out-of-memory kill at 8 GB before a 4 GB swap file was added). |
| Clean (studio, `ADULT_FEATURES=false`) | t3a.medium (2 vCPU AMD EPYC 7571, 4 GB) since 2026-11-25, with a 4 GB swap file (swappiness 10) | Mostly idle: about 0.9 GB in use after start. Measured 1080p x264 encode at CRF 18: 8.0 fps (medium), 4.8 fps (slow). The public IP changed with the type change; DNS was updated. |

Each instance runs five containers:

| Container | Job | Typical memory |
| --- | --- | --- |
| frontend | Next.js pages | about 50 MB |
| backend | API (uvicorn, 2 workers) | about 210 MB |
| worker | RQ worker (`default`, `inspect`): renders, preview copies, generation polling | about 15 MB idle, much more during ffmpeg renders |
| postgres | Database | about 35 MB (dumps are about 120-270 KB compressed) |
| redis | Job queue for the worker | about 8 MB |

Media lives under `./data` on each instance's own disk (about 10 GB each). Backups go to S3 Files (see
[backups.md](backups.md)).

## Instance options considered

These are on-demand Linux prices in us-west-2 as of 2026-11-24. Check current prices before deciding.

| Type | CPU | Size | About $/month | Notes |
| --- | --- | --- | --- | --- |
| t3a.medium | AMD EPYC (older) | 2 vCPU / 4 GB | 27 | Same software as today; per-core about 10-20% slower than t3 for video encoding; web and database unaffected. |
| t3a.large | AMD EPYC (older) | 2 vCPU / 8 GB | 55 | Keep 8 GB with no swap. |
| t9g.medium | Graviton5 (ARM) | 2 vCPU / 4 GB | 30 | New in Nov 2026; fastest per dollar at 4 GB. No t9g.large yet. |
| t4g.medium / .large | Graviton2 (ARM) | 4 / 8 GB | 25 / 49 | Oldest Graviton; little saving over t3a. |
| m7g.large / m8g.large | Graviton3 / 4 (ARM) | 2 vCPU / 8 GB, not burstable | 60 / 66 | Full cores at all times; only worth it for heavy rendering. |
| t8i.medium | Intel Xeon 6 | 2 vCPU / 4 GB | 36 | Newest burstable x86; much faster per core than t3/t3a. |

**ARM (Graviton).** Video encoding with H.264 (x264) is competitive on Graviton3/4. Each Graviton vCPU is a full
core, while an x86 vCPU is a hyperthread. But moving to ARM is not an in-place type change. It needs:
- a new instance;
- the Docker images rebuilt for arm64 (including FFmpeg);
- the colour and render acceptance tests re-run;
- the data and database moved.

CI only tests on x86. Running one deployment on ARM and the other on x86 would let ARM-only breakage reach
production unnoticed. If we move to ARM, move both deployments together and add an ARM test job to CI.

**Savings Plans.** A 1-year Savings Plan usually takes 30-40% off any of these. That is a bigger saving than any
type change.

## Scenarios tabled

**Three t9g.medium (vid, studio, shared database).** About $91 a month, against $122 today or about $88 after
studio moves to t3a.medium. Tabled because:
- vid needs 8 GB for builds and checks;
- the databases are tiny, so a separate database server mostly adds cost, a network hop and a single point of
  failure for both sites;
- studio's clean data would sit beside vid's.

Revisit when a t9g.large (8 GB) exists. The likely shape then: vid on t9g.large and studio on t9g.medium, each with
its own database container, moved together, with an ARM CI job.

**Sharing containers between deployments.**
- frontend and backend: per-deployment. They hold each deployment's settings, such as `ADULT_FEATURES`, and its
  database connection.
- postgres and redis: technically shareable, but tiny, so sharing saves almost nothing and couples the sites'
  uptime.
- **Redis trap.** Both deployments use the same RQ queue names (`default`, `inspect`) on Redis database 0. A shared
  Redis must give each deployment its own database number, or its own queue names. Otherwise one site's worker
  runs the other site's jobs against the wrong database and files.
- worker: the only container where sharing could pay off, because ffmpeg rendering is the heavy load. A shared or
  separate render worker needs:
  - media on shared storage (for example S3) instead of each instance's `./data`;
  - per-deployment queues and credentials;
  - a clean split of settings.

  That is a development project, not a configuration change.

**When to revisit.** Revisit any of this when:
- we run more than two instances;
- rendering load grows enough that the app servers need sizing for it;
- a t9g.large is available.

At that point the first candidate is a separate render-worker machine, possibly started on demand, so the app
servers can stay small.
