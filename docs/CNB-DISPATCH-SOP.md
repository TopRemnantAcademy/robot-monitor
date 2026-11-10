# CNB NPC Dispatch Standard Operating Procedure (SOP)

> This document specifies **how to assign tasks to CNB's @codebuddy**, and **how to tell whether it actually finished**.
> All items come from this project's own verification (2026-10-09 ~ 10), not speculation. Items not empirically verified are marked "unverified".

---

## 0. Three Dispatch Channels

| Channel | Trigger Method | Applicable Scenario | Injects `CNB_NPC_TRIGGER_CONTENT`? |
|---|---|---|---|
| **A. Issue/PR comment** (primary) | A comment containing `@codebuddy` | A single concrete task | ✅ Yes |
| **B. crontab schedule** | The `crontab` stage in `.cnb.yml` | Periodic inspection | ❌ No |
| **C. Designated role** | `@flow.237/RobotMonitor(role name)` | Needs a specific expert perspective | ✅ Yes |

**This project only uses Channel A.** See §5.3 for the pitfalls of Channel B.

---

## 1. Standard Dispatch Flow (7 steps)

### Step 1 — Confirm Baseline Commit
```bash
git log --oneline -1          # note the sha, write it into the dispatch body
```
The dispatch body must state "based on `main @ <sha>`", otherwise the NPC may start from old code.

### Step 2 — Write the Requirement as a File First
**Forbidden** to stuff long requirements directly into `--body`. Always write `orders/NN-dispatch.md` first then use `--body-file`. Reason: archivable, reusable, diffable, and avoids shell escaping issues.

### Step 3 — Create Issue (**body must not contain the trigger word**)
```bash
cnb issues create-issue \
  --repo flow.237/RobotMonitor \
  --title "NN — <one-line task>" \
  --body-file orders/NN-dispatch.md
```

> ⚠️ **Verified trap**: If the `create-issue` body contains `@codebuddy`, CNB will **immediately trigger a build**; then a later `post-issue-comment` triggers a second one → two NPCs editing the same batch of files, causing conflicts and duplicate submissions. (This actually happened in this project's Order 07; one was stopped with `stop-build`.)
>
> **Rule: the create-issue body must not contain the trigger word; triggering happens only once at Step 4.**

### Step 4 — Trigger NPC via Comment (**must include `--work-mode`**)
```bash
cnb issues post-issue-comment \
  --repo flow.237/RobotMonitor \
  --number <issue number> \
  --work-mode \
  --body "@codebuddy please pick up and execute Order NN. <one-line key point>. Full requirements in orders/NN-dispatch.md."
```

`--work-mode` **cannot be omitted**. Verified: without it the NPC gets `CNB_NPC_ENABLE_WORKMODE=false`, cannot write to the repo, and ultimately only produces analysis without landing anything.

> ⚠️ **Verified trap (2026-10-11 Order 19)**: `--body` **must actually contain the written `@codebuddy`**. When only `--work-mode` is present but the body lacks the trigger word, the API still returns `201` and the comment appears normally in the issue, **but no build is ever launched** — the failure is completely invisible from the return code. After dispatching, you must immediately run Step 5 (check `get-build-logs`), not wait until you remember.

### Step 5 — Immediately Verify `workMode`
```bash
cnb build get-build-status --repo flow.237/RobotMonitor --sn <sn>
```
Confirm the output contains:
```
npc:
  name: CodeBuddy
  match: @codebuddy
  workMode: true          # ← must be true
```
`workMode: false` means Step 4 missed a parameter — a wasted run.

### Step 6 — Wait for Build to Finish, Check Status
```bash
cnb build get-build-status --repo flow.237/RobotMonitor --sn <sn>
# status: pending → success / error
```
A single NPC stage took **about 12 minutes** in practice (Order 05: 716 seconds).

### Step 7 — ⭐ Determine "Truly Done" (**most critical, don't trust status**)

`status: success` **does not equal** code merged into main. In practice Order 05 was success but `main` did not move at all.

You must apply this criterion:

```bash
# 7-1 see if there is a new branch remotely (NPC cannot push main, only branches)
git -c http.extraHeader="Authorization: Bearer ${TOKEN}" ls-remote --heads cnb

# 7-2 new branch → fetch + merge
git -c http.extraHeader="Authorization: Bearer ${TOKEN}" fetch cnb <branch>
git merge <sha>

# 7-3 then check if main actually advanced
git log --oneline -3
```

> **Rule of thumb: success but no new commit = output is on a branch.**

---

## 2. 8 Elements the Dispatch Body Must Contain

Missing any one makes it easy for the NPC to freelance or falsely pass:

| # | Element | Role |
|---|---|---|
| 1 | **Baseline commit sha** | Prevent starting from old code |
| 2 | **Required-reading file list** (with paths) | Prevent contract mismatch from skipped reading |
| 3 | **Task breakdown** (T1/T2…) | Acceptable item by item |
| 4 | **Acceptance criteria** (judgeable `- [ ]`) | Prevent "I think it's done" |
| 5 | **Prohibited items** | Prevent accidentally breaking other places |
| 6 | **Delivery method** (branch name / open PR or not) | Easy to recover |
| 7 | **Failure-reporting requirement** (forbid masking 403 with "already pushed") | Prevent silent failure |
| 8 | **One-line role positioning** (e.g. "you are the acceptor, evidence first") | Constrain role behavior |

### Minimal Usable Template

```markdown
@codebuddy please pick up and execute Order NN (<task name>).

## 0. Current Status
Baseline commit: main @ <sha>
Completed: <list prerequisite orders>

## 1. Required Reading
| Order | File | Purpose |

## 2. Task Breakdown
### T1 — ...

## 3. Acceptance Criteria
- [ ] ...

## 4. Prohibited Items
1. Forbidden to use "no log errors" instead of a pass criterion
2. Forbidden to fabricate numbers; if it can't run, write "unverified + required conditions"
3. Forbidden to claim done just by pushing a branch; push failure must be reported verbatim

## 5. Delivery Method
1. Branch feat/order-NN-<slug>
2. Attempt push + open PR
3. If push fails → write the diff and conclusion into the issue comment
```

---

## 3. Token and Push (CNB Special Requirements)

### Get Token
```bash
TOKEN=$(python -c "import json,os;\
p=os.path.expanduser('~/.cnb/token');\
print(json.load(open(p))['auths']['cnb.cool']['access_token'])")
```

### Push (**must use Bearer header**)
```bash
git -c http.extraHeader="Authorization: Bearer ${TOKEN}" push cnb main
```

> ⚠️ **Verified**: The following two forms both **hang indefinitely** (not slow, but hang):
> - URL-embedded token `https://user:TOKEN@cnb.cool/...`
> - `cnb git-credential` helper (returns `unknown host: cnb.cool`, exit 2)
>
> Also note: `git ls-remote` (GET) works normally, but `git push` (POST git-receive-pack) hangs — easily misjudged as "permission issue", but it is actually a protocol issue.

---

## 4. Failure Diagnosis Table

| Symptom | Root Cause | Handling |
|---|---|---|
| Build log `403 ... repo-code:rw` | NPC token lacks write-repo scope | NPC cannot push to main → **main agent merges branch manually** |
| `402 [EXCEEDING_LIMIT] AI credits exhausted` | Account-level quota depleted | Needs user to top up, not a code issue |
| `workMode: false` | Dispatch missed `--work-mode` | Re-dispatch with the parameter |
| **2 builds** from one dispatch | create-issue body contained the trigger word | `cnb build stop-build --sn <duplicate sn>` |
| Comment returns `201` but **no build at all** | Trigger comment **missed the trigger word** | Post another comment containing `@codebuddy` (stepped on in 2026-10-11 Order 19) |
| `status: success` but main didn't move | NPC only pushed a branch | Follow §1 Step 7 |
| crontab stage exits instantly `process.exit(1)` | crontab injects no trigger content, `system_prompt` or `user_prompt` empty | Both prompts must be **inline and non-empty** |
| `[CONFIG_YAML_EMPTY]` | CI config placed in wrong location | Must be `.cnb.yml` at the **repo root**, not `.cnb/.yml` |

---

## 5. Dispatch Discipline (violation = rework)

1. **One order per dispatch**: Do not append a second task in the same issue, to avoid context pollution.
2. **No duplicate triggering**: create-issue must not contain the trigger word (see §1 Step 3).
3. **Conflict prediction**: If two orders change the same file (e.g. both change `requirements.txt`), state in the later order "the one that includes both is authoritative", see `orders/08-dispatch.md` §6.
4. **Do not relax thresholds just to pass**: e.g. 30,000 rows must not be changed to 300.
5. **Distinguish environment errors from code errors**: see `docs/requirements/R-07-integration-and-stress.md` §4.
6. **NPC output must be accepted by the main agent**: after merging, run at least one compile/import verification.
7. **Must re-verify the dispatch order itself before dispatching** (verified 2026-10-11 Order 15): If the dispatch order was written and then dispatched several rounds later, the `file:line`, dependent order numbers, and column names inside will rot. This re-verification found 4 stale entries — line numbers shifted overall (after `/metrics` wiring), referenced a pre-renumber order number (13→17), wrote an audit column name that doesn't exist (`operator`), and **missed `/metrics` auth exemption** (following the original would block Prometheus scraping at the door, breaking the just-wired observability entirely). → **Re-verification checklist**: ① line numbers ② order number / renumber table ③ whether column names exist ④ whether there are "endpoints added only after dispatch" that need to be included in constraints (especially the auth-exemption allowlist).

---

## 6. This Project's Dispatch Ledger

| Order | Issue | Build sn | Status | Notes |
|---|---|---|---|---|
| 05 Frontend large screen | #5 | `cnb-ft3-1k4hf69oe` | ✅ success → manually merged | 34 files/6696 lines; `tsc` 0 errors, `vite build` passes |
| 06 Integration/stress test | #6 | `cnb-q81-1k4hgt1ee` | ⏳ pending | Closure order |
| 07 aiomqtt contract (I-1/I-2/I-3) | #8 | `cnb-s51-1k4hhvjem` | ⏳ pending | One duplicate build already stopped |
| 08 asyncpg upgrade (I-6) | #9 | `cnb-ijj-1k4himq2k` | ⏳ pending | Changes `requirements.txt` same as 07, conflict handling noted |
| 09 Frontend bundle slimming | #10 | `cnb-ua1-1k4hj5d3h` | ✅ recovered & verified | 1209.34→671.23 kB (gzip 401.05→224.79) |
| 10 Alert inhibition (I-5) | #13 | — | ✅ recovered & verified | `alert_engine` 11/11 tests passed |
| 12 CNB self-downloads upstream sources | #15 | — | ✅ recovered | Overturned my I-1 conclusion, see `external/ORDER12-VERIFICATION.md` |
| 13 Observability wiring + 6-branch convergence | #33 | `cnb-f0b-1k4j8osuu` | ✅ recovered & verified | Produced `feat/order-13` @ `911b667`; recovered by path; its alert `main.py`/`config.py` judged as regression and discarded; added the `alerts_suppressed` the NPC missed |
| 19 `robot_alert` identity + ack field | #34 | `cnb-38h-1k4jdbsm4` | ✅ recovered & reverse-verified | Produced `auto/alert-identity-759f` @ `d27b972`; tests 25→29; NPC found SQLAlchemy puts BIGSERIAL first in the composite PK, fixed with explicit `PrimaryKeyConstraint` |
| 15 Auth and RBAC (fastapi-users) | #36 | `cnb-e2k-1k4jf5tfo` | ⏳ pending | Baseline `main@58bafb5`; workMode=true verified. **Pre-dispatch fix**: original line numbers stale, order number not renumbered, audit column name wrong (`operator`→actual `ack_by`), missed `/metrics` auth exemption |

> ⚠️ **Issue numbers and order numbers are misaligned and jump wildly**: #6=06, **#8=07, #9=08, #10=09, #13=10, #15=12, #33=13, #34=19**. Intermediate numbers were taken by other issues. **Be sure to record the actual number after dispatch**, as all later branch recovery and added intelligence depend on it.

### 2026-10-11 Batch Recovery Ledger (remote once accumulated to 19 branches)

Recovered 12 branches at once; the main agent landed them in batches after **local verification**:

| Branch | Disposition | Basis |
|---|---|---|
| `feat/order-06-integration` | ✅ take all (only `reports/`) | Pure evidence files, zero code |
| `auto/upstream-sources-3ec1`, `codebuddy/external-sources-download` | ✅ take all | Pure docs |
| `auto/ui-alert-render-997e` | ✅ take all | Frontend alert row incremental render; `tsc` 0 errors |
| `feat/ops-observability-layer` | ✅ take all | +13448 lines are 9 Grafana dashboard JSON, not junk |
| `feat/order-10-alert-inhibition` | ✅ take all | Own order, 11/11 tests pass |
| `auto/db-integration-copy` | ✅ take all | Introduced JSONB type fix and three-tier degradation chain |
| `fix/infra-image-facts-and-contract-drift` | ⚠️ partial | Take its EMQX retraction and `init.sql` contract insight; **reject `db/models.py`** (using `sqlalchemy.JSON` would regress the JSONB fix) |
| `fix/issue19-merge-safe` | ⚠️ only added files | Its `batch_writer.py` **would delete the entire three-tier degradation chain** |
| `codebuddy/observability-direct-wiring` | ⚠️ only added files | Its `main.py` would regress `/healthz` semantics |
| `feat/order-08-asyncpg-upgrade` | ⚠️ only take `pool.py` + single-line dependency | Its `requirements.txt` would roll aiomqtt back from 2.5.1 to 2.0.1 |
| `cb/issue19-github-rescan`, `docs/detail-gap-reassessment`, `feat/enterprise-gap-analysis-issue19` | ⏳ handed to Order 13 | Three parties simultaneously appending `STATUS.md`/`external/*.md`, need union merge |

### The Right Way to Add Follow-up Intelligence

After dispatching, if you discover a key fact, you can **append a comment** to supplement — but the body **must not contain `@codebuddy`**, otherwise it triggers another build. Verified: after Order 09 was dispatched, a comment about echarts reference scope was appended (no trigger word), and the build count stayed at 1.

---

## 6.5 Batch Recovery Discipline (2026-10-11, summary of recovering 12 branches at once)

Parallel dispatching makes the remote accumulate a dozen branches within hours. When recovering, **merging all at once is forbidden**; you must dispose in the following order.

### Step 1: Test each branch's "baseline generation"

```bash
for b in <branch>...; do
  base=$(git merge-base main cnb/$b)
  echo "$b : contains order07=$(git merge-base --is-ancestor <sha7> cnb/$b && echo Y||echo N) \
        contains order09=$(git merge-base --is-ancestor <sha9> cnb/$b && echo Y||echo N)"
done
```

**Branches based on an old generation hide already-fixed old bugs in their "unchanged" files.** Verified: `feat/order-08`'s `batch_writer.py` lacks the `_STOP` sentinel, and `requirements.txt` says `aiomqtt==2.0.1` — a direct checkout would silently regress Order 07.

### Step 2: Distinguish "added files" from "modified files"

```bash
git diff --name-status --diff-filter=A $base cnb/$b   # added → can take directly
git diff --name-status --diff-filter=M $base cnb/$b   # modified → must review each
```

**Added files have zero regression risk and can be batch-checked-out `git checkout cnb/$b -- <path>`. Modified files are where all the risk lies.**

### Step 3: For each modified file, judge "regression or improvement"

Judgment standard: **what did the other side delete relative to `main`**. Deletion is far more dangerous than addition. Three real cases verified:

| Branch change | Judgment | Basis |
|---|---|---|
| Deleted the three-tier degradation chain (L1/L2/L3) | ❌ reject | It is the fault-tolerance core of `spec/01 §4.2` |
| `/healthz` degraded from reachability-dependent to hardcoded `status:ok` | ❌ reject | Loses `db`/`retries`/`rejected` |
| `primary_key=True` → completely no primary key | ❌ reject (and **won't run**) | SQLAlchemy throws `ArgumentError` |

### Step 4: Merge dry-run, quantify conflict surface

```bash
git checkout -b tmp/probe main
for b in ...; do
  git merge --no-commit --no-ff cnb/$b -m x >/dev/null 2>&1
  echo "$b conflicts=$(git diff --name-only --diff-filter=U | wc -l)"
  git diff --name-only --diff-filter=U
  git merge --abort
done
```

⚠️ **Before the dry-run, make sure you are not on `tmp/probe`**, otherwise the final `git branch -D tmp/probe` fails and subsequent commits all land in the temp branch (this actually happened this time, causing an empty push).

### Step 5: For unautomatable merges, separate "judgment" from "execution"

The main agent is responsible for **judgment** (what to keep, what will regress, why), writes the judgment result as an **explicit retention-rule table** in the dispatch order, and hands it to CNB to **execute** the merge and verification. Don't hand-craft a dozen conflict files yourself — error-prone and not reproducible.

### Step 6: To find "branch-unique contribution" use a **three-dot** diff, not two-dot

```bash
git diff main...cnb/$b        # ← correct: merge-base → branch, i.e. the branch's own changes
git diff main      cnb/$b     # ← wrong: treats things main has but branch doesn't as "branch deleted"
```

The two-dot diff's `D` mostly shows "main has it, branch doesn't", not that the branch really deleted something. Only the three-dot diff gets the branch's **truly added** content (verified second round 2026-10-11: of 22 branches, only 5 had truly valuable unique contributions).

### Step 7: Even for "pure additions", first verify it is a superset relative to main

A three-dot diff showing `+N -0` **does not mean** you can check out the whole file. Branches are often old versions:

```bash
git diff --numstat main cnb/$b -- <file>     # look at deletion count; if not 0, can't take whole file
```

Verified: `cb/issue19-github-rescan`'s `external/SOURCES.md` looks like `+138 -0` in three-dot, but `+143 -101` in two-dot — taking the whole file would delete the "§1.1 retraction" correction added after main. The correct approach is to **extract only the branch-unique paragraphs** and append them.

⚠️ **Incidentally check for section-number conflicts**: both sides have `## 5.` but with different content (main's §5 = fourth batch, branch's §5 = fifth batch); appending directly would produce two §5s. When extracting, **renumber** according to the target file's actual numbering before appending.

### Step 8: After recovery, always run two repo-wide scans

```bash
# ① fake-coverage scan: test_*.py with no test_* function = pytest collects 0 but silently "passes"
#    (this repo has stepped on this twice: alert_engine/tests/test_notifier.py,
#     backend_ingestion/tests/test_db_integration.py)
python - <<'PY'
import pathlib, re
for p in pathlib.Path(".").rglob("test_*.py"):
    if "external" in p.parts: continue
    txt = p.read_text(encoding="utf-8", errors="ignore")
    if not re.search(r'^\s*(async\s+)?def\s+test_', txt, re.M):
        print("FAKE-COVERAGE:", p)
PY

# ② ensure repo has pytest config, otherwise root pytest collects external/ third-party sources
#    (this repo previously measured 61 collection errors and aborted, its own results completely invisible)
python -m pytest -q        # after adding pytest.ini: 29 passed

# ③ "downloads done?" cannot be asserted from the repo —— external/* excluded by .gitignore,
#    sources are not committed, so "22 downloaded" cannot be found in the repo.
#    must compare fetch-sources.sh's declared list against disk reality:
python - <<'PY'
import re, pathlib
block = re.search(r'SOURCES=\((.*?)\)',
    pathlib.Path("external/fetch-sources.sh").read_text(encoding="utf-8"), re.S).group(1)
declared = set(re.findall(r'^\s*"([A-Za-z0-9._-]+)\|', block, re.M)) | {"alertmanager"}
actual = {p.name for p in pathlib.Path("external").iterdir() if (p/".git").exists()}
print("missing:", sorted(declared - actual), " extra:", sorted(actual - declared))
PY
#    verified (2026-10-11): declared 22 / actual 14, missing 8 —— earlier "22 downloaded" was wrong.
```

⚠️ **Regex trap**: using `"([^|]+)\|` would swallow the closing quote of the previous line, producing dirty names like `'\n  "aiomqtt'`. You must use `^\s*"([A-Za-z0-9._-]+)\|` with `re.M`.

---

## 7. Related File Index

| File | Content |
|---|---|
| `.cnb.yml` | CI/NPC pipeline definition (**must be at repo root**) |
| `.cnb/settings.yml` | 10 NPC role definitions |
| `AGENTS.md` | Engineering discipline auto-injected into every NPC run |
| `orders/NN-dispatch.md` | Each order's dispatch body |
| `orders/NN-<name>.md` | Each order's original task definition |
| `docs/requirements/R-*.md` | Acceptance specs |
| `external/INTEGRATION-PLAN.md` | Upstream OSS integration plan |
| `external/SOURCES.md` | Download-source provenance |
