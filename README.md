# cutcheck

A linter for rendered video.

> "After the agent makes edits, how does it know if the output is good? There's no linter for video. We have to build one."

cutcheck is a first step at the technical half of that problem: the checks a human editor does by eye before publishing. It reports each problem with a timestamp, a severity and a rule name, like a code linter, and exits with code 1 when there is an error. So an editing agent, or CI, can gate on it before anything ships.

```
$ python cutcheck.py broken.mp4 --target reels

  00:02.00-00:03.00  error    black-frames         1.00s of black frames mid-video.
  00:02.00-00:03.50  warning  silence              1.50s of silence mid-video (dropped audio?).
  00:03.50-00:05.50  warning  frozen-frames        Picture frozen for 2.00s (stalled render or held frame?).
                     error    av-length-mismatch   Audio and video lengths differ by 1.50s ...
                     error    aspect-ratio         1280x720 is not 9:16, which reels expects.
                     error    clipping-risk        True peak 0.7 dBTP is above -1.0 dBTP ...
                     warning  loudness             Integrated loudness -2.4 LUFS is too loud for reels (target -14 LUFS).

  4 error(s), 3 warning(s)
```

`--json` prints the same findings as structured data, for an agent to read and act on. For example: "re-render 00:02–00:03", or "normalise audio to -14 LUFS".

## Rules

| Rule | Severity | What it catches |
|---|---|---|
| `black-frames` | error | Black stretch of 0.5 s or more mid-video (a missing clip or failed render) |
| `frozen-frames` | warning | Identical frames for 1 s or more (a stalled render or a held frame) |
| `silence` | warning | 1 s or more of silence mid-video (dropped audio) |
| `av-length-mismatch` | error | Audio and video lengths differ by more than 0.2 s (sync drift or a cut track) |
| `clipping-risk` | error | True peak above -1 dBTP; it will clip after platform transcoding |
| `loudness` | warning | Integrated loudness more than 2 LU off the platform target |
| `aspect-ratio` | error | Wrong shape for the target (9:16 for reels and shorts, 16:9 for YouTube) |
| `no-video` / `no-audio` | error / warning | Missing stream |

A fade in or out touching the first or last 0.25 s is ignored, because it's usually intentional. A black stretch is reported once as `black-frames`, not also as frozen.

## Design choices

- **Deterministic, no model.** ffmpeg measures and Python decides. The same file always gets the same verdict, which is what you want from the check that judges an agent's work. Taste ("does this edit look good?") is a separate, model-based layer that should sit on top of this one, not replace it.
- **Tested against known answers.** `test_cutcheck.py` builds a clean clip and a broken clip with defects planted at known times. It asserts that the clean clip produces zero findings, that every planted defect is found, and that the black stretch is located to within 0.2 s.
- **Built like a linter.** Rule names, severities, timestamps and an exit code, so it can sit in a pipeline.

## Run

Needs Python 3.10+ and ffmpeg on PATH. No Python dependencies.

```
python cutcheck.py video.mp4 [--target reels|shorts|youtube] [--json]
python test_cutcheck.py
```

## Prior art: what this is and isn't

These checks are not new. They are built on ffmpeg's own `blackdetect`, `freezedetect`, `silencedetect` and `ebur128` filters, and broadcast QC tools such as [MediaQCTool](https://github.com/keithkeekw/MediaQCTool) already run them. Evaluating AI edits is also active research:
- [VEFX-Bench](https://github.com/Visko-Platform/VEFX-Bench) scores instruction following, render quality and *edit exclusivity* (whether only the intended parts changed).
- [Timeline-Bench](https://timelinebench.tensortest.com/paper) evaluates full editing tasks against delivery, content and brief requirements.

cutcheck is a small, deterministic baseline, shaped like a linter so an editing agent can gate on it. The interesting next question is narrower and still open for me to test. Given the brief and a machine-readable edit plan, can a verifier catch the regressions an edit *wasn't supposed to* cause, such as a protected segment changing or captions drifting from the audio? And can it do that better than technical QC alone or a general model critique? That needs a proper comparison against the benchmarks above before any claim.

## Not done yet

- Real A/V sync offset (lip sync). It currently checks length drift only.
- Scene-aware checks: jump cuts, flash frames, text running off the safe area.
- Running on a timeline (before render), not just on the rendered file.
- The taste layer: pacing, brand match and story arc, scored by a model and checked against engagement data.
