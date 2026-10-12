"""Known-answer tests: build videos whose defects we planted on purpose, then
check cutcheck finds exactly those, and nothing on a clean file.

    python test_cutcheck.py
"""
import subprocess
import tempfile
from pathlib import Path

from cutcheck import check

FF = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]


def make_clean(out: Path) -> None:
    # 6 s, 1080x1920 (9:16), moving picture, steady tone around -14 LUFS.
    subprocess.run(FF + [
        "-f", "lavfi", "-i", "testsrc2=size=1080x1920:rate=30:duration=6",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=6,volume=2.4",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(out)],
        check=True)


def make_broken(out: Path) -> None:
    # 16:9, with: black 2-3 s, frozen picture 3.5-5.5 s, silence 2-3.5 s,
    # audio hot enough to clip, and audio ~1 s shorter than video.
    fc = (
        "[0:v]trim=0:2,setpts=PTS-STARTPTS[v1];"
        "[1:v]trim=0:1,setpts=PTS-STARTPTS[v2];"
        "[0:v]trim=2:2.5,setpts=PTS-STARTPTS[v3];"
        "[2:v]trim=0:2,setpts=PTS-STARTPTS[v4];"
        "[0:v]trim=3:4.5,setpts=PTS-STARTPTS[v5];"
        "[v1][v2][v3][v4][v5]concat=n=5:v=1:a=0[v];"
        "[3:a]atrim=0:2,asetpts=PTS-STARTPTS[a1];"
        "[4:a]atrim=0:1.5,asetpts=PTS-STARTPTS[a2];"
        "[3:a]atrim=2:4,asetpts=PTS-STARTPTS[a3];"
        "[a1][a2][a3]concat=n=3:v=0:a=1,volume=20,aformat=sample_fmts=s16[a]"
    )
    subprocess.run(FF + [
        "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=8",
        "-f", "lavfi", "-i", "color=black:size=1280x720:rate=30:duration=2",
        "-f", "lavfi", "-i", "smptebars=size=1280x720:rate=30:duration=3",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=8,volume=1",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=3",
        "-filter_complex", fc, "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(out)],
        check=True)


def rules(rep):
    return {f.rule for f in rep.findings}


def main() -> None:
    d = Path(tempfile.mkdtemp())
    clean, broken = d / "clean.mp4", d / "broken.mp4"
    make_clean(clean)
    make_broken(broken)

    rep = check(str(clean), target="reels")
    assert not rep.findings, [f.message for f in rep.findings]
    print("PASS clean file: no findings")

    rep = check(str(broken), target="reels")
    found = rules(rep)
    expected = {"black-frames", "frozen-frames", "silence", "clipping-risk",
                "aspect-ratio", "av-length-mismatch"}
    missing = expected - found
    assert not missing, f"missed: {missing}; found {found}"
    black = next(f for f in rep.findings if f.rule == "black-frames")
    assert 1.8 <= black.start <= 2.2 and 2.8 <= black.end <= 3.2, (black.start, black.end)
    print("PASS broken file: found", sorted(found))
    print("PASS black frames located at", black.start, "-", black.end)


if __name__ == "__main__":
    main()
