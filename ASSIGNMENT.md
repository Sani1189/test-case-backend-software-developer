# TrackBox Technical Challenge: Automated Pitch Boundary & Crop Engine

Welcome to the TrackBox Software / Backend Developer Challenge!

At TrackBox, we build production pipelines that turn AI/ML models into observable, scalable match-analysis engines.
This task simulates a real initiative: taking an experimental video-analysis prototype script and turning it into
something that could actually run as part of our platform — reliably, efficiently, and in a way that keeps the rest
of the system informed about what it's doing.

The scenario, code, and data here are synthetic — built to mirror the real engineering problem without using any of
our actual production code or models.

**Expected effort:** this is designed to take a focused 4–6 hours. We'd rather see a well-reasoned, partial solution
with clear notes on what you skipped and why, than a rushed, complete one. Use the `DECISIONS.md` (see below) to
tell us where you stopped and what you'd do next with more time.

---

## 🚨 Mandatory Git Workflow Requirement (read carefully)

Your development process and git discipline matter to us as much as your final code.

**Commit history will be evaluated.** A submission that lands as a single commit, or as one clean burst pushed all
at once, will be discarded without review.

Submit a link to a private GitHub/GitLab repository and invite **laurens.vandamme@trackbox.be** as a reviewer.

---

## System Overview & Starter Code

The starter repository contains:

- `synthetic_generator.py` — produces the synthetic match feed (ported from a camera vendor's own sample code), with
  common broadcast artifacts included — camera cuts, close-ups where no pitch is visible, and momentary detection
  noise, the same way a real feed would have them. This is the input your pipeline consumes, not something you're
  building — your work starts from the feed it produces.
- `synthetic_field_prototype.py` — a naive prototype script that takes that feed, infers playing-field boundary
  polygons from a mock segmentation mask frame by frame, computes a spatial metric per detected boundary, and
  reports basic execution metrics.
- `mock_api/` — a small, already-working reporting service your solution will need to talk to (see Part 4 below).
  You don't need to modify it.
- `docker-compose.yml` — brings up `mock_api`; add your own service to it once your solution is containerized.

This is real prototype code in the sense that matters: it's exactly the kind of first pass a research engineer
writes to validate an idea quickly, not code written with production concerns in mind. We're not going to tell you
what's wrong with it. Read it, run it, and use your own judgment about what would concern you if this had to run
continuously, on long video feeds, unattended, for a long time.

---

## Core Assignment

### Part 1 — Architecture & Configuration

1. **Separate the core logic from the script that runs it.** Split this into a reusable library and a thin
   execution entry point. How you draw that boundary, and what you name the pieces, is up to you.
2. **Replace the raw configuration dictionary with a validated configuration model.** Bad, unexpected, or unparsable
   configuration must cause the application to fail immediately at load time, with a clear error message — not fail
   partway through a run, and not silently fall back to a default.
3. **The field-detection mechanism will vary by sport and by deployment context.** The pipeline code should not be
   tightly coupled to any single detection implementation. Find the one seam that actually needs to flex, and design
   for that — we're not looking for a plugin system on every class.

### Part 2 — Processing Efficiency

This pipeline needs to run against long, continuous video feeds. Processing time must scale with how much of the
video you actually need to look at, not with the total length of the file — a video twice as long, analyzed the same
way, should not automatically take twice as long to process. Apply the same discipline to any other expensive
calculation your solution performs more than once.

### Part 3 — Failure Handling & Observability

1. **This pipeline will run unattended, in a batch environment, with no one watching its console.** Whoever operates
   it needs to be able to monitor its progress and diagnose a failure after the fact, without attaching a debugger.
2. **Not every failure is the same.** Decide which failures should stop the pipeline immediately and which
   shouldn't, and make sure a real failure is never silently absorbed.
3. **Real video feeds are noisy.** Your pipeline must handle frames where the pitch boundary is missing, obscured,
   or invalid without crashing or polluting reported metrics with garbage data. Decide how your solution aggregates
   valid detections into a final output while handling the invalid ones.

### Part 4 — Reporting to the Platform

In production, this pipeline doesn't run standalone — an orchestrator needs to know how a run is going and whether
it finished successfully, without reading its console output.

1. **Your solution must run inside a Docker container** (see `docker-compose.yml`) **and report its progress and
   outcome to the reporting service included in `mock_api/`, over the network** — not via a shared file or database.
   See `mock_api/app.py` for the endpoints and the shape of data it expects.
2. A failure to reach that service is not the same as a failure in the video pipeline itself. Decide how your
   solution should behave in each case, and make sure one doesn't silently turn into the other.
3. Whatever you send over the wire should be built from the same kind of validated data model you used for
   configuration — not a loose dict assembled ad hoc.

---

## Your `DECISIONS.md`

There's no single correct way to resolve every trade-off in this system. Include a `DECISIONS.md` (or a section in
your `README.md`) addressing:

1. **Assumptions & open questions.** What did you assume about video stability, stream errors, or a missing field
   boundary? What would you have asked the product/ML team before taking this to production?
2. **Validation strictness vs. fallback.** Where did you fail fast, where (if anywhere) did you allow a sensible
   fallback, and why?
3. **Performance trade-offs.** How did you balance processing throughput against detection accuracy?
4. **AI/LLM disclosure.** Tell us if and how you used an LLM (Claude, ChatGPT, Copilot, etc.) during the exercise —
   what you prompted for, and what you wrote or changed by hand. This isn't a trick question; using an LLM well is
   part of the job. We want to know how you used it.

---

## Evaluation Criteria

We evaluate your submission on four pillars:

- **Software Architecture & Design** — how you structure, decouple, and validate the system.
- **System Efficiency** — how well your solution scales to long-running, real-world inputs.
- **Production Readiness & Resilience** — how observable the system is once deployed, and how it behaves when
  something goes wrong.
- **Git Workflow** — how your commit history reflects your development process.
