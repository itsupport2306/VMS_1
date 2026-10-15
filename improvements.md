# VMS Website — Performance, Scalability & Resource Plan

| | |
|---|---|
| **Scope** | Whole repository: FastAPI backend ([backend/main.py](backend/main.py)), Nexus sync script ([api_nexus.py](api_nexus.py)), static web UI ([backend/web/](backend/web/)), MongoDB usage, [Dockerfile](Dockerfile), [docker-compose.yml](docker-compose.yml), [requirements.txt](requirements.txt), [.env.example](.env.example) |
| **Revision** | 2026-10-15, revision 2. It re-verifies revision 1 against commit `81712e0` (job alert preferences), adds new findings, re-runs the benchmarks, and adds a Redis verdict, resource categories, sizing and cost. |
| **Method** | Code and config review, plus local micro-benchmarks (§3.2). There was no production access: no logs, APM, Atlas metrics or traffic data. |
| **Status** | Plan only. No application code was changed, no files were deleted, and no services were installed. |
| **Constraint** | Nexus stays the **only** external job source. |

### Implementation status (2026-10-15)

Done in code, and verified by the functional tests described below:

| Item | Status |
|---|---|
| L1 job-list rebuild: reload no longer clears the response cache unless the data changed; description sanitising memoised (`lru_cache`); response cache is checked before the job list is rebuilt | Done |
| L2 Excel loader running on every request | Done |
| L3 per-user auth cache (60 s) and no full users scan on a miss; login always reads fresh | Done |
| L4 cross-worker coherence via a `meta` version stamp (polled every 5 s) | Done |
| L5 slim `/api/jobs` (260-character preview, `description_truncated`), full text from `/api/jobs/{id}` on demand | Done |
| L6 logo 600 KB → 31 KB, `Cache-Control` on assets, chunked job rendering ("Show more"), explicit load-error state | Done |
| S-1 ingest token (opt-in via `NEXUS_INGEST_TOKEN`), S-2 auth on `/api/jobs/{id}` and `/api/candidates/job/{id}` (vendors see only their own) | Done (resumes route and Excel/Ceipal removal still open) |
| S-4 escaped job-card fields | Done (CSP not added) |
| S-7 `/healthz`, `/readyz`; S-10 Mongo timeouts and pool 20; request timing header and slow-request log | Done |
| Sync script: one CSV read per scan, no rewrite on idle polls, failed-row backoff, stale `posting` recovery, automatic re-login on 401 | Done |
| Compose: healthcheck and log rotation (the Nexus sync runs on a separate server, so no sync container is added) | Done |
| 2-hourly open/closed job report e-mail | Done (see `JOB_STATUS_REPORT_*` in `.env.example`) |

Not done: moving sanitising fully to write time (the memoised cache removes the repeated cost without a data migration), the MongoDB-based sync rewrite with updates and closures (F-1), pagination of the candidate lists, rate limiting, secret rotation, and removal of the Excel and Ceipal code.

### Evidence labels

| Tag | Meaning |
|---|---|
| **[CODE]** | Verified in this repo at the cited file and line. |
| **[MEASURED-LOCAL]** | Measured on a dev laptop (12 logical CPUs, Python 3.10, pydantic 2.12, pandas 2.2) using the repo's 95-row `jobs.csv`. This is an order of magnitude, not production data. |
| **[HYPOTHESIS]** | Plausible from the code. Needs production timing to confirm. The test is stated. |
| **[ESTIMATE]** | A planning number derived from stated assumptions. |

No secrets are reproduced in this document. Hard-coded credentials are described by location only (§7, S-6).

---

## 1. Executive Summary

**Verdict.** The lag comes from **work the code repeats unnecessarily**. It does not come from a lack of CPU, RAM or Redis. The web tier never calls Nexus, so neither users nor Nexus can slow each other down. Most of the fixes cost **$0 in infrastructure**.

**Verified causes of lag, ranked by likely impact:**

| # | Cause | Evidence | Fix cost |
|---|---|---|---|
| L1 | Each worker re-reads **all** jobs from MongoDB, re-runs about 22 regexes per description, and re-serialises the list, every 300 s and after every job post, whether or not anything changed. Requests on that worker queue behind the rebuild. | [CODE] + [MEASURED-LOCAL]: about **1.2 s of CPU per 1,000 jobs** for the vendor payload | $0, code |
| L2 | **New finding:** when the Excel file is absent (the Docker image does not ship it), the Excel loader re-runs on **every** `/api/jobs` call. Each run takes a lock and prints 2 log lines. | [CODE] | $0, one-line fix |
| L3 | After the first 5 minutes of uptime, **every authenticated request makes a MongoDB round trip** to look up the user. A cache miss falls through to a regex query and then a **full scan of the users collection**. | [CODE]. Cost = Atlas RTT, unknown until measured | $0, code |
| L4 | The two uvicorn workers keep separate caches, so tabs and refreshes see **different job lists for up to 5 minutes**. Users perceive this as "random" behaviour. | [CODE] | $0, code |
| L5 | `/api/jobs` ships every full description, but the card shows only 260 characters. | [MEASURED-LOCAL] at 1,000 jobs: **405 KB gzip / 2.3 MB JSON**, compared with **37 KB / 0.58 MB** for a slim list | $0, code |
| L6 | The 600 KB logo and the uncompressed, un-fingerprinted static assets slow the first paint. | [CODE] | $0 |
| L7 | **Unknown, check first:** Atlas region and tier. M0 throttles at 100 ops/s, Flex at 500 ops/s. A cross-region cluster adds tens of ms to every query. | README warns about this; `/api/health` reports `mongodb_ping_ms` | Possibly $8-57/month |

**Not causes** [CODE]: Nexus API calls (the web path makes none), browser tabs triggering syncs (they can't), notification polling (visibility-gated at 60 s), and digest e-mails (lease-guarded, one worker per cycle).

**The biggest reliability risk is the Nexus sync script.** [CODE] It uses a CSV file as its database, rewritten in full roughly every 10 s. It never propagates job updates or closures. Its admin JWT silently expires after 7 days. When run without credentials it falls back to an unauthenticated ingest endpoint.

**Infrastructure verdict:**

| | |
|---|---|
| **Redis** | **Not needed** now, or at 1,000 concurrent users on one or two app instances (§4). |
| **Required now** | A separate sync-worker container (same droplet, $0); free-tier monitoring and uptime checks ($0); a reverse proxy if the droplet has none ($0); confirmation that Atlas is in the same region and is not M0. |
| **Recommended later** | A larger droplet, more workers, a second instance with a load balancer, Atlas M10/M20, object storage for resumes. Each has a measured trigger (§5). |
| **Estimated monthly cost** | ≈ **$30-80 today**, ≈ $30-80 at 100 concurrent users, ≈ $110-200 at 500, ≈ $110-260 at 1,000 (§6). |

---

## 2. Current Architecture (verified)

| Layer | What is there | Source |
|---|---|---|
| Frontend | One static page with vanilla JS and no build step: `index.html` (395 lines), `app.js` (2,668 lines, 100 KB), `app.css` (25 KB), `logo.png` (**600 KB**). Served by FastAPI `StaticFiles`. | [backend/web/](backend/web/) |
| Backend | One FastAPI file: 5,375 lines, 43 routes, 204 `print()` calls. | [backend/main.py](backend/main.py) |
| Server | `uvicorn --workers ${WEB_CONCURRENCY}` (default **2**) on `python:3.11-slim`. `build-essential` is left in the image. | [Dockerfile](Dockerfile) |
| Orchestration | One compose service with `restart: unless-stopped`. **No** healthcheck, resource limits, reverse proxy or TLS. | [docker-compose.yml](docker-compose.yml) |
| Database | MongoDB Atlas (`vms` database) via pymongo, `maxPoolSize=50` per worker, resumes in GridFS. Falls back to JSON files if `MONGODB_URI` is unset. | [main.py:131-136](backend/main.py#L131-L136) |
| Job source | **Nexus** via the stand-alone script [api_nexus.py](api_nexus.py), which POSTs into `manual_jobs`. **Where it runs is not defined in the repo.** | §8 |
| Dormant code | Ceipal client (~800 lines, `CEIPAL_ENABLED = False`), Excel job loader, `database/models.py` (SQLAlchemy, unused by the backend). | [main.py:2157](backend/main.py#L2157) |
| Dependencies | **All unpinned.** pydantic resolves to v2, but the code uses v1 APIs (`.dict()`, `.json()`, `root_validator`, `@app.on_event`) through deprecation shims. | [requirements.txt](requirements.txt) |

**Jobs page request flow** [CODE]:

```
Browser                         FastAPI worker (1 of 2)                           MongoDB Atlas
GET / , /assets/*  ──────────▶  StaticFiles (logo 600 KB, no long-lived cache headers)
GET /api/jobs  (Bearer JWT) ──▶ get_current_user: jwt.decode
                                  └─ load_user_by_email ─ user cache stale (>300 s)? ──▶ users.find_one   (L3)
                                get_jobs_response_payload (in a thread)
                                  ├─ get_all_jobs()  ← runs on EVERY request, before the response cache check
                                  │    ├─ load_manual_jobs: TTL 300 s expired? lock → find({}) all docs ──▶ manual_jobs (L1)
                                  │    │                    → pydantic Job per doc → clear response cache
                                  │    └─ load_excel_jobs: file absent ⇒ cache [] ⇒ "not fresh" ⇒ lock + 2 prints   (L2)
                                  └─ response cache miss → regex-sanitise every job → JSON → sha1 → ETag           (L1)
◀── 200 gzip body, or 304 if If-None-Match matches
JS: allJobs = data.jobs → renderJobs() builds one card per job; notifications poll every 60 s while visible
```

---

## 3. Root Causes of Lag (verified, with measurements)

### 3.1 Findings

#### L1 — Full rebuild on the read path, per worker, on a timer (Critical)
- **Evidence [CODE]:**
  - `load_manual_jobs` refreshes on a 300 s TTL ([main.py:3612-3621](backend/main.py#L3612-L3621)).
  - `_read_manual_jobs_from_store` reads every document and **unconditionally** calls `clear_jobs_response_cache()` ([main.py:3640](backend/main.py#L3640)).
  - The next request re-runs `sanitize_job_for_display` per job: `hide_compensation_details` plus 21 client-name regexes for vendors ([main.py:264-284](backend/main.py#L264-L284), [main.py:3848-3857](backend/main.py#L3848-L3857)).
  - Every job post (`upsert_manual_jobs`, [main.py:3737](backend/main.py#L3737)) also clears the receiving worker's cache. The sync posts in batches of 25, so an initial load of N new jobs causes up to N/25 rebuilds.
  - The vendor and admin payloads are built separately.
- **Measured [MEASURED-LOCAL]:**

  | Step, per 1,000 jobs | Time |
  |---|---|
  | Vendor sanitising (HTML-stripped text, avg 1.9 KB) | **≈ 1,230 ms** |
  | Admin sanitising | ≈ 140 ms |
  | Building the pydantic `Job` models and calling `.dict()` | ≈ 14 ms |
  | JSON encoding | ≈ 16 ms |

  Regex sanitising is about 85× the cost of everything else.
- **Effect:** the work runs in `asyncio.to_thread`, but it is CPU-bound Python and holds the GIL. Other requests on that worker slow down for about 1-1.5 s per rebuild at 1,000 jobs, and requests that need the job list wait on the lock. This repeats at least every 5 minutes on each worker. [HYPOTHESIS: confirm as a periodic p95 spike once request timing exists, R-1.]
- **Fix:** sanitise **once at write time** and store the vendor and admin text. Check the response cache **before** calling `get_all_jobs()`. Invalidate on change, not on a timer (L4). Together these turn a rebuild into a memory copy.

#### L2 — Excel loader runs on every request when the file is absent (High impact, trivial fix) — new
- **Evidence [CODE]:**
  - `_excel_jobs_cache_is_fresh()` returns `bool(_excel_jobs_cache and ...)` ([main.py:3222-3227](backend/main.py#L3222-L3227)). An empty list is falsy, so "no Excel jobs" never counts as cached.
  - `EXCEL_JOBS_FILE` defaults to `VMS Job Fiule.xlsx` in the working directory ([main.py:2165](backend/main.py#L2165)), and the [Dockerfile](Dockerfile) does not copy it.
  - Every `get_all_jobs()` call (every `/api/jobs`, `/api/jobs/{id}`, including 304s) therefore takes `_excel_jobs_rebuild_lock`, stats the file and prints `[Excel] Original file not found` and `[Excel] Total jobs ...` ([main.py:3230-3260](backend/main.py#L3230-L3260)).
- **Impact:** all job requests on a worker serialise briefly on one lock, and the logs gain two lines per request. Docker's default `json-file` driver does not rotate logs, so the log file grows without limit.
- **Fix:** test `_excel_jobs_cache is not None`. Better, retire the Excel path entirely (it conflicts with "Nexus only"; §7, S-8).

#### L3 — User lookup costs a MongoDB round trip on most requests (High)
- **Evidence [CODE]:**
  - The user cache is seeded at import and marked fresh ([main.py:2052-2053](backend/main.py#L2052-L2053)). After `USERS_CACHE_SECONDS=300` it is stale.
  - `load_user_by_email` adds to the cache **only if it is still fresh** ([main.py:1870](backend/main.py#L1870)), so it is never refilled per user. Only a full `load_users_from_json()` (from `/api/health` or submission history) refreshes it.
  - On a miss, the code runs an exact `find_one`, then a case-insensitive `$regex` query, then a **full collection scan** ([main.py:1853-1863](backend/main.py#L1853-L1863)). An unknown or mistyped login e-mail therefore triggers a full users scan.
- **Impact:** one or more Atlas round trips per authenticated call. That is 1-5 ms in the same region and 20-80 ms cross-region [ESTIMATE]. It also doubles MongoDB ops/s, which matters on throttled tiers (L7).
- **Fix:** a per-user cache with a 60 s TTL, bounded in size. Store e-mails lower-cased so the regex and full-scan fallbacks can go.

#### L4 — Workers and tabs disagree (High, correctness)
- **Evidence [CODE]:** caches are process-local. A write clears only the cache of the worker that received it ([main.py:3737](backend/main.py#L3737)). `.env.example` says so itself: "Lower MANUAL_JOBS_CACHE_SECONDS if new jobs must appear faster on the workers that did not receive the post".
- **Impact:** for up to 300 s, a new, edited or deleted job is visible on one worker but not the other. Two tabs, or two refreshes, can show different lists.
- **Fix (no Redis):** add one document, `meta {_id: "jobs", version}`, and `$inc` it on every job write. Each worker reads it at most every ~5 s (one indexed `find_one` per worker per 5 s, not per request) and rebuilds only when the version changes.

#### L5 — Job list payload (High once there are hundreds of jobs)
- **Evidence:**
  - [CODE] The card truncates descriptions at 260 characters ([app.js:1030-1031](backend/web/assets/app.js#L1030-L1031)), and search does not look at descriptions ([app.js:952](backend/web/assets/app.js#L952)).
  - [MEASURED-LOCAL] Synthetic 1,000 jobs: the full list is 2.29 MB JSON / **405 KB gzip**. A slim list with a 260-character preview is 0.58 MB / **37 KB gzip**, about 11× smaller on the wire and about 4× less JSON for the browser to parse.
- **Already mitigated:** gzip and ETag/304 make repeat loads cheap. The first load each session, and every change to the job set, still pay the full cost.
- **Fix:** return slim items from `/api/jobs`. Fetch the full description from `/api/jobs/{id}` when the modal opens, after adding authentication to that route.

#### L6 — Frontend
- **Evidence [CODE]:**
  - `logo.png` is 600 KB and is used twice.
  - Static assets have no `Cache-Control` and no content-hashed names.
  - `renderJobs()` rebuilds every card on every search keystroke (200 ms debounce) ([app.js:981-1056](backend/web/assets/app.js#L981-L1056)).
  - On a failed load, the page shows "No jobs found." instead of an error.
- **Impact:** the logo is a one-off cost of about 0.5 s on a 10 Mbit link. The rendering cost is small at about 100 jobs and grows linearly [HYPOTHESIS: profile at 1,000 jobs].
- **Fix:** compress the logo to WebP or SVG (a few KB), add cache headers, precompute search fields, render in chunks, and add an explicit error state with a Retry button.

#### L7 — Unknown infrastructure factors (check before buying anything)
| Check | Why | How |
|---|---|---|
| Atlas region vs droplet region | A cross-region cluster adds latency to every query | `GET /api/health` → `mongodb_ping_ms`; a few ms is fine |
| Atlas tier | **M0 is capped at 100 ops/s with no backups. Flex is capped at 500 ops/s and 5 GB.** Throttling looks like random lag. | Atlas UI → cluster tier; Metrics → operation throttling |
| Droplet size and CPU steal | 2 workers on 1 vCPU contend with each other | `nproc`, `top` (`st` column), `docker stats` |
| What polls `/api/health` | Each call makes 3 pings and may scan all users | Proxy logs and uptime-monitor configuration |

### 3.2 Benchmarks (re-run for this revision) [MEASURED-LOCAL]

| What | Result |
|---|---|
| `pd.read_csv(jobs.csv)`, 95 rows × 206 columns, 1 MB | **46 ms** median |
| `df.to_csv(...)` for the same frame | **45 ms** median |
| Vendor sanitising, raw HTML descriptions (avg 16 KB) | 5.5 ms per job |
| Vendor sanitising, HTML-stripped text (avg 1.9 KB, which is what the sync posts) | **1.34 ms per job** → 1.23 s per 1,000 |
| Admin sanitising, stripped text | 0.14 ms per job |
| pydantic `Job(**doc)` + `.dict()`, 1,000 jobs | 14 ms |
| `/api/jobs` body at 1,000 jobs, full vs slim | 2.29 MB / 405 KB gzip vs 0.58 MB / 37 KB gzip |

Not measured: bcrypt is not installed locally. A cost-12 `checkpw` is typically **≈ 150-300 ms of CPU** [ESTIMATE]. That figure is used for login-burst sizing. Reproduction steps are in Appendix A.

---

## 4. Redis: Is It Necessary?

**Verdict: not needed now, and not needed for 1,000 concurrent users on one or two app instances.** Each candidate use has a simpler, already-present or $0 alternative.

| Candidate use | What exists today [CODE] | Is Redis needed? | Better option |
|---|---|---|---|
| **Caching the job list** | Per-process cache with a TTL | **No.** The data set is a few MB. An in-process cache is faster than any network cache. The real problems are invalidation (L4) and what is cached (L1), not where it is stored. | Write-time sanitising plus a MongoDB version stamp |
| **Cross-worker invalidation** | None (L4) | **No.** One `find_one` per worker every 5 s is negligible load. | `meta.version` document |
| **Request de-duplication** | A server-side single-flight lock per worker; client-side `inflightGets` per tab ([app.js:735-762](backend/web/assets/app.js#L735-L762)); ETag/304 | **No.** Once rebuilds are cheap and change-driven, duplicate rebuilds across workers cost milliseconds. | Keep what exists |
| **Background task coordination** | MongoDB lease (`locks`) and a durable `job_notification_outbox` with a unique index ([main.py:1089](backend/main.py#L1089), [main.py:1177-1263](backend/main.py#L1177-L1263)). The logic is correct. | **No.** Volume is one digest every 5 minutes. | Keep the MongoDB lease. Run the sync as one worker container. |
| **Task queue (Celery/RQ)** | None needed | **No.** There are no long user-triggered jobs. E-mail already runs in background tasks and the digest. | — |
| **Rate limiting** | None (§7, S-3) | **No** on a single instance. | Proxy-level `limit_req` (nginx/Caddy) or `slowapi` in memory, plus a MongoDB TTL counter for per-e-mail OTP limits |
| **Sessions** | Stateless JWT | **No** | — |

**When Redis would become "Recommended Later"** (any one of these, measured):
- 3 or more app instances, **and** the 5 s version poll is measurably too slow or too costly;
- a shared rate-limit counter with more than ~200 writes/s across instances;
- a real job queue for new heavy work, such as resume parsing.

The cost then would be about **$15/month** for a small managed Redis/Valkey on DigitalOcean.

---

## 5. Resource Categorisation

### Required Now

| Resource | Why (evidence) | Cost |
|---|---|---|
| **Code fixes L1-L5** (§9, phases 1-2) | They remove the actual stalls. No hardware change does. | Engineering time only |
| **Sync worker as its own container** (same image, same droplet) | Today the script's host and supervision are unknown, and it has stuck states and silent token expiry [CODE] | $0 |
| **Request timing and structured logs** (middleware), **DigitalOcean monitoring agent**, **Atlas built-in metrics and alerts**, **a free uptime check** | There is no observability today, and every number in §6 needs to be replaced with a measured one | $0 (free tiers) |
| **Error tracking** (Sentry free tier, or equivalent) | 500s are currently only `print()`ed | $0 |
| **Atlas tier and region check.** Move off M0 if it is in use. | M0 throttles at 100 ops/s and has **no backups** | $0 if already Flex or M10; otherwise Flex ≈ $8-30/month |
| **Reverse proxy** (Caddy or nginx container) **if the app runs on a bare droplet** | TLS, real client IP for rate limits, body-size limits, static caching. Not needed on App Platform, which provides one. | $0 |
| **Docker log rotation** (`max-size`/`max-file`) | L2 plus 204 `print`s grow logs without limit | $0 |
| **Pinned dependencies**, compose healthcheck and resource limits | Unpinned deps can change behaviour on any rebuild | $0 |

### Recommended Later (only when the trigger is measured)

| Resource | Trigger | Approx. cost* |
|---|---|---|
| Larger droplet (vertical) | App CPU p95 > 70% sustained, or RAM > 75% | 2 vCPU/4 GB ≈ $24 → 4 vCPU/8 GB ≈ $48/month |
| More uvicorn workers | Workers < vCPUs and CPU is saturated | $0 |
| Atlas **M10** (dedicated, continuous backups) | More than about 300 sustained ops/s, more than 5 GB data, Flex throttling, or a need for point-in-time restore | ≈ $57/month |
| Atlas **M20** | M10 profiler shows memory or IOPS pressure | ≈ $146/month |
| Object storage for resumes (DO Spaces / S3) | GridFS larger than about 5 GB, or backup size or cost becomes significant | ≈ $5/month for 250 GB |
| Second app instance plus a load balancer | One 4 vCPU instance is saturated, or zero-downtime deploys or failover are needed | +$48 droplet + ≈ $12 LB |
| CDN (for example Cloudflare free) | Users far from the droplet region, after the static-asset fixes | $0-20/month |
| Managed Redis | §4 triggers only | ≈ $15/month |

### Not Needed

| Resource | Reason |
|---|---|
| Redis (now) | §4 |
| Celery / RQ / Kafka / RabbitMQ | The MongoDB outbox and lease already cover the queueing needs |
| Kubernetes or autoscaling groups | One droplet handles the 1,000-user estimate once the code is fixed (§6) |
| MongoDB read replicas or sharding | The working set is MBs to low GBs, and reads are indexed |
| Self-hosted Prometheus/Grafana/ELK | Free hosted tiers cover this scale; self-hosting adds a box to run |
| Server-side pagination of `/api/jobs` | Client-side filtering over a slim list is fine below about 5,000 jobs [ESTIMATE] |
| A second job source or Nexus caching proxy | Nexus is the only source, and only the sync worker calls it |
| BroadcastChannel leader-tab election | Notification polling is already visibility-gated and cheap |

\* Approximate public list prices (DigitalOcean Basic droplets, MongoDB Atlas on AWS US regions) as of this writing. Verify current pricing before purchasing.

---

## 6. Capacity and Cost Estimates

### 6.1 Assumptions [ESTIMATE]

| Parameter | Value used | Replace with |
|---|---|---|
| Requests per concurrent user | 0.10-0.15 req/s peak: page loads, 304 revalidations, detail views, submissions, plus 1 notification poll per 60 s | R-1 request logs |
| Job count N | 1,000 jobs, avg 1.9 KB stripped description (measured average) | `manual_jobs.countDocuments()` |
| CPU per warm request after the fixes | 3-8 ms of Python CPU (auth from cache, one indexed query, JSON) | R-1 timings |
| CPU per login | 150-300 ms (bcrypt cost 12) | Measure on the droplet |
| Login burst | 30% of concurrent users log in within 10 minutes | Logs |
| MongoDB ops per request | ≈ 2 before the L3 fix, ≈ 1 after | Atlas metrics |
| Worker RSS | 150-250 MB each (FastAPI, pymongo, caches ≈ 10-15 MB at N = 1,000) | `docker stats` |
| Resume size and volume | 0.3-0.5 MB avg; 20-100 submissions/day | GridFS `fs.files` stats |
| Notification docs | One per (recipient × matched new job) from the digest ([main.py:1229-1242](backend/main.py#L1229-L1242)), ≈ 0.3 KB each | `notifications` stats |

### 6.2 Sizing table (after phases 1-2 of the roadmap)

| Resource | Current (unmeasured, assume ≤ 50) | 100 concurrent | 500 concurrent | 1,000 concurrent |
|---|---|---|---|---|
| Peak requests/s | measure | 10-15 | 50-75 | 100-150 |
| Steady app CPU (rps × 3-8 ms) | < 0.1 vCPU | 0.05-0.12 vCPU | 0.15-0.6 vCPU | 0.3-1.2 vCPU |
| Login burst CPU | small | ≈ 0.02 vCPU | ≈ 0.1 vCPU | ≈ 0.25 vCPU |
| **Droplet (with ~2× headroom)** | 2 vCPU / 2-4 GB | **2 vCPU / 4 GB** | **2-4 vCPU / 4-8 GB** | **4 vCPU / 8 GB** (or 2 × 2 vCPU behind an LB for failover) |
| Uvicorn workers | 2 | 2 | 2-4 | 4 (= vCPUs) |
| App RAM used | ≈ 0.5 GB | ≈ 0.6 GB | ≈ 0.8-1.2 GB | ≈ 1.2-1.6 GB (+ 10 MB per concurrent resume upload) |
| Sync worker container | 0.25 vCPU / 256 MB | same | same | same (scales with jobs, not users) |
| MongoDB ops/s (after the L3 fix) | < 20 | 15-25 | 60-100 | 120-200 |
| **Atlas tier** | Flex (not M0) | Flex, or M10 if backups/PITR required | **M10** | **M10**; M20 only if the profiler shows pressure |
| DB connections (actual, not pool max) | < 20 | < 30 | < 60 | < 120. All tiers allow ≥ 500. Set `maxPoolSize` to 20 per worker. |
| DB storage, year 1 (excluding resumes) | < 0.5 GB | < 1 GB | 1-3 GB | 2-6 GB (notifications dominate without a TTL) |
| Resume storage, year 1 | `S × R × 365`: 20/day × 0.4 MB ≈ **3 GB**; 100/day ≈ **15 GB** | | | |
| Redis | none | none | none | none (§4) |
| Bandwidth, jobs list, first loads | negligible | < 1 GB/month | ≈ 1-3 GB/month | ≈ 2-6 GB/month (slim list, 37 KB per full load) |
| Logs (JSON, 30-day retention) | < 1 GB | 1-3 GB | 5-10 GB | 10-20 GB |

**Without the code fixes**, the same hardware still shows the periodic 1-1.5 s stall at 1,000 jobs, the 5-minute cross-worker inconsistency, and about twice the MongoDB ops. Adding hardware shortens the stall by a constant factor but does not remove it.

### 6.3 Estimated monthly cost (approximate list prices, verify before purchase)

| Scenario | Droplet(s) | Atlas | Other | **Total / month** |
|---|---|---|---|---|
| Current | 2 vCPU/2-4 GB: $18-24 | Flex $8-30, or M10 $57 | $0 (free monitoring, Sentry free, uptime free) | **≈ $26-81** |
| 100 concurrent | 2 vCPU/4 GB: $24 | Flex $8-30 or M10 $57 | $0 | **≈ $32-81** |
| 500 concurrent | 4 vCPU/8 GB: $48 | M10 $57 | Spaces $5 (optional) | **≈ $105-110** (+$89 if M20 is needed) |
| 1,000 concurrent | 4 vCPU/8 GB: $48, or 2 × 4 vCPU + LB: $108 | M10 $57 (M20 $146 if needed) | Spaces $5 | **≈ $110-260** |

The single biggest variable is the **Atlas tier**, and it is driven by storage, mostly resumes, and backup requirements, not by user count. Measure resume volume (Q-6) before choosing.

---

## 7. Other Verified Issues That Affect Reliability

These are mostly security items, but each one can also cause outages, junk data or e-mail floods, so they belong in the plan.

| ID | Issue [CODE] | Fix | Priority / Effort |
|---|---|---|---|
| S-1 | **Unauthenticated writes:** `POST /api/nexus/jobs` and `/api/nexus/jobs/bulk` ([main.py:2920-2966](backend/main.py#L2920-L2966)) create or overwrite jobs and queue e-mails. The sync script uses them when started without credentials ([api_nexus.py:1117](api_nexus.py#L1117)). | A dedicated service token (env var, constant-time compare). Accept the old and new paths during a short cutover. | Critical / S |
| S-2 | **Unauthenticated reads of PII:** `GET /api/candidates/job/{id}` ([main.py:4954](backend/main.py#L4954), and the UI calls it without a token: [app.js:1246](backend/web/assets/app.js#L1246)), `GET /api/resumes/{id}` ([main.py:5167](backend/main.py#L5167)), `GET /api/jobs/{id}` ([main.py:4704](backend/main.py#L4704)). There is also a public `/uploads` static mount ([main.py:3860](backend/main.py#L3860)), which is only populated in JSON-fallback mode. | Require auth with the vendor-owns / admin-sees-all rule. Remove the `/uploads` mount. | Critical / S |
| S-3 | **No rate limiting.** `/api/auth/request-otp` sends an e-mail per call; login runs bcrypt per attempt. | Proxy `limit_req` per IP, plus a per-e-mail OTP limit (for example 3 per 10 minutes) using a MongoDB TTL counter. Return 429 with `Retry-After`. | High / S |
| S-4 | **Stored XSS:** job fields go into `innerHTML` unescaped ([app.js:1020-1026](backend/web/assets/app.js#L1020-L1026)). Combined with S-1, anyone can inject script that runs for every user, and the JWT is in `localStorage` ([app.js:125](backend/web/assets/app.js#L125)). | Use the existing `escapeHtml`. Add a CSP in report-only mode first. | High / S |
| S-5 | CORS `allow_origins=["*"]` with credentials ([main.py:2125-2132](backend/main.py#L2125-L2132)) | Restrict to the real origin | Medium / S |
| S-6 | **Secrets in source:** a default Ceipal API key ([main.py:2155](backend/main.py#L2155)) and a default admin seed password in `backend/main.py`; a CLI comment containing a password in `api_nexus.py`; `SECRET_KEY` falls back to a placeholder ([main.py:2060](backend/main.py#L2060)). `cache/ceipal_auth_last.json`, `data/users.json`, `Users` and `jobs.csv` are tracked in git. | Rotate everything that appeared in source. Fail startup without `SECRET_KEY`. `git rm --cached` the data files. | High / S |
| S-7 | `/api/health` is public, takes 3 pings, may scan all users, and returns paths and counts ([main.py:4746-4770](backend/main.py#L4746-L4770)) | `/healthz` (no I/O) and `/readyz` (1 ping, cached for 5 s). Move diagnostics behind admin auth. | Medium / S |
| S-8 | **The Excel job path conflicts with "Nexus only".** Uploads go to an in-memory list per worker ([main.py:2166](backend/main.py#L2166)), which is lost on restart and invisible to the other worker. It is also the source of L2. | Retire it after owner sign-off (Q-5) | Medium / S |
| S-9 | **Unbounded admin lists:** `/api/candidates` and `/api/submission-history` load every matching document with no limit or projection ([main.py:3305-3316](backend/main.py#L3305-L3316)) | Pagination, projection, `$group` for history | Medium / M |
| S-10 | **MongoDB client has no socket or wait-queue timeouts** ([main.py:131-136](backend/main.py#L131-L136)). A hung socket pins a thread out of the 40-thread pool. | `socketTimeoutMS=20000`, `connectTimeoutMS=5000`, `waitQueueTimeoutMS=5000`, `maxPoolSize=20` | Medium / S |
| S-11 | `created_at` is overwritten on every upsert ([main.py:3730](backend/main.py#L3730)), so a re-post reorders the list | `$setOnInsert` plus `updated_at` | Low / S |
| S-12 | `notifications` grows by recipients × jobs with no TTL | TTL index (for example 90 days) after business sign-off | Medium / S |
| S-13 | Resumes are read fully into memory on upload and download (≤ 10 MB each) | Stream downloads. Move to object storage later (§5). | Low / M |
| S-14 | Dead code: Ceipal (~800 lines and 6 routes), `database/models.py`, the unreachable `/api/jobs/load-more` (declared after `/api/jobs/{job_id}`), and a duplicate `GET /` route ([main.py:2138](backend/main.py#L2138), [main.py:4661](backend/main.py#L4661)) | Remove after sign-off | Low / S |

---

## 8. Nexus Integration (sole job source)

### 8.1 Verified behaviour [CODE]

| Aspect | Finding |
|---|---|
| Who calls Nexus | Only `api_nexus.py`. The web app has no Nexus URL. Users and tabs **cannot** trigger Nexus calls. |
| Schedule | Full list scan every 300 s, 50 per page; a detail `GET` only for job ids not already in `jobs.csv` ([api_nexus.py:36-49](api_nexus.py#L36-L49)) |
| Duplicates | Server-side idempotent: `manual_jobs.id` is unique, and the outbox `job_id` is unique. No duplicate jobs or e-mails on a re-post. |
| **Updates and closures** | **Never propagated.** `has_job()` skips any known id ([api_nexus.py:1026](api_nexus.py#L1026)), and jobs missing from a scan are never closed. Vendors can see filled jobs and stale rates. |
| **State store** | `jobs.csv`, read and rewritten in full by pandas inside `async` functions (the event loop is blocked). `has_job` is called once per job per scan, so a scan is O(N) file reads: ≈ 46 ms × N at 95 rows, and more as the file grows [MEASURED-LOCAL]. |
| **10 s rewrite loop (new detail)** | `ready_rows()` rewrites the whole CSV whenever any row is `posted`/`posting` ([api_nexus.py:517](api_nexus.py#L517)), which means on effectively **every** 10 s cycle, forever. A crash mid-write corrupts the only copy of the state. |
| Failed rows | Retried every 10 s forever, with no backoff or cap |
| Stuck rows | Rows left in `posting` after a timeout are never retried ([api_nexus.py:870-876](api_nexus.py#L870-L876)) |
| Auth | Logs in once at start ([api_nexus.py:1114](api_nexus.py#L1114)). The JWT lasts 7 days and is never refreshed. After that every post returns 401 → the row is marked failed → it is retried every 10 s. |
| Nexus errors | No retry on list or detail calls ([api_nexus.py:91-112](api_nexus.py#L91-L112)). One bad page aborts the scan until the next cycle. There is no "last successful sync" record. |
| Where it runs | **Not in the repo.** Unknown (Q-3). |

### 8.2 Recommended design (no new infrastructure)

1. Run it as a **`worker` container from the same image** in `docker-compose.yml`, with `restart: unless-stopped`. It needs no message broker.
2. Keep sync state **in MongoDB on the `manual_jobs` documents**: `nexus_hash`, `nexus_last_seen_at`, `nexus_missing_scans`, `sync_attempts`, `sync_next_attempt_at`. Load all hashes with **one query per scan**.
3. Compute a fingerprint from the summary fields (status, billRate, openings, start/end dates, pay) so that details are fetched only for **new or changed** jobs. The summary has no `updatedAt`.
4. Write directly to MongoDB (or call an authenticated ingest endpoint with a service token). Sanitise at write time (L1) and bump `meta.version` (L4).
5. **Soft-close** jobs that are missing from a **complete, error-free** scan for 2 consecutive scans. Add a circuit breaker: abort if more than X% would close in one run. Reuse the safety pattern from `detect_and_notify_closures`. Requires business sign-off (Q-4, Q-5).
6. Use retries with exponential backoff, jitter and `Retry-After` on Nexus calls. Widen the scan interval after repeated failures. Record `last_successful_sync_at` and alert after 30 minutes without one.
7. Keep the 5-minute poll. Freshness becomes "poll interval + ≤ 5 s", down from up to 10 minutes today.

---

## 9. Multi-Tab and Multi-User Isolation

| Question | Today [CODE] | After the plan |
|---|---|---|
| Does one tab or user slow another? | **Yes, during a rebuild (L1)** on the shared worker. Otherwise no: warm requests return a cached body or a 304. | No. Rebuilds are a memory copy. |
| Does a refresh trigger global work? | Only if the worker's TTL has expired (L1). It never triggers a Nexus call. | No. Work happens only on data change. |
| Do tabs see the same data? | **Not guaranteed** for up to 300 s (L4) | Yes, within about 5 s |
| Duplicate background jobs from tabs? | No. Tabs start none. The digest is lease-guarded. | Same |
| Duplicate API calls within a tab | Deduplicated by `inflightGets`, with a 30 s TTL for candidate lists | Same |
| Polling load from many tabs | Only visible tabs poll, every 60 s | Optionally return an `unread_count` only, so each poll is tiny |
| Admin screens hurting vendors | Unbounded candidate lists run CPU-bound JSON on the same worker (S-9) | Paginated |
| Cross-tab login/logout | Not synchronised until the next API call (no `storage` listener) | Low priority: add a `storage` listener |
| Per-user data isolation | Correct: per-user queries are keyed by user id/e-mail, **except** the open endpoints in S-2 | Fixed by S-2 |

---

## 10. Already Implemented (verified, keep)

| Feature | Location |
|---|---|
| gzip for responses ≥ 1 KB | [main.py:2123](backend/main.py#L2123) |
| ETag/304 on `/api/jobs`, content-hash ETag, identical across workers | [main.py:3697](backend/main.py#L3697), [main.py:4675-4685](backend/main.py#L4675-L4685) |
| Single-flight rebuild locks (manual jobs, Excel) | [main.py:3617](backend/main.py#L3617), [main.py:3216](backend/main.py#L3216) |
| Pre-serialised per-role response cache with a generation guard | [main.py:3675-3704](backend/main.py#L3675-L3704) |
| Blocking calls (bcrypt, SendGrid, GridFS, user lookup) moved off the event loop | `asyncio.to_thread` calls throughout auth and submit |
| Bulk upsert of jobs in one round trip | [main.py:3726](backend/main.py#L3726) |
| Indexes on all hot query paths, plus a TTL index for auth tokens | [main.py:58-81](backend/main.py#L58-L81) |
| Durable digest outbox, unique per job, with a MongoDB lease (one sender per cycle) and staggered start | [main.py:1054-1275](backend/main.py#L1054-L1275) |
| Auth tokens/OTPs persisted in MongoDB (shared across workers) | `auth_tokens` collection |
| Per-tab GET de-duplication plus a short TTL cache for candidate lists | [app.js:735-762](backend/web/assets/app.js#L735-L762) |
| Visibility-gated notification polling | [app.js:2349-2371](backend/web/assets/app.js#L2349-L2371) |
| Startup MongoDB RTT measurement and warning | [main.py:150-170](backend/main.py#L150-L170) |
| VMS-side post retry with `Retry-After` in the sync script | [api_nexus.py:625-702](api_nexus.py#L625-L702) |

---

## 11. Recommended Architecture

```
 Browsers (any number of tabs)
        │  HTTPS
        ▼
 ┌──────────────────────────────┐   Caddy/nginx (or App Platform's built-in proxy)
 │ Reverse proxy                │   TLS · gzip · long cache for fingerprinted /assets
 │                              │   per-IP rate limits · body-size limit · X-Request-ID
 └──────────────┬───────────────┘
                ▼
 ┌──────────────────────────────┐   1 container, N uvicorn workers (N = vCPUs)
 │ web (FastAPI)                │   · /api/jobs: slim, pre-sanitised, ETag
 │ in-process cache per worker  │   · rebuild only when meta.version changes (poll ≤ 5 s)
 │ JSON logs + timing           │   · per-user auth cache (60 s) · /healthz /readyz
 └──────────────┬───────────────┘   · digest sender stays here (lease-guarded, light)
                ▼
 ┌──────────────────────────────────────────────────────────────┐
 │ MongoDB Atlas, same region, Flex → M10 as triggers say       │
 │ manual_jobs (+ vendor/admin text, nexus_hash, closed_at)     │
 │ meta{jobs version, last_sync_at} · locks · outbox · users    │
 │ candidates · notifications (TTL) · GridFS (→ Spaces later)   │
 └──────────────▲───────────────────────────────────────────────┘
                │ upsert changed / soft-close missing / bump version
 ┌──────────────┴───────────────┐
 │ worker (same image, own      │   one instance · backoff + circuit breaker
 │ container) — Nexus sync      │   detail fetch only for new/changed jobs
 └──────────────┬───────────────┘
                ▼
          Nexus API (sole job source; only the worker calls it)
```

**Principles:**
1. Only the worker talks to Nexus. The web tier never blocks on it.
2. Derived data (sanitised text, previews) is computed **once at write time**.
3. Workers share state through MongoDB only. No process holds the source of truth.
4. Background work is idempotent and leased, with capped retries.
5. Add infrastructure only when a measured trigger is hit.

**Unchanged:** the UI, login and OTP flows, vendor/admin roles, bill-rate display and client-name hiding rules, digest e-mail behaviour, and the data model (additive fields only).

---

## 12. Prioritised Roadmap

Impact: H/M/L. Effort for one engineer: S (< ½ day), M (1-3 days), L (≈ 1 week). Cost is infrastructure spend.

### Phase 0 — Measure (day 1, $0)
| # | Task | Impact | Effort |
|---|---|---|---|
| R-1 | Request-timing and request-ID middleware (one JSON line per request). Add timers around `get_jobs_response_payload`, `_read_manual_jobs_from_store` and `load_user_by_email`. | H (proves L1 and L3) | S |
| R-2 | Record `mongodb_ping_ms`, the Atlas tier and region, the droplet size, `nproc`, CPU steal, the job count, and where `api_nexus.py` runs | H | S |
| R-3 | Enable the DO monitoring agent, Atlas alerts, an uptime check on `/` and Sentry free tier. Add Docker log rotation. | M | S |

### Phase 1 — Quick wins (days 2-4, $0)
| # | Task | Fixes | Impact | Effort |
|---|---|---|---|---|
| Q-1 | Treat an empty Excel cache as cached (`is not None`) | L2 | H | S |
| Q-2 | Check the response cache before `get_all_jobs()`; clear the response cache only when the data changed | L1 (partial) | H | S |
| Q-3 | Per-user auth cache with a 60 s TTL; drop the full-scan fallback on a miss | L3 | H | S |
| Q-4 | Authenticate the ingest endpoints (service token) and the open read endpoints; remove `/uploads` | S-1, S-2 | H (reliability and security) | S |
| Q-5 | Escape job-card fields; CSP in report-only mode | S-4 | H | S |
| Q-6 | Rate limits on OTP, login and ingest | S-3 | M | S |
| Q-7 | Sync-script stop-gaps: no CSV rewrite unless something changed; failed-row backoff and cap; re-login on 401; `posting` lease timeout | §8 | H | S |
| Q-8 | MongoDB timeouts; `maxPoolSize=20` | S-10 | M | S |
| Q-9 | Compress the logo; `Cache-Control` on `/assets` | L6 | M | S |
| Q-10 | Rotate hard-coded secrets; require `SECRET_KEY`; untrack data files | S-6 | H (security) | S |

**Exit criteria:** R-1 data shows whether a periodic `/api/jobs` p95 spike exists; no unauthenticated write route remains; the sync script no longer rewrites the CSV every 10 s.

### Phase 2 — Core performance (weeks 2-3, $0)
| # | Task | Fixes | Impact | Effort |
|---|---|---|---|---|
| P-1 | Sanitise at write time (store vendor and admin text plus a 260-character preview); one backfill script; golden-file test proving byte-identical output per role | L1 | H | M |
| P-2 | `meta.version` cross-worker coherence; rebuild only on change | L4 | H | M |
| P-3 | Slim `/api/jobs`; lazy full description in the modal | L5 | H at N ≥ 300 | M |
| P-4 | Frontend: precompute search fields, chunked rendering, event delegation, error/retry state | L6 | M | M |
| P-5 | `$setOnInsert` for `created_at`; TTL on `notifications` | S-11, S-12 | L-M | S |
| P-6 | Split `/healthz` and `/readyz`; pin dependencies; compose healthcheck and limits | S-7 | M | S |

**Exit criteria:** no periodic latency spike; workers agree within 5 s of a change; list payload ≤ 50 KB gzip per 1,000 jobs.

### Phase 3 — Reliability (weeks 4-5, $0)
| # | Task | Impact | Effort |
|---|---|---|---|
| F-1 | Rewrite the sync on MongoDB as its own container: hashing, updates, guarded soft-closes, backoff, freshness alert (§8.2) | H | L |
| F-2 | Paginate the candidate and submission-history endpoints | M | M |
| F-3 | Confirm backups, run a restore drill into a scratch cluster, write a deploy/rollback runbook (image tags by git SHA) | H | M |
| F-4 | Retire the Excel path, Ceipal code, `database/models.py` and the duplicate routes (after Q-5 sign-off) | M (less to run and secure) | S |

### Phase 4 — Scale (only on measured triggers; cost per §6.3)
| Trigger | Action |
|---|---|
| CPU p95 > 70% sustained | Raise workers to the vCPU count, then a 4 vCPU/8 GB droplet |
| Atlas throttling, > 5 GB, or a need for point-in-time restore | Atlas M10 |
| GridFS > ~5 GB | Resumes to Spaces/S3; stream downloads |
| Need for failover or one instance saturated | Second instance plus a load balancer (the design is already stateless) |
| §4 triggers | Redis |

### Validation (run after each phase)
- **Load:** Locust scenarios at 100, 500 and 1,000 virtual users, each held ≥ 10 minutes (longer than two cache TTLs). Mix: login, `/api/jobs` (cold, warm, 304), job detail, notifications, candidate list, submit.
- **Pass if:** `/api/jobs` warm p95 < 300 ms with no periodic spike > 2× p50; 5xx < 1%; CPU < 75%; all workers return the same ETag within 10 s of a change.
- **Multi-tab:** Playwright with 5 tabs per user. Opening tabs causes no extra rebuilds (log counter) and **zero** Nexus calls.
- **Nexus faults:** a mock returning 429, 500, timeouts, malformed JSON and an empty list. Expect bounded retries, the site still serving, and **no jobs closed** on partial or empty data.
- **Browser:** Lighthouse and the Performance panel at 1,000 jobs. No long task > 100 ms while typing.
- **Soak:** 4-8 h. Worker RSS plateaus.

---

## 13. Expected Benefits and Trade-Offs

| Change | Benefit | Trade-off |
|---|---|---|
| Q-1, Q-2, Q-3 | Removes per-request lock contention, log spam and most MongoDB lookups, in hours of work | None of note |
| Write-time sanitising (P-1) | Removes ~1.2 s of CPU per rebuild per 1,000 jobs | A backfill is needed; a rule change requires reprocessing (add a `sanitize_version` field) |
| Version stamp (P-2) | Consistent data across tabs and workers | One tiny query per worker every 5 s |
| Slim list (P-3) | ≈ 11× less transfer, ≈ 4× less parse | One extra small request when the modal opens |
| Sync rewrite (F-1) | Fresh, correct jobs; no CSV fragility | Closures are a behaviour change that needs business sign-off |
| No Redis/Celery | Lower cost and fewer moving parts | Revisit only on the §4 triggers |

---

## 14. Open Questions

| # | Question | Affects |
|---|---|---|
| Q-1 | Droplet or App Platform size and region; Atlas tier and region | §6, L7 |
| Q-2 | Real job count, active users, peak concurrency | Every [ESTIMATE] |
| Q-3 | Where and how `api_nexus.py` runs today, and with which credentials | §8, S-1 |
| Q-4 | What Nexus returns for filled or closed jobs; its rate limits | §8.2 step 5 |
| Q-5 | Can the Excel path and Ceipal code be retired? Should closed jobs disappear for vendors? | S-8, S-14, F-1 |
| Q-6 | Resume count and size per month; retention policy | Atlas tier, object storage |
| Q-7 | Is anything polling `/api/health`, and how often? | S-7, L3 |
| Q-8 | Required freshness window and downtime tolerance | SLOs, poll interval |

---

## 15. Changes from Revision 1

| Change | Reason |
|---|---|
| **Added** L2 (the Excel loader running on every request) | New verified finding |
| **Added** the full-collection scan on a user-lookup miss (L3) | New verified finding |
| **Corrected** "CSV rewritten on retries" to "CSV rewritten on effectively every 10 s cycle" | `ready_rows` rewrite condition ([api_nexus.py:517](api_nexus.py#L517)) |
| **Re-measured** the sanitise cost on HTML-**stripped** text (1.34 ms/job, not 6 ms/job raw HTML), which is what production stores | More representative |
| **Added** the Redis evaluation, resource categories, $ cost table, and an ops/s and tier analysis (M0/Flex throttling) | Requested scope |
| **Merged** the duplicated issue descriptions (the old §3, §7, §14 and §16 repeated each item) into one table per area | Removes duplication |
| **Removed** self-hosted Prometheus/Grafana, toxiproxy, memray, read replicas, a `BroadcastChannel` leader tab, `HttpOnly`-cookie migration, and server-side job pagination | Not justified at this scale, or no evidence of need |
| **Removed** proposed SLO targets beyond the load-test pass criteria | Need a real baseline first (R-1) |

---

### Appendix A — Reproducing the measurements
1. CSV: `pd.read_csv('jobs.csv', dtype=str, keep_default_na=False)` 10 times (median), and `df.to_csv(..., encoding='utf-8-sig')` 5 times.
2. Sanitising: take `COMPENSATION_PATTERN`, `CLIENT_NAME_PATTERNS`, `hide_compensation_details` and `sanitize_job_description` verbatim from `backend/main.py`, and `strip_html_text` from `api_nexus.py`. Apply them to the 56 non-empty `detail.jobDescription` values, both raw and stripped.
3. Scale: 1,000 synthetic jobs sampled from the stripped descriptions. Measure the full vs slim JSON and gzip sizes, and the vendor sanitise time.
4. pydantic: build 1,000 `Job` models with the same fields as [main.py:2174-2190](backend/main.py#L2174-L2190) and call `.dict()`.
5. These ran on a laptop. Re-run on the droplet for production numbers.
