# SPDX-License-Identifier: Apache-2.0
"""Measure what LL-HLS packaging adds to vLLM-Omni's "faster than playback" claim.

The claim as posted: 10.1 seconds of video rendered in 8.7 seconds, a real-time
factor of 1.161. That is a render number. A viewer never sees render time; they
see prompt-to-glass, and the packaging step sits between the two.

This script feeds a fragmented-MP4 source through the same splitter and packager
that ``hls_client.py`` uses against a live server, but paces the fragments to
arrive at a chosen real-time factor instead of waiting on a GPU. It then reports
the terms the packager can observe, plus the one a player cares about:
live-edge lead, which is how far ahead of playback the playlist is at every
moment. If lead ever drops below zero, the player stalls.

No GPU, no server. Needs ffmpeg only to synthesise the source and to verify the
playlist decodes; pass ``--source`` to reuse a file and ``--no-verify`` to skip
the decode check.

Usage::

    python3 bench.py                       # synthesise source, pace at 1.161
    python3 bench.py --pace 1.0 --slice 1500
    python3 bench.py --source out/source.fmp4 --json out/report.json --markdown
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from fmp4 import FragmentedMP4Splitter  # noqa: E402
from llhls import LLHLSPackager, PartRecord  # noqa: E402

# Reference shape from the server's own ``video.chunk_metadata`` in the default
# streaming config: 640x384, 16 fps, 9 frames per chunk.
DEFAULT_WIDTH = 640
DEFAULT_HEIGHT = 384
DEFAULT_FPS = 16
DEFAULT_FRAMES_PER_CHUNK = 9
DEFAULT_MEDIA_SECONDS = 6

# 10.1 s of media in 8.7 s, as posted for the reference demo.
POSTED_MEDIA_SECONDS = 10.1
POSTED_RENDER_SECONDS = 8.7
POSTED_REALTIME_FACTOR = POSTED_MEDIA_SECONDS / POSTED_RENDER_SECONDS


def synthesise_source(path: Path, *, width: int, height: int, fps: int,
                      frames_per_chunk: int, seconds: int) -> None:
    """Produce an fMP4 with one keyframe-led fragment per chunk, like the server."""
    if shutil.which("ffmpeg") is None:
        sys.exit("ffmpeg not found; pass --source with an existing fMP4 instead")
    frag_us = int(frames_per_chunk / fps * 1_000_000)
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc2=size={width}x{height}:rate={fps}:duration={seconds}",
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
        "-g", str(frames_per_chunk), "-keyint_min", str(frames_per_chunk), "-sc_threshold", "0",
        "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
        "-frag_duration", str(frag_us),
        "-f", "mp4", str(path),
    ]
    subprocess.run(cmd, check=True)


def run(source: bytes, out_dir: Path, *, part_duration: float, pace: float,
        slice_size: int, parts_per_segment: int) -> tuple[dict, list[PartRecord]]:
    """Feed ``source`` through splitter and packager at ``pace`` x real time."""
    if out_dir.exists():
        shutil.rmtree(out_dir)
    packager = LLHLSPackager(out_dir=out_dir, part_duration=part_duration,
                             parts_per_segment=parts_per_segment)
    splitter = FragmentedMP4Splitter()
    records: list[PartRecord] = []

    # Pre-split so pacing is not distorted by the cost of reading the file. The
    # splitter still receives the bytes in ``slice_size`` pieces, which is the
    # condition that breaks naive implementations, and it is timed separately.
    t_split = time.perf_counter()
    fragments: list[bytes] = []
    for i in range(0, len(source), slice_size):
        fragments.extend(splitter.feed(source[i:i + slice_size]))
    split_seconds = time.perf_counter() - t_split
    if not fragments or splitter.init_segment is None:
        sys.exit("source produced no fragments; is it a fragmented MP4?")

    # A generator running at ``pace`` x real time finishes chunk i at
    # (i + 1) * part_duration / pace after it starts. Chunk 0 is not free.
    interval = part_duration / pace
    packager.start(splitter.init_segment)
    t0 = packager.started_at
    assert t0 is not None
    for i, frag in enumerate(fragments):
        due = t0 + (i + 1) * interval
        while True:
            now = time.perf_counter()
            if now >= due:
                break
            time.sleep(min(due - now, 0.002))
        records.append(packager.add_fragment(frag, received_at=time.perf_counter()))
    packager.finish()

    report = packager.report()
    report["split_ms_total"] = round(split_seconds * 1000, 3)
    report["slice_bytes"] = slice_size
    report["pace"] = round(pace, 3)

    # Live-edge lead after each part, for a player that joined on the first
    # part and has been playing since: media the playlist advertises minus wall
    # time since that first publish. Below zero the player has run out.
    first_pub = records[0].published_at
    leads = [
        (i + 1) * part_duration - (r.published_at - first_pub)
        for i, r in enumerate(records)
    ]
    hold_back = 3 * part_duration
    covered = next((i for i, lead in enumerate(leads) if lead >= hold_back), None)
    report["hold_back_s"] = round(hold_back, 4)
    report["lead_s"] = {
        "first": round(leads[0], 4),
        "min": round(min(leads), 4),
        "final": round(leads[-1], 4),
        "per_part_delta": round((leads[-1] - leads[0]) / max(1, len(leads) - 1), 4),
        "hold_back_covered_at_part": covered,
    }
    pub_ms = [r.publish_latency * 1000 for r in records]
    report["publish_latency_ms"]["p95"] = round(
        statistics.quantiles(pub_ms, n=20, method="inclusive")[-1] if len(pub_ms) >= 2 else pub_ms[0], 3
    )
    report["publish_latency_ms"]["per_part"] = [round(x, 3) for x in pub_ms]
    report["verdict"] = verdict(report)
    return report, records


def verdict(report: dict) -> str:
    """Whether a player that joined on the first part would have stalled.

    STALL: lead went negative, so at some point the next part was not there
    when playback reached it. DRAINING: no stall on this clip, but lead shrank
    part over part, so a longer clip would stall; generation is slower than
    playback once packaging is counted. PASS: lead never went negative and
    grew or held, so "faster than playback" survived packaging.

    ``hold_back_covered_at_part`` in the report says when a late joiner at the
    advertised PART-HOLD-BACK is fully covered; it is informational here.
    """
    lead = report["lead_s"]
    if lead["min"] < 0:
        return "STALL"
    if lead["per_part_delta"] < 0:
        return "DRAINING"
    return "PASS"


def verify(out_dir: Path) -> dict:
    """Decode the playlist with ffmpeg and count frames with ffprobe."""
    playlist = out_dir / "stream.m3u8"
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        return {"skipped": "ffmpeg/ffprobe not found"}
    dec = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-allowed_extensions", "ALL",
         "-i", str(playlist), "-f", "null", "-"],
        capture_output=True, text=True,
    )
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-allowed_extensions", "ALL",
         "-count_frames", "-select_streams", "v:0",
         "-show_entries", "stream=nb_read_frames,r_frame_rate,width,height",
         "-of", "json", str(playlist)],
        capture_output=True, text=True,
    )
    result: dict = {"decode_exit": dec.returncode, "decode_stderr": dec.stderr.strip()}
    try:
        stream = json.loads(probe.stdout)["streams"][0]
        result["frames"] = int(stream["nb_read_frames"])
        result["frame_rate"] = stream["r_frame_rate"]
        result["size"] = f'{stream["width"]}x{stream["height"]}'
    except (KeyError, IndexError, ValueError, json.JSONDecodeError):
        result["probe_error"] = probe.stderr.strip() or "unparseable ffprobe output"
    return result


def markdown_row(report: dict) -> str:
    v = report.get("verify", {})
    frames = v.get("frames", "n/a")
    lat = report["publish_latency_ms"]
    return (
        f"| {report['pace']:.3f} | {report['slice_bytes']} | {report['parts']} | "
        f"{report['realtime_factor']:.3f} | {report['time_to_first_part_s']:.3f} | "
        f"{lat['mean']:.3f} | {lat['p95']:.3f} | {lat['max']:.3f} | "
        f"{report['lead_s']['min']:.3f} | {report['lead_s']['final']:.3f} | "
        f"{report['verdict']} | {frames} |"
    )


def aggregate_row(reports: list[dict]) -> str:
    """Worst case across runs: the tail is the point of repeating."""
    rank = {"PASS": 0, "DRAINING": 1, "STALL": 2}
    worst = max(reports, key=lambda r: rank[r["verdict"]])
    return (
        f"| {reports[0]['pace']:.3f} | {reports[0]['slice_bytes']} | x{len(reports)} | "
        f"{statistics.median(r['realtime_factor'] for r in reports):.3f} | "
        f"{max(r['time_to_first_part_s'] for r in reports):.3f} | "
        f"{statistics.median(r['publish_latency_ms']['mean'] for r in reports):.3f} | "
        f"{max(r['publish_latency_ms']['p95'] for r in reports):.3f} | "
        f"{max(r['publish_latency_ms']['max'] for r in reports):.3f} | "
        f"{min(r['lead_s']['min'] for r in reports):.3f} | "
        f"{min(r['lead_s']['final'] for r in reports):.3f} | "
        f"{worst['verdict']} (worst) | {min(r.get('verify', {}).get('frames', 0) for r in reports)} |"
    )


MARKDOWN_HEADER = (
    "| pace | slice B | parts | measured RTF | first part s | pub mean ms | pub p95 ms | pub max ms "
    "| lead min s | lead final s | verdict | frames decoded |\n"
    "|---|---|---|---|---|---|---|---|---|---|---|---|"
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--source", type=Path, help="existing fMP4; synthesised if omitted")
    ap.add_argument("--out", type=Path, default=Path("out"), help="output root (default: out)")
    ap.add_argument("--pace", type=float, default=POSTED_REALTIME_FACTOR,
                    help=f"generation real-time factor to simulate (default {POSTED_REALTIME_FACTOR:.3f}, the posted claim)")
    ap.add_argument("--slice", type=int, default=8192, help="bytes per feed() call (default 8192)")
    ap.add_argument("--fps", type=int, default=DEFAULT_FPS)
    ap.add_argument("--frames-per-chunk", type=int, default=DEFAULT_FRAMES_PER_CHUNK)
    ap.add_argument("--seconds", type=int, default=DEFAULT_MEDIA_SECONDS, help="synthesised source length")
    ap.add_argument("--parts-per-segment", type=int, default=4)
    ap.add_argument("--runs", type=int, default=1, help="repeat the run to expose the tail (default 1)")
    ap.add_argument("--json", type=Path, help="write the report here as JSON (a list when --runs > 1)")
    ap.add_argument("--markdown", action="store_true", help="print a Markdown table row")
    ap.add_argument("--no-verify", action="store_true", help="skip the ffmpeg decode check")
    args = ap.parse_args(argv)

    if args.slice < 1:
        ap.error("--slice must be at least 1")
    if args.pace <= 0:
        ap.error("--pace must be positive")

    source_path = args.source or args.out / "source.fmp4"
    if args.source is None and not source_path.exists():
        synthesise_source(source_path, width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT, fps=args.fps,
                          frames_per_chunk=args.frames_per_chunk, seconds=args.seconds)
    source = source_path.read_bytes()

    if args.runs < 1:
        ap.error("--runs must be at least 1")

    part_duration = args.frames_per_chunk / args.fps
    hls_dir = args.out / "hls"
    reports: list[dict] = []
    for _ in range(args.runs):
        report, _ = run(source, hls_dir, part_duration=part_duration, pace=args.pace,
                        slice_size=args.slice, parts_per_segment=args.parts_per_segment)
        report["source"] = str(source_path)
        report["source_bytes"] = len(source)
        if not args.no_verify:
            report["verify"] = verify(hls_dir)
        reports.append(report)

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        payload = reports[0] if args.runs == 1 else reports
        args.json.write_text(json.dumps(payload, indent=2) + "\n")

    if args.markdown:
        print(MARKDOWN_HEADER)
        for report in reports:
            print(markdown_row(report))
        if args.runs > 1:
            print(aggregate_row(reports))
    else:
        print(json.dumps(reports[0] if args.runs == 1 else reports, indent=2))

    failed = [r for r in reports if r.get("verify", {}).get("decode_exit", 0) != 0]
    if failed:
        print(f"decode FAILED: {failed[0]['verify'].get('decode_stderr')}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
