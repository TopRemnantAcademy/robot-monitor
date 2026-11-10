# Verifiability Tiers of Deliverables (CodeBuddy Self-Discipline)

> Background: this repo has repeatedly seen "the NPC reports success, but the branch wasn't merged /
> the config was fabricated". `docs/CNB-DISPATCH-SOP.md` already gives the reclaim process; this document
> adds the **verifiability tiers of the deliverable itself**, to judge which conclusions in a PR can
> be taken at face value.

## Three tiers

| Tier | Meaning | Example |
|---|---|---|
| **L1 Verified** | Ran a command locally / read program output, reproducible | `python3 -c "json.load(...)"` passes; `git rev-parse HEAD` gets the commit |
| **L2 Static evidence** | Cites a specific upstream **source/doc file:line**, not executed | `emqx_prometheus_api.erl:63` proves the endpoint path |
| **L3 To-be-verified** | Only reasoning, or needs Docker/image/real hardware to verify | "Grafana can connect to TimescaleDB" |

**Rule**: L3 must be called out separately, and must state "what is still needed to reach L1/L2".

## Two known traps in this repo

### Trap 1: `--depth 1` pins "the current HEAD", not a version

Evidence: after a bare clone, `external/aiomqtt`'s HEAD had drifted to `3.0.0-alpha.1`, where
`docs/reconnection.md` **does not exist** (merged into `guides.md`) — causing the I-1/I-2 file:line
evidence to point at a file that doesn't exist.

→ To check a **pinned version**'s API, you must `--branch <tag>` or `checkout <tag>`.

### Trap 2: A bare image name resolves to **something else**

Evidence: `emqx:5.6` and `emqx/emqx:5.6` have completely different digests
(`sha256:06f2341d…` vs `sha256:c5a4118b…`); the former is not an MQTT broker at all.

→ Before adding any image, first run `docker manifest inspect`. The 6 ops images added this time
**have not done this step** and are listed as L3 in the PR.
