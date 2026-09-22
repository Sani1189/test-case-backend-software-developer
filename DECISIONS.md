# Decisions

Where I stopped, what I assumed, and what I would ask before this went near production.

Design reasoning lives in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). This document
is about the trade-offs: what I chose, what it cost, and what I deliberately did not do.

Every performance number below was measured on this machine against the shipped
1800-frame fixture, not estimated.

---

## 1. Assumptions and open questions

### Assumed

- **The input is a local file.** The assignment says the feed generator is the input and
  that my work starts from the feed it produces, so the pipeline consumes a path rather
  than producing one. In production this is more likely an object-store key or a live
  stream; `video.py` is the only module that would change, because everything downstream
  sees an iterator of frames.
- **Container metadata is approximately right.** Frame rate and frame count are read from
  the container and trusted to within 2% (see §2).
- **Resolution is constant within a feed.** A mid-stream change stops the run rather than
  being absorbed, because every metric is in pixel space and mixing coordinate spaces
  would silently invalidate all of them.
- **The spatial metric is what the prototype meant by one.** The area of the detected
  boundary intersected with the frame rectangle. I kept it, computed once, rather than
  inventing a different metric.
- **`confidence` is a proxy, not a score.** A colour-threshold detector has no calibrated
  confidence, so it reports mask coverage. It is named and documented as a proxy instead
  of being dressed up as a model probability.

### Would ask the product / ML team

1. **Is sampling acceptable?** Analysing at 5fps rather than 30fps is the single biggest
   performance decision here, and it discards five frames in six. That is fine for an
   aggregate. It would be wrong if anything downstream needs per-frame guarantees. This
   is the question I would want answered before anything else.
2. **What should "found nothing" mean to the orchestrator?** I chose exit 5 with a
   `no_valid_detections` status. A whole match shot from a fixed camera with no pitch in
   frame is a legitimate outcome, not a crash — but if the platform treats it as a data
   gap that must be retried, that is a different code path.
3. **Truncated feed: abort or salvage?** I abort (exit 4) with partial results attached.
   The alternative is to process the prefix and mark the run degraded. That is a product
   decision about whether partial data is better than none.
4. **Who owns stream reconnection?** I treat a mid-stream decode failure as fatal. If
   these are transient in production, the orchestrator should retry, and my exit code
   tells it to. If the pipeline is expected to reconnect, that is new work.
5. **Does anything actually consume the consensus boundary polygon?** If consumers only
   want the aggregate metric, the ring buffer and the consensus selection can go and the
   module gets simpler. I kept it because the prototype produced polygons and the README
   describes them feeding a crop step.
6. **Is crop layout in scope?** The prototype declares a `crop_search` config block and
   never reads it, and its README says crop layout is "not yet implemented". I did not
   implement it (see §2). If it is in scope, it belongs behind the same kind of seam.
7. **Multi-camera.** The prototype README describes ingesting multi-camera video. I built
   one feed per run and assume the orchestrator fans out. If a single run is meant to
   handle several synchronised feeds, the scheduler and aggregator both change shape.
8. **What false-positive rate is tolerable?** The detector is degenerate on this feed
   (see §3), so I cannot give a real number. I would want a target before tuning.

---

## 2. Validation strictness versus fallback

### Where I fail fast

| Situation | Behaviour | Exit |
|---|---|---|
| Configuration missing, unparsable, unknown key, out of range | Stop **before any I/O** | 2 |
| Input absent, unopenable, or reports no frames | Stop | 3 |
| Stream truncated, undecodable, or resolution changed | Stop | 4 |
| Any unclassified exception | Stop, with traceback | 1 |

Configuration is validated before the video is opened, so a misconfigured run costs
nothing. `extra="forbid"` on every model means a typo is an error: `min_are` instead of
`min_area` reports both the offending key and the one that is now missing.

**The cost of that strictness:** adding a field to the configuration is a breaking change
for older config files. I accepted that. The alternative is a typo'd key being ignored
while the pipeline runs with a value nobody chose, which is the exact failure the
prototype already had — its `sport` fell back to `"soccer"` while the file said
`"football"`, and its `min_area` fell back to `500` while the file said `1000`. Nobody
would have noticed, because nothing failed.

### The three deliberate fallbacks

Each is a fallback in *behaviour*, and each is visible. None of them fabricates data.

1. **Unknown frame rate → analyse every frame.** If the container reports no usable fps, a
   stride cannot be derived. Falling back to stride 1 costs more time but never discards
   data, which is the safe direction. Logged at `WARNING` with the reason, so an operator
   can tell why a run is slower than the configuration implies.
2. **Truncation tolerance of 2%.** Container frame counts are approximate, so a stream
   that ends a frame or two short is a clean end-of-file, not a corrupt feed. Beyond that
   it is a `StreamError`. Without a tolerance every run would fail; with too generous a
   one a genuinely truncated feed would be reported as complete, which is the bug being
   fixed.
3. **An unreachable platform degrades the run, it does not fail it.** Reports are retried
   with backoff, then dropped, with a circuit breaker so a dead endpoint cannot make a
   long run progressively slower. The run completes, and the result carries
   `reporting_degraded` plus a dropped count. A failure to report is never allowed to
   become the failure being reported.

### What I removed rather than carried forward

Three pieces of the prototype's configuration are **not** carried over, because dead
configuration is worse than no configuration — it tells an operator that turning a dial
does something when it does not:

| Prototype key | Status |
|---|---|
| `field_detector.type: "sam_mask_v1"` | Never read. Now load-bearing: it selects the detector, and an unknown value fails at startup. |
| `confidence_threshold: 0.5` | Assigned to `self.threshold` and never read. Became `aggregation.min_confidence`, which the aggregator actually enforces — a detection below it is counted as a `low_confidence` rejection. |
| `debug_mode: True` | Never read. Became `logging.level` / `logging.format`, because a boolean cannot distinguish "quiet" from "diagnosable". |
| `crop_search.*` | Entirely unused, and crop layout is not implemented. Omitted rather than included as decoration. |
| `target_fps: 30` | Never read; every frame was decoded anyway. Now drives the sampling stride. |

### Where I did not "fix" the prototype

The detector is **kept faithful**, including a flaw, rather than quietly corrected.
`DECISIONS` is the right place to say this rather than leaving a reviewer to discover it:

The synthetic background is solid green and the pitch is drawn as white lines *on top* of
it. The detector thresholds for green, so in every non-blank frame **the green mask
covers the whole frame**, and with `RETR_EXTERNAL` the largest contour is the **frame
border, not the pitch**. Measured directly:

```
blank frame      -> rejected (empty mask)
green close-up   -> DETECTED, area 919,601  (the frame)
noise rectangle  -> DETECTED, area 919,601  (the frame)
normal frame     -> DETECTED, area 919,601  (the frame)
```

All three non-blank cases produce an identical polygon. That is why the prototype's
reported "1776 boundaries found" is not 1776 pitch boundaries — it is 1776 frames that
contained green, and 98.7% is an artefact rather than a signal. It also shows up in the
aggregate metric, whose **interquartile range across the whole run is exactly 0**.

Fixing this is a modelling change, not a plumbing change, and doing it silently would
hide the one thing a reviewer most needs to know about the detector. The test suite
contains `test_known_limitation_the_boundary_is_the_frame_not_the_pitch`, which fails if
the behaviour changes — so changing it has to be deliberate and documented rather than
incidental.

---

## 3. Performance trade-offs

### Measured

| | Prototype | This pipeline |
|---|---|---|
| Frames decoded | 1800 | 1800 (advanced with `grab()`) |
| Frames analysed | 1800 | **300** |
| Wall time | **47.6 s** | **2.5 s** |
| Per frame | ~26 ms | — |
| Metric IQR | — | **0** (see §2) |

Roughly **19× faster** on the same feed, from the same detector, on the same machine.

### What the speedup is actually made of, and what it costs

**Sampling is the big win and the big risk.** Deriving a stride from the stream's real
frame rate (30fps source, 5fps target → stride 6) means five frames in six are never
looked at. For an aggregate result that is the right call. For anything needing per-frame
output it is wrong. This is the first question in §1 for a reason: it is a product
decision wearing a performance costume.

**`grab()` instead of `read()`.** `read()` is `grab()` + `retrieve()`, and `retrieve()` is
the expensive decode-to-BGR. Skipped frames are advanced with `grab()`, so they are
demuxed but never decoded. Honest limitation: on inter-frame codecs you cannot truly jump
past a frame without demuxing it, and `CAP_PROP_POS_FRAMES` seeking is not reliably
frame-accurate across containers, so seeking was not used. This is a real reduction in
work, not a free lunch.

**Hoisting repeated work.** The prototype rebuilt a hardcoded `1280x720` polygon *every
frame* to compute the intersection, and rebuilt its HSV bound arrays every frame. Both are
built once now, from the stream's real dimensions — which also fixes correctness, not just
speed: the prototype would have computed the wrong metric on any feed that was not
720p.

**One area pass instead of two.** `max(contours, key=cv2.contourArea)` evaluates every
contour's area and then evaluates the winner's again. Replaced with a single pass that
keeps the winner.

**Cheap rejects before expensive work.** The minimum-area gate now runs *before* any
Shapely construction, so the noise frames the generator deliberately injects — which
otherwise become full-frame polygons — are rejected for the cost of a contour area.

**Memory bounded in the dimension that matters.** The prototype appended
`(frame, polygon, area)` for every frame: memory grew with feed length, so a genuinely
long run would eventually die. Here the polygons — the heavy objects — are bounded by a
ring buffer. Plain floats for the metric are retained so the median is exact; at 8 bytes
per analysed frame that is about 1.4 MB for a ten-hour feed at 5fps, which is not worth
approximating to save.

**Median and interquartile range, not mean.** The feed contains deliberate false
positives. A mean lets them drag the headline number; a median does not. The cost is
slightly less sensitivity to genuine gradual change across a run, which matters if the
metric is expected to drift — worth revisiting if that is a real signal.

**Convergence early-exit is opt-in and off by default.** It is the only mechanism that
makes a *longer* feed genuinely cheaper for the *same* analysis, so it is worth having.
But it stops looking before the end, so it must be an explicit choice rather than the
default. `aggregation.early_exit: false` in the shipped config.

### What I left on the table

- **`requirements.txt` uses open-ended pins** (`opencv-python-headless>=4.10.0.84`), which
  resolved to OpenCV **5.0.0** on this machine. The generator and video I/O are verified
  working there, but a submission that resolves dependencies differently next month is not
  reproducible. I would pin exact versions with a constraints file. I stopped short of
  doing it only because there is no CI here to keep pins honest.
- **No benchmarking harness.** The 19× number is a single wall-clock measurement, not a
  repeatable benchmark. A proper one would separate decode time from detection time from
  aggregation time, and would use a longer feed to show the scaling curve rather than one
  data point.
- **No profiling.** I did not use a profiler; the optimisations are the ones visible from
  reading the prototype, not the ones a profile would have found.

---

## 4. AI / LLM disclosure

**Used: GitHub Copilot, in agent mode, throughout.** The design was directed by me; the
code was largely written by the assistant from that direction.

**How it was used, concretely**

- Reading the prototype and enumerating its defects against the four assignment parts.
  The 12-row defect table at the top of `docs/ARCHITECTURE.md` came from that pass.
- Writing the configuration models, the detector seam, the scheduler, the aggregator, the
  reporter, the pipeline, and the CLI.
- Writing the majority of the 176 tests, and the ad-hoc verification scripts used
  throughout.
- Drafting this document and the README from decisions I had already made and facts we
  had already measured.

**What I decided, and did not delegate**

- The scope call that matters most: **port the detector faithfully rather than fix it, and
  document the flaw loudly.** Left to its own devices the assistant would have quietly
  improved the detector, which would have hidden the most important finding in the
  exercise.
- Failing fast on unknown configuration keys rather than tolerating them, and accepting
  the backward-compatibility cost that follows.
- Not implementing crop layout, and removing its dead configuration block rather than
  carrying it forward as decoration.
- Commit granularity and the decision to push incrementally. The assignment grades history,
  so an honest history had to be built as the work happened rather than reconstructed.
- Every trade-off in §3.

**Where the assistant was wrong, and how it was caught**

Its *claims* were much less reliable than its *code*, and the difference only showed up
when things were run:

- Six tests failed on first run. Every one was a mistake in the test, not the code: the
  scheduler opens the capture in `__enter__` rather than in the constructor; the fixture
  frames were drawn at 720p coordinates on a 320×240 canvas so the pitch outline was
  clipped into an open shape that split the green into separate regions; the progress
  cadence was never reached because the fixture only yields 30 samples and the default
  reports every 250. None of these were visible by reading the diff.
- The truncation test returned exit 3 rather than 4, because truncating an MP4 removes its
  `moov` atom and makes the file *unopenable* rather than *truncated*. The assistant's
  expectation was wrong, not the code. The real `StreamError` path is now covered with a
  fake capture instead.
- Several edits were applied incorrectly during parallel edits to a single file, producing
  syntactically broken Python that had to be repaired. Caught by running the parser, not by
  reading.
- A verification script compared the wrong paths and printed `DIFF` for files that were in
  fact byte-identical. The check was wrong, not the code.

The practical consequence: **no claim in this document rests on the assistant saying so.**
The 19× speedup, the 0 interquartile range, the identical areas across frame types, and the
exit codes were all measured after the fact. The assistant asserted the detector was
degenerate from reading the source; that assertion was only believed once it had been
demonstrated on real frames.

**Not used:** nothing in the exercise was generated by an LLM and left unverified.
Automated formatting and linting (`ruff`) are used, but those are tools, not generation.

---

## 5. Where I stopped, and what is next

**Working, tested, and containerised:** configuration, the detector seam, sampling,
aggregation, failure taxonomy, structured logging, platform reporting with degradation
handling, the CLI, and the test suite.

**Deliberately not done**

| Not done | Why | Next step |
|---|---|---|
| Crop-layout recommendation | Not required by Parts 1–4; the prototype's config for it was dead. | Confirm scope; implement behind the same seam. |
| Pinned dependency versions | No CI to keep pins honest. | Add a constraints file and a lock step. |
| A real benchmark harness | One measurement, not a curve. | Time decode/detect/aggregate separately on a longer feed. |
| A learned detector | The seam exists for it; the model does not. | Add a `sam_mask_v1` config variant plus a registry entry. The test asserting config and registry agree will enforce it. |
| Auth on the reporting call | `mock_api` has none, and inventing one would be speculative. | Confirm the platform's auth scheme. |
| Feed trimming to a time range | Not asked for; the prototype had no notion of it. | Would be a scheduler parameter, cheap to add. |

**The first thing I would do with more time:** answer question 1 in §1. If per-frame
analysis turns out to be required, the largest performance decision in this design is
wrong, and everything else is detail by comparison.
