# Decisions

Where I stopped, what I assumed, and what I would ask before this went anywhere near
production.

The numbers below were all measured on this machine against the 1800 frame test video.
None of them are estimates.

---

## 1. Assumptions and open questions

### Things I assumed

- **The video is a file on disk.** The brief says the generator makes the input and my
  job starts from what it produces, so the pipeline takes a path. In production it is
  probably a stream or an object store key. Only `video.py` would need to change, because
  everything else just gets frames.

- **The frame rate and frame count from the file are roughly right.** I allow them to be
  off by 2% (explained in section 2).

- **The resolution stays the same for the whole video.** If it changes, I stop the run.
  Every measurement is in pixels. If half the video is at one size and half at another,
  all the numbers become meaningless.

- **The spatial metric is the one the prototype meant.** That is the area where the
  boundary overlaps the frame. I kept it, but I compute it once now instead of rebuilding
  the same rectangle on every frame.

- **`confidence` is a rough stand-in, not a real score.** A colour threshold cannot give
  you a real confidence. It reports how much of the frame matched the colour, and I
  named and documented it that way instead of pretending it is something better.

### Things I would ask the product and ML teams

1. **Is it OK to look at 5 frames a second instead of 30?** This is the single biggest
   performance decision in here, and it throws away five out of every six frames. That is
   fine when the answer is a summary. It would be wrong if anything downstream needs every
   single frame. I would want an answer to this before anything else.

2. **What should "found nothing" mean to the scheduler?** I made it exit 5 with a
   `no_valid_detections` status. A whole game shot from a fixed camera with no pitch in
   view is a real outcome, not a crash. But if the platform treats it as missing data that
   should be retried, that is a different piece of work.

3. **If the video is cut short, should we stop or keep what we have?** I stop and exit 4,
   with the partial results attached. The other option is to finish processing what is
   there and mark the run as degraded. That is a product call about whether half a result
   is better than none.

4. **Who reconnects if the stream drops?** I treat a broken frame as fatal. If that is
   usually a blip in production, the scheduler should retry and my exit code tells it so.
   If the pipeline is supposed to reconnect itself, that is new work.

5. **Does anything actually use the polygon?** If consumers only want the summary numbers,
   the ring buffer and the consensus polygon can go and the code gets simpler. I kept them
   because the prototype produced polygons and its README says they feed a crop step.

6. **Is crop layout in scope?** The prototype has a `crop_search` config block that
   nothing reads, and its README says crop layout is "not yet implemented". I did not build
   it. If it is needed, it should sit behind the same kind of seam as the detector.

7. **Multi-camera.** The prototype README talks about multi-camera video. I built one
   video per run and assumed the scheduler fans out. If one run is meant to handle several
   cameras at once, both the frame reader and the aggregator change.

8. **How wrong is acceptable?** The detector is broken in a specific way on this feed
   (section 2), so I cannot give you a real false positive rate. I would want a target
   before tuning anything.

---

## 2. Where I fail fast, and where I allow a fallback

### Where it stops

| What happened | What it does | Exit code |
|---|---|---|
| Config missing, unreadable, unknown key, bad value | Stops **before opening anything** | 2 |
| Video missing, unopenable, or has no frames | Stops | 3 |
| Video cut short, frame won't decode, resolution changes | Stops | 4 |
| Anything unexpected | Stops, with the traceback | 1 |

Config is checked before the video is opened, so a bad config costs you nothing.

Every model rejects unknown keys. That means `min_are` instead of `min_area` is an error,
and the message tells you both that `min_are` is not allowed and that `min_area` is
missing.

**What that costs:** adding a new setting breaks older config files. I took that trade
deliberately. The alternative is a typo being ignored while the program runs with a value
nobody picked. That is exactly what the prototype did. Its `sport` fell back to
`"soccer"` while the file said `"football"`, and `min_area` fell back to `500` while the
file said `1000`. Nobody would have noticed, because nothing failed.

### The three places I do fall back

All three are visible, and none of them invent data.

1. **No frame rate in the file, so look at every frame.** If the file does not say how
   fast it is, I cannot work out a stride. Falling back to "look at everything" costs more
   time but never throws data away. That is the safe direction. It logs a warning with the
   reason, so if a run is slower than you expected you can find out why.

2. **Cut short by up to 2% is fine.** Video files often report a frame count that is a
   little off, so finishing two frames early is normal. More than that and it is a broken
   file. Without a tolerance every run would fail. With too much tolerance, a genuinely
   damaged video would be reported as a success, which is the bug I was fixing.

3. **If the platform is down, the run still finishes.** Reports are retried with a
   backoff and then dropped. A circuit breaker stops it hammering a dead service, which
   would otherwise make a long run slower and slower. The run completes and the result
   says `reporting_degraded` with a count of what was lost. A failure to report never
   becomes the failure being reported.

### Config I removed instead of copying

Dead settings are worse than no settings. They tell an operator that turning a dial does
something when it does nothing. These were all in the prototype and none of them were ever
read:

| Old setting | What happened to it |
|---|---|
| `field_detector.type: "sam_mask_v1"` | Never read. Now it actually picks the detector, and an unknown name fails at startup. |
| `confidence_threshold: 0.5` | Stored in a variable and never used. Became `aggregation.min_confidence`, which is really enforced: anything below it is counted as a `low_confidence` rejection. |
| `debug_mode: True` | Never read. Became `logging.level` and `logging.format`, because a true/false cannot tell you the difference between quiet and diagnosable. |
| `crop_search` (the whole block) | Unused, and crop layout is not built. Left out rather than copied as decoration. |
| `target_fps: 30` | Never read, and every frame was decoded anyway. Now it drives the sampling. |

### One thing I did not "fix", on purpose

I ported the detector as-is, including a real flaw. This is the part I would most want
you to read.

The fake background is solid green and the pitch is drawn as white lines on top of it.
The detector looks for green. So on every frame that is not blank, **the green mask covers
the whole frame**, and the largest outline is therefore the **edge of the frame, not the
pitch**. I measured it:

```
blank frame     -> rejected
green close-up  -> DETECTED, area 919,601  (the frame)
noise frame     -> DETECTED, area 919,601  (the frame)
normal frame    -> DETECTED, area 919,601  (the frame)
```

All three non-blank cases give back the same polygon. That is why the prototype's "1776
boundaries found" is not 1776 pitch boundaries. It is 1776 frames that happened to contain
green, and 98.7% is an artefact rather than a result. You can see it in the summary
numbers too: across the whole run, the interquartile range of the metric is **exactly 0**.

I could have quietly made the detector better. I did not, because fixing it is a modelling
job rather than a plumbing one, and doing it silently would hide the most important thing
about this detector.

There is a test called
`test_known_limitation_the_boundary_is_the_frame_not_the_pitch` that fails if this
behaviour changes. So if someone improves the detector later, they have to do it on
purpose and update the docs.

---

## 3. Performance

### The measurements

|  | Old prototype | This |
|---|---|---|
| Frames decoded | 1800 | 1800 (but stepped over with `grab()`) |
| Frames actually looked at | 1800 | **300** |
| Wall clock | **47.6 s** | **2.5 s** |
| Per frame | about 26 ms | - |
| Metric interquartile range | - | **0** (see section 2) |

About **19 times faster** on the same video, same detector, same machine.

### Where that comes from, and what it costs

**Sampling is the big win and the big risk.** I read the real frame rate from the file
(30fps) and work out a stride from the target (5fps), which gives 6. So five frames out of
six are never looked at. For a summary that is the right call. For anything needing
per-frame output it is wrong. That is why it is question 1 above.

**`grab()` instead of `read()`.** `read()` is really two steps: grab the frame, then decode
it. The decode is the expensive half. Skipped frames get the cheap half only. Honest
caveat: on most codecs you still cannot jump past a frame without touching it, and seeking
by frame number is not reliable across all video formats, so I did not use seeking. This
is a real saving, not a free lunch.

**Doing repeated work once.** The prototype rebuilt the same 1280x720 rectangle every
single frame to calculate the overlap, and rebuilt its colour range arrays every frame
too. Both are built once now, using the video's actual size. That also fixes a bug: on any
video that was not 720p, the old code would have measured the wrong thing.

**One pass instead of two.** `max(contours, key=cv2.contourArea)` works out the area of
every outline and then works out the winner's again. Now it is one pass that keeps the
best one.

**Cheap rejections first.** The minimum size check now happens before any geometry work.
The deliberately noisy frames in the test video used to become full-size polygons. Now
they are thrown out for the cost of one area calculation.

**Memory is bounded where it matters.** The prototype added every polygon to a list for
the whole run, so memory grew with the video length and a long enough run would eventually
die. Here the polygons, which are the heavy things, are kept in a small ring buffer. The
metric values are plain numbers and I keep all of them so the median is exact. At 8 bytes
each that is about 1.4 MB for ten hours of video at 5fps, which is not worth throwing
accuracy away to save.

**Median and interquartile range, not an average.** The video has fake detections built
into it. An average lets those drag the headline number around. A median does not. The
cost is that it is less sensitive to a genuine slow change during a run. If that turns out
to be a real signal, it is worth revisiting.

**Early exit is off by default.** It is the only thing here that makes a *longer* video
genuinely cheaper for the *same* work, so it is worth having. But it stops looking before
the end, so it should be a choice, not the default.

### What I did not get to

- **Dependencies are not pinned to exact versions.** `requirements.txt` says things like
  `opencv-python-headless>=4.10.0.84`, and on this machine that resolved to OpenCV
  **5.0.0**. I checked the generator and video reading work on it, but a submission that
  installs something different next month is not reproducible. I would pin exact versions
  with a lock file. I did not do it here because there is no CI to keep pins honest.

- **No proper benchmark.** The 19x is one wall clock measurement, not a curve. A real
  benchmark would time decoding, detection, and adding up separately, and would use a much
  longer video to show how it scales.

- **No profiler.** I found these wins by reading the prototype, not by profiling it. A
  profile would probably find different ones.

---

## 4. How I used AI

**I used GitHub Copilot in agent mode throughout.** I decided the design. The assistant
wrote most of the code from that direction.

### What it did

- Read the prototype and list its problems against the four parts of the brief. The table
  at the top of `docs/ARCHITECTURE.md` came from that.
- Wrote the config models, the detector seam, the frame scheduler, the aggregator, the
  reporter, the pipeline, and the CLI.
- Wrote most of the 176 tests, and the small scripts I used to check things by hand.
- Drafted this document and the README, from decisions I had already made and numbers we
  had already measured.

### What I decided myself

- **The big one: port the detector as-is instead of fixing it, and write down the flaw
  loudly.** Left alone, the assistant would have quietly improved the detector. That would
  have hidden the most useful finding in the whole exercise.

- Fail hard on unknown config keys, and accept that adding a setting later breaks old
  configs.

- Not build crop layout, and delete its dead config block rather than keep it as
  decoration.

- Commit granularity, and pushing as I went. The brief grades git history, so the history
  had to be built while the work happened, not reconstructed at the end.

- Every trade-off in section 3.

### Where the assistant was wrong

Its **claims** were much less reliable than its **code**, and the difference only showed up
when I actually ran things.

- Six tests failed on the first run. All six were mistakes in the tests, not in the source
  code. The scheduler opens the video when you enter the `with` block, not when you create
  the object. The test frames were drawn at 720p coordinates on a 320x240 canvas, so the
  pitch outline got clipped into an open shape and split the green into separate pieces.
  The progress reporting test never fired because the test video only produces 30 frames
  and the default setting reports every 250. None of these were visible by reading the
  code.

- My truncation test gave exit 3 instead of 4. Truncating an MP4 file removes its `moov`
  atom, which makes the file impossible to open, which is exit 3. My expectation was
  wrong, not the code. The real "video cut short" path is now tested with a fake video
  reader instead.

- Some edits were applied incorrectly when I sent several at once to the same file, which
  produced Python that would not even parse. Found by running the parser, not by reading.

- One of my check scripts compared the wrong file paths and reported "DIFF" for two files
  that were actually byte for byte identical. The check was wrong, not the code.

The point of all that: **nothing in this document rests on the assistant saying so.** The
19x speedup, the zero interquartile range, the identical areas, and the exit codes were all
measured after the fact. The assistant claimed the detector was broken just from reading
the source. I only believed it once it was shown on real frames.

**Not used:** nothing generated by an LLM was left unchecked. I do use `ruff` for
formatting and linting, but that is a tool, not generation.

---

## 5. Where I stopped

**Done, tested, and containerised:** config and its validation, the detector seam,
sampling, aggregation, the failure rules, structured logging, platform reporting with
degradation handling, the CLI, and the tests.

**Left out on purpose**

| Not done | Why | Next step |
|---|---|---|
| Crop layout recommendation | Not required by parts 1 to 4, and the prototype's config for it was dead | Confirm it is wanted, then build it behind the same seam |
| Pinned dependency versions | No CI to keep pins honest | Add a lock file |
| A real benchmark | One measurement, not a curve | Time decode, detect and aggregate separately on a longer video |
| A real ML detector | The seam is there, the model is not | Add a `sam_mask_v1` config variant plus a registry entry. The test that checks config and registry agree will force it to be done properly |
| Auth on the reporting call | `mock_api` has none, and inventing one would be guessing | Ask what the platform expects |
| Processing only part of a video | Not asked for, and the prototype had no concept of it | A parameter on the frame scheduler. Cheap. |

**The first thing I would do with more time** is get an answer to question 1. If per-frame
analysis turns out to be required, the biggest performance decision in here is wrong, and
everything else is small by comparison.
