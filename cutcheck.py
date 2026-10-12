"""cutcheck: a linter for rendered video.

Code has linters. Video has "final_v3_actual (1).mp4". cutcheck runs the
technical checks a human editor does by eye before publishing, and reports each
problem with a timestamp, a severity and a rule name, like a code linter.

    python cutcheck.py video.mp4                 # human-readable report
    python cutcheck.py video.mp4 --json          # machine-readable, for an agent
    python cutcheck.py video.mp4 --target reels  # platform rules (9:16, -14 LUFS)

Exit code is 1 if any error is found, so an editing agent (or CI) can gate on it.

Every check is deterministic: ffmpeg/ffprobe measure, Python decides. No model is
involved, so the same file always gets the same verdict. That is the point: an
agent that edits video needs a verifier it cannot argue with.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, asdict, field

# Platform targets. Loudness targets are the commonly used normalisation levels.
TARGETS = {
    "reels":   {"aspect": (9, 16), "lufs": -14.0},
    "shorts":  {"aspect": (9, 16), "lufs": -14.0},
    "youtube": {"aspect": (16, 9), "lufs": -14.0},
    "none":    {"aspect": None,    "lufs": None},
}

LUFS_TOLERANCE = 2.0        # +/- dB around the target before we complain
TRUE_PEAK_MAX = -1.0        # dBTP; above this, platform transcodes tend to clip
AV_DRIFT_MAX = 0.20         # seconds of audio/video length mismatch allowed
BLACK_MIN = 0.5             # seconds of black before it counts
FREEZE_MIN = 1.0            # seconds of identical frames before it counts
SILENCE_MIN = 1.0           # seconds of silence before it counts
EDGE = 0.25                 # ignore issues that only touch the first/last 0.25 s


@dataclass
class Finding:
    rule: str
    severity: str               # "error" | "warning"
    message: str
    start: float | None = None
    end: float | None = None


@dataclass
class Report:
    file: str
    duration: float
    findings: list[Finding] = field(default_factory=list)

    @property
    def errors(self) -> int:
        return sum(f.severity == "error" for f in self.findings)

    @property
    def warnings(self) -> int:
        return sum(f.severity == "warning" for f in self.findings)


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        raise SystemExit(f"cutcheck: '{cmd[0]}' not found. Install ffmpeg (which includes "
                         "ffprobe) and make sure it is on your PATH.")


def probe(path: str) -> dict:
    r = _run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path])
    if r.returncode != 0:
        raise SystemExit(f"cutcheck: cannot read {path}: {r.stderr.strip()}")
    return json.loads(r.stdout)


def _filter_log(path: str, vf: str | None = None, af: str | None = None) -> str:
    """Run ffmpeg with an analysis filter and return its log (stderr)."""
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-i", path]
    if vf:
        cmd += ["-vf", vf, "-an"]
    if af:
        cmd += ["-af", af, "-vn"]
    cmd += ["-f", "null", "-"]
    return _run(cmd).stderr


def _intervals(log: str, start_key: str, end_key: str, total: float) -> list[tuple[float, float]]:
    """Pair up start/end markers from an ffmpeg detect filter. An open interval
    (start with no end) runs to the end of the file."""
    starts = [float(x) for x in re.findall(rf"{start_key}:\s*(-?[\d.]+)", log)]
    ends = [float(x) for x in re.findall(rf"{end_key}:\s*(-?[\d.]+)", log)]
    out = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else total
        out.append((max(0.0, s), e))
    return out


def _is_edge(s: float, e: float, total: float) -> bool:
    """A fade in/out at the very start or end is usually intentional."""
    return e <= EDGE or s >= total - EDGE


def check(path: str, target: str = "none") -> Report:
    info = probe(path)
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    total = float(info.get("format", {}).get("duration", 0) or 0)
    rep = Report(file=path, duration=round(total, 3))
    add = rep.findings.append

    # --- structure -----------------------------------------------------------
    if video is None:
        add(Finding("no-video", "error", "File has no video stream."))
    if audio is None:
        add(Finding("no-audio", "warning", "File has no audio stream."))

    if video is not None and audio is not None:
        vd = float(video.get("duration", total) or total)
        ad = float(audio.get("duration", total) or total)
        drift = abs(vd - ad)
        if drift > AV_DRIFT_MAX:
            add(Finding("av-length-mismatch", "error",
                        f"Audio and video lengths differ by {drift:.2f}s "
                        f"(video {vd:.2f}s, audio {ad:.2f}s); likely sync drift or a cut track."))

    rule = TARGETS[target]
    if video is not None and rule["aspect"]:
        w, h = int(video.get("width", 0)), int(video.get("height", 0))
        aw, ah = rule["aspect"]
        if w and h and abs(w / h - aw / ah) > 0.01:
            add(Finding("aspect-ratio", "error",
                        f"{w}x{h} is not {aw}:{ah}, which {target} expects."))

    # --- visual defects ------------------------------------------------------
    if video is not None:
        log = _filter_log(path, vf=f"blackdetect=d={BLACK_MIN}:pix_th=0.10")
        for s, e in _intervals(log, "black_start", "black_end", total):
            if not _is_edge(s, e, total):
                add(Finding("black-frames", "error",
                            f"{e - s:.2f}s of black frames mid-video.", round(s, 2), round(e, 2)))

        log = _filter_log(path, vf=f"freezedetect=n=0.003:d={FREEZE_MIN}")
        starts = [float(x) for x in re.findall(r"freeze_start:\s*([\d.]+)", log)]
        ends = [float(x) for x in re.findall(r"freeze_end:\s*([\d.]+)", log)]
        for i, s in enumerate(starts):
            e = ends[i] if i < len(ends) else total
            if not _is_edge(s, e, total):
                add(Finding("frozen-frames", "warning",
                            f"Picture frozen for {e - s:.2f}s (stalled render or held frame?).",
                            round(s, 2), round(e, 2)))

    # --- audio defects -------------------------------------------------------
    if audio is not None:
        log = _filter_log(path, af=f"silencedetect=n=-50dB:d={SILENCE_MIN}")
        for s, e in _intervals(log, "silence_start", "silence_end", total):
            if not _is_edge(s, e, total):
                add(Finding("silence", "warning",
                            f"{e - s:.2f}s of silence mid-video (dropped audio?).",
                            round(s, 2), round(e, 2)))

        log = _filter_log(path, af="ebur128=peak=true")
        summary = log[log.rfind("Summary:"):] if "Summary:" in log else ""
        m_i = re.search(r"I:\s*(-?[\d.]+|-inf)\s*LUFS", summary)
        m_p = re.search(r"Peak:\s*(-?[\d.]+|-inf)\s*dBFS", summary)
        lufs = float(m_i.group(1)) if m_i and m_i.group(1) != "-inf" else None
        peak = float(m_p.group(1)) if m_p and m_p.group(1) != "-inf" else None
        if peak is not None and peak > TRUE_PEAK_MAX:
            add(Finding("clipping-risk", "error",
                        f"True peak {peak:.1f} dBTP is above {TRUE_PEAK_MAX} dBTP; "
                        "it will likely clip after platform transcoding."))
        if lufs is not None and rule["lufs"] is not None and abs(lufs - rule["lufs"]) > LUFS_TOLERANCE:
            direction = "too quiet" if lufs < rule["lufs"] else "too loud"
            add(Finding("loudness", "warning",
                        f"Integrated loudness {lufs:.1f} LUFS is {direction} for {target} "
                        f"(target {rule['lufs']:.0f} LUFS)."))

    # A black stretch is also a run of identical frames. Report it once, as black.
    blacks = [(f.start, f.end) for f in rep.findings if f.rule == "black-frames"]
    rep.findings = [
        f for f in rep.findings
        if not (f.rule == "frozen-frames"
                and any(bs - 0.1 <= f.start and f.end <= be + 0.1 for bs, be in blacks))
    ]
    rep.findings.sort(key=lambda f: (f.start is None, f.start or 0))
    return rep


def _fmt_t(t: float | None) -> str:
    if t is None:
        return "  --  "
    m, s = divmod(t, 60)
    return f"{int(m):02d}:{s:05.2f}"


def print_report(rep: Report) -> None:
    print(f"\n{rep.file}  ({rep.duration:.2f}s)\n")
    if not rep.findings:
        print("  no problems found\n")
        return
    for f in rep.findings:
        span = f"{_fmt_t(f.start)}-{_fmt_t(f.end)}" if f.start is not None else " " * 17
        print(f"  {span}  {f.severity:<7}  {f.rule:<20} {f.message}")
    print(f"\n  {rep.errors} error(s), {rep.warnings} warning(s)\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Lint a rendered video for technical defects.")
    ap.add_argument("video")
    ap.add_argument("--target", choices=sorted(TARGETS), default="none")
    ap.add_argument("--json", action="store_true", help="print JSON instead of text")
    args = ap.parse_args(argv)

    rep = check(args.video, args.target)
    if args.json:
        out = asdict(rep)
        out["errors"], out["warnings"] = rep.errors, rep.warnings
        print(json.dumps(out, indent=2))
    else:
        print_report(rep)
    return 1 if rep.errors else 0


if __name__ == "__main__":
    sys.exit(main())
