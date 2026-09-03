# omni-hls

What "generation faster than playback" costs to deliver.

vLLM-Omni renders a 10.1 second clip in 8.7 seconds, a real-time factor of 1.161.
That is a render number. A viewer never sees render time. They see prompt-to-glass:
render, mux, publish, fetch, decode, first frame. This repo measures the middle of
that budget, the part that turns a fragment stream into something a player and a CDN
can consume, and reports whether the surplus survives it.

It is the standalone home of the code in
[vllm-project/vllm-omni#7015](https://github.com/vllm-project/vllm-omni/pull/7015)
(proposed in [#7014](https://github.com/vllm-project/vllm-omni/issues/7014)), plus
the benchmark harness that produced the numbers below. The four packaging files are
mirrored verbatim from the PR so the two stay diffable.

**Demo:** https://expertvagabond.github.io/omni-hls/ plays the playlist the CI bench
wrote, in hls.js or native HLS, with the runner's own bench tables under it.

## The gap

`WS /v1/realtime/video` already emits fragmented MP4, one fragment per generated
chunk. In the reference config a chunk is 9 frames at 16 fps, 0.5625 seconds. The
two shipped consumers both terminate at a single viewer:

| Consumer | What it does | What it cannot do |
|---|---|---|
| `streaming_video_client.py` | Saves chunks, remuxes to a progressive MP4 at the end | Nothing is watchable until generation finishes |
| `gradio_demo.py` | Appends fragments via MSE in one attached browser | One viewer, no CDN, bespoke player |

fMP4 is the container LL-HLS and DASH already use, so publishing each fragment as an
`EXT-X-PART` is a packaging step, not a transcode. Bytes pass through untouched.
That is what `llhls.py` does, and `bench.py` measures what it costs.

## What is in here

| File | Role |
|---|---|
| `fmp4.py` | Incremental fMP4 splitter. Accepts bytes in any slicing, emits the init segment once plus one buffer per `moof`+`mdat`. Stdlib only. |
| `llhls.py` | LL-HLS packager. Publishes each fragment as a part the moment it arrives, keeps the rolling playlist with `EXT-X-PART` and `EXT-X-PRELOAD-HINT`, writes parent segments for late joiners and non-LL players, records the timings it can observe. |
| `hls_client.py` | Connects to a live vLLM-Omni server and drives the two above. Derives part duration from the first `video.chunk_metadata` rather than assuming a constant. |
| `test_hls_packaging.py` | 10 tests. No GPU, no server, no ffmpeg. Asserts framing independence from whole-buffer down to one byte at a time, and that the playlist references only files that exist. |
| `bench.py` | The benchmark. Paces a fragment stream at a chosen real-time factor through the splitter and packager, reports publish latency and live-edge lead, then decodes the playlist with ffmpeg to prove it is valid. |
| `dash.py` | MPEG-DASH packager over the same fragments. Writes an MPD (`dynamic` while live, `static` on finish) whose `SegmentList` names exactly the files on disk, reads `@codecs`, width and height from the init segment's `avcC`, and can share one directory with the LL-HLS output so the same bytes serve both players. |
| `test_dash.py` | 9 tests. Codec probe on a structurally real `moov`, manifest lists exactly the written files, dynamic-to-static transition, share mode writes no media, media bytes identical to what `llhls.py` writes. |

## Method

The benchmark does not need a GPU. It takes a fragmented MP4 of the server's shape
(640x384, 16 fps, 9-frame fragments, synthesised with ffmpeg `testsrc2` and x264),
feeds it to the splitter in fixed-size slices to simulate transport framing, and then
releases fragment `i` at `(i + 1) * 0.5625 / pace` seconds, which is when a generator
running at `pace` times real time would have finished it. Chunk 0 is not free.

For each part it records:

- **publish latency**: from having the bytes to the playlist advertising them
- **live-edge lead**: media the playlist advertises minus wall time since the first
  part was published, for a player that joined on the first part and has been
  playing since. Below zero, that player has run out of media.

The verdict is **PASS** if lead never went negative and did not shrink part over
part, **DRAINING** if it shrank (a longer clip would stall), **STALL** if it went
negative. Every run is then decoded with `ffmpeg -f null` and frame-counted with
`ffprobe`, so a playlist that looks right but does not play is caught.

## Results

Apple M2, macOS, APFS with 6.5 GB free, Python 3.14, ffmpeg 7. Source: 6.19 s, 11
fragments, 1.09 MB. Five runs per row where marked `x5`; the aggregate row is the
worst case across runs, because the tail is the point of repeating.

**Posted pace (1.161), 8192-byte slices**

| pace | slice B | parts | measured RTF | first part s | pub mean ms | pub p95 ms | pub max ms | lead min s | lead final s | verdict | frames decoded |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1.161 | 8192 | 11 | 1.161 | 0.485 | 0.843 | 1.544 | 2.048 | 0.562 | 1.342 | PASS | 96 |
| 1.161 | 8192 | 11 | 1.161 | 0.486 | 33.702 | 178.070 | 305.268 | 0.413 | 1.341 | PASS | 96 |
| 1.161 | 8192 | 11 | 1.159 | 0.866 | 87.196 | 468.711 | 557.110 | 0.465 | 1.715 | PASS | 96 |
| 1.161 | 8192 | 11 | 1.161 | 0.485 | 4.146 | 18.629 | 31.390 | 0.562 | 1.342 | PASS | 96 |
| 1.161 | 8192 | 11 | 1.161 | 0.488 | 1.931 | 5.214 | 7.128 | 0.562 | 1.344 | PASS | 96 |
| 1.161 | 8192 | x5 | 1.161 | 0.866 | 4.146 | 468.711 | 557.110 | 0.413 | 1.341 | PASS (worst) | 96 |

**Posted pace, 17-byte slices** (transport framing far smaller than any box)

| pace | slice B | parts | measured RTF | first part s | pub mean ms | pub p95 ms | pub max ms | lead min s | lead final s | verdict | frames decoded |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1.161 | 17 | 11 | 1.161 | 0.486 | 1.767 | 6.482 | 8.878 | 0.562 | 1.342 | PASS | 96 |
| 1.161 | 17 | 11 | 1.161 | 0.486 | 1.862 | 6.539 | 7.134 | 0.562 | 1.344 | PASS | 96 |
| 1.161 | 17 | 11 | 1.161 | 0.485 | 1.711 | 6.589 | 10.893 | 0.562 | 1.342 | PASS | 96 |
| 1.161 | 17 | 11 | 1.161 | 0.486 | 6.131 | 30.963 | 58.998 | 0.562 | 1.342 | PASS | 96 |
| 1.161 | 17 | 11 | 1.161 | 0.486 | 0.823 | 1.270 | 1.274 | 0.562 | 1.342 | PASS | 96 |
| 1.161 | 17 | x5 | 1.161 | 0.486 | 1.767 | 30.963 | 58.998 | 0.562 | 1.342 | PASS (worst) | 96 |

**Slower generation, 8192-byte slices**

| pace | slice B | parts | measured RTF | first part s | pub mean ms | pub p95 ms | pub max ms | lead min s | lead final s | verdict | frames decoded |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1.000 | 8192 | 11 | 1.000 | 0.563 | 0.913 | 2.396 | 3.998 | 0.559 | 0.563 | PASS | 96 |
| 0.900 | 8192 | 11 | 0.900 | 0.626 | 0.890 | 1.778 | 2.558 | -0.062 | -0.062 | STALL | 96 |

## Findings

**1. Packaging itself costs about a millisecond per part.** The median run publishes
a part in under 2 ms mean, and the best runs under 1 ms. Splitting the whole 1.09 MB
source costs 1 ms at 8192-byte slices. The delivery path is not where the latency
lives; "faster than playback" survives packaging on CPU cost alone.

**2. The tail is the filesystem, not the code.** One run in five at the posted pace
hit a 557 ms publish, another 305 ms. Both were APFS write stalls on a disk with
6.5 GB free; the same run at 17-byte slices, moments later, peaked at 59 ms. The
same five runs on a GitHub Ubuntu runner (run 33763828813) had a mean of 0.5 ms and a
worst case of 1.1 ms, no tail at all. Any packager that writes parts to disk inherits
whatever its disk does, which is why production origins publish from memory or
tmpfs. Measured, not assumed, and it changes the next point.

**3. The posted surplus is smaller than an LL-HLS hold-back.** At pace 1.161, a
player that joins on the first part starts with 0.5625 s of lead, one part, and gains
78 ms per part. Over the full 10.1 s clip that adds up to the 1.4 s surplus in the
headline (10.1 minus 8.7). The playlist advertises `PART-HOLD-BACK` of three parts,
1.6875 s, per the spec's minimum. So a low-latency player joining late never reaches
a full hold-back of margin inside the clip, and an early joiner sitting on one part
of lead is one write stall away from a rebuffer. The 557 ms stall above took minimum
lead from 0.562 s to 0.413 s. It held, with 0.4 s to spare.

**4. Framing does not matter to correctness, only to CPU.** 17-byte slices, where
`ftyp` and `moov` land in different reads and every box straddles a boundary, produce
byte-identical output and the same lead. Splitting cost rises from 1 ms to 187 ms for
the whole file because the splitter rescans its buffer per feed, which is irrelevant
at real WebSocket frame sizes and is the trade the PR makes for stdlib-only code.

**5. The edge is sharp.** At pace 1.0 lead holds flat at one part for the whole clip.
At pace 0.9 it drains 62 ms per part and goes negative on the eleventh, so a 6 s clip
stalls on its last part and a 10 s clip stalls at about part 9. The gap between
"faster than playback" and a rebuffer is a 10% swing in generation speed.

## DASH from the same bytes

The issue said DASH would be a small addition on the same splitter. It is: `dash.py`
is 200 lines and writes no media of its own when pointed at the LL-HLS directory.

```bash
python3 dash.py --source out/source.fmp4 --out out/hls --share
```

produces `out/hls/stream.mpd` next to `stream.m3u8`, both referencing the same
`init.mp4` and `partNNNNN.m4s` files. The MPD carries `codecs="avc1.42C016"` read
from the init segment, which ffprobe confirms as Constrained Baseline level 2.2.
CI decodes the MPD with ffmpeg on every push. macOS Homebrew ffmpeg lacks
libxml2 and cannot demux an MPD, so the local check is to decode the segments the
manifest lists; the test suite checks that list against the directory.

## What this does not show

- **Not a live server.** The source is ffmpeg `testsrc2` encoded with x264 into the
  server's fragment shape. Generation was built and measured on an Apple M2 with no
  GPU. `hls_client.py` is the piece that runs against `WS /v1/realtime/video`, and it
  has not been exercised against a real endpoint yet.
- **Only the middle of the budget.** Render is simulated by the pacing. Fetch, CDN,
  player buffer and decode are not measured. Those are the terms a real
  prompt-to-glass number needs, and they are the ones this harness is built to sit
  in front of.
- **No VMAF.** Bytes are copied, so quality is identical to what the server produced
  and a VMAF run would report 100. It becomes meaningful once a bitrate ladder or a
  transcode enters the path.
- **One rendition.** A ladder is the obvious next step for real distribution and
  would move the numbers, since it adds a transcode.
- **One machine.** The tail in finding 2 is specific to this disk. CI runs the
  benchmark on an Ubuntu runner and publishes its table to the job summary, so there
  is a second data point per commit.

## Reproduce

```bash
python3 -m pytest -q                               # 19 tests, no ffmpeg needed
python3 bench.py --runs 5 --markdown                # synthesises the source, needs ffmpeg
python3 bench.py --slice 17 --runs 5 --markdown
python3 bench.py --pace 1.0 --markdown
python3 bench.py --pace 0.9 --markdown
```

Every run writes `out/hls/stream.m3u8` plus its parts and segments. Open it in any
LL-HLS-capable player, or check it the way the bench does:

```bash
ffmpeg -allowed_extensions ALL -i out/hls/stream.m3u8 -f null -
ffprobe -allowed_extensions ALL -count_frames -select_streams v:0 \
  -show_entries stream=nb_read_frames out/hls/stream.m3u8
```

Against a live server:

```bash
python3 hls_client.py --host HOST --port 8000 --prompt "..." --out out/live
```

## License

Apache-2.0, matching vLLM. The four packaging files carry the vLLM project SPDX
header because they are the PR #7015 sources, unchanged.
