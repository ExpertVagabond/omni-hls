# SPDX-License-Identifier: Apache-2.0
"""MPEG-DASH packager for the same fragmented-MP4 fragment stream.

LL-HLS and DASH consume the same fMP4 segments; only the manifest differs. This
writes an MPD alongside the LL-HLS playlist so the same bytes on disk serve both
kinds of player. The media files are byte-identical to what ``llhls.py`` writes,
and can be shared with it (``write_media=False``) when both packagers publish to
one directory.

The manifest is ``type="dynamic"`` while fragments are arriving and becomes
``type="static"`` with a ``mediaPresentationDuration`` on ``finish()``. A
``SegmentList`` with explicit ``SegmentURL`` entries is used rather than a
``SegmentTemplate`` so the manifest advertises exactly the files that exist;
that is the property the tests check.

Codec, width and height are read from the init segment's ``avcC`` and sample
entry so ``@codecs`` is correct without the caller knowing the encoder.

Command line::

    python3 dash.py --source out/source.fmp4 --out out/dash
    python3 dash.py --source out/source.fmp4 --out out/hls --share   # reuse LL-HLS media
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import quoteattr

sys.path.insert(0, str(Path(__file__).parent))

from fmp4 import FragmentedMP4Splitter, iter_boxes  # noqa: E402

CONTAINERS = {"moov", "trak", "mdia", "minf", "stbl"}


def _children(buf: bytes, start: int, end: int):
    """Yield (type, start, end) for boxes inside ``buf[start:end]``."""
    for box in iter_boxes(buf[:end], start):
        yield box.type, box.start, box.end


def _find(buf: bytes, path: list[str], start: int = 0, end: int | None = None) -> tuple[int, int] | None:
    """Locate a nested box by path, e.g. ["moov", "trak", "mdia"]."""
    end = len(buf) if end is None else end
    for typ, s, e in _children(buf, start, end):
        if typ != path[0]:
            continue
        if len(path) == 1:
            return s, e
        found = _find(buf, path[1:], s + 8, e)
        if found:
            return found
    return None


@dataclass
class VideoInfo:
    codecs: str | None
    width: int | None
    height: int | None


def probe_init_segment(init: bytes) -> VideoInfo:
    """Read codecs/width/height from the first video sample entry in ``moov``.

    Returns ``None`` fields rather than raising on anything unexpected: the MPD
    is still valid without ``@codecs``, and a player will sniff the init segment.
    """
    stsd = _find(init, ["moov", "trak", "mdia", "minf", "stbl", "stsd"])
    if not stsd:
        return VideoInfo(None, None, None)
    s, e = stsd
    # stsd: 8 header + 4 version/flags + 4 entry_count, then sample entries.
    entries = list(_children(init, s + 16, e))
    if not entries:
        return VideoInfo(None, None, None)
    typ, es, ee = entries[0]
    # Visual sample entry: 8 header, 6 reserved, 2 data_reference_index,
    # 16 pre_defined/reserved, then width and height as u16.
    width = height = None
    if ee - es >= 8 + 6 + 2 + 16 + 4:
        width, height = struct.unpack_from(">HH", init, es + 8 + 6 + 2 + 16)
    codecs: str | None = typ
    if typ in ("avc1", "avc3"):
        # Child boxes start after the 78-byte visual sample entry body.
        for ctyp, cs, ce in _children(init, es + 8 + 78, ee):
            if ctyp == "avcC" and ce - cs >= 8 + 4:
                _ver, profile, compat, level = struct.unpack_from(">BBBB", init, cs + 8)
                codecs = f"{typ}.{profile:02X}{compat:02X}{level:02X}"
                break
    return VideoInfo(codecs, width, height)


@dataclass
class DASHPackager:
    """Write an MPD as fragments arrive; media files match ``llhls.py`` exactly."""

    out_dir: Path
    segment_duration: float
    timescale: int = 90000
    manifest_name: str = "stream.mpd"
    write_media: bool = True

    _uris: list[str] = field(default_factory=list)
    _bytes: int = 0
    _info: VideoInfo | None = None
    _t0: float | None = None
    _availability_start: str | None = None

    def __post_init__(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)

    def start(self, init_segment: bytes) -> None:
        self._t0 = time.perf_counter()
        self._availability_start = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._info = probe_init_segment(init_segment)
        if self.write_media:
            (self.out_dir / "init.mp4").write_bytes(init_segment)

    def add_fragment(self, data: bytes) -> str:
        if self._t0 is None:
            raise RuntimeError("start() must be called with the init segment first")
        uri = f"part{len(self._uris):05d}.m4s"
        if self.write_media:
            (self.out_dir / uri).write_bytes(data)
        self._uris.append(uri)
        self._bytes += len(data)
        self._write_manifest(ended=False)
        return uri

    def finish(self) -> None:
        self._write_manifest(ended=True)

    # -- manifest ------------------------------------------------------------

    def _write_manifest(self, ended: bool) -> None:
        assert self._info is not None
        seg_ticks = round(self.segment_duration * self.timescale)
        total = len(self._uris) * self.segment_duration
        bandwidth = int(self._bytes * 8 / max(total, self.segment_duration))

        mpd_attrs = [
            'xmlns="urn:mpeg:dash:schema:mpd:2011"',
            'profiles="urn:mpeg:dash:profile:isoff-live:2011"',
            f'minBufferTime="PT{self.segment_duration:.4f}S"',
        ]
        if ended:
            mpd_attrs += ['type="static"', f'mediaPresentationDuration="PT{total:.4f}S"']
        else:
            mpd_attrs += [
                'type="dynamic"',
                f'availabilityStartTime="{self._availability_start}"',
                f'minimumUpdatePeriod="PT{self.segment_duration:.4f}S"',
                f'timeShiftBufferDepth="PT{total:.4f}S"',
            ]
        rep_attrs = [f'id="v0"', f'bandwidth="{bandwidth}"']
        if self._info.codecs:
            rep_attrs.append(f"codecs={quoteattr(self._info.codecs)}")
        if self._info.width and self._info.height:
            rep_attrs += [f'width="{self._info.width}"', f'height="{self._info.height}"']

        lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            f"<MPD {' '.join(mpd_attrs)}>",
            '  <Period id="0" start="PT0S">',
            '    <AdaptationSet mimeType="video/mp4" segmentAlignment="true" startWithSAP="1">',
            f"      <Representation {' '.join(rep_attrs)}>",
            f'        <SegmentList timescale="{self.timescale}" duration="{seg_ticks}">',
            '          <Initialization sourceURL="init.mp4"/>',
        ]
        lines += [f'          <SegmentURL media="{u}"/>' for u in self._uris]
        lines += [
            "        </SegmentList>",
            "      </Representation>",
            "    </AdaptationSet>",
            "  </Period>",
            "</MPD>",
        ]
        (self.out_dir / self.manifest_name).write_text("\n".join(lines) + "\n")

    def report(self) -> dict:
        assert self._info is not None
        return {
            "segments": len(self._uris),
            "media_seconds": round(len(self._uris) * self.segment_duration, 4),
            "bytes": self._bytes,
            "codecs": self._info.codecs,
            "width": self._info.width,
            "height": self._info.height,
            "manifest": str(self.out_dir / self.manifest_name),
        }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Package a fragmented MP4 as MPEG-DASH")
    ap.add_argument("--source", type=Path, required=True, help="fragmented MP4 to package")
    ap.add_argument("--out", type=Path, default=Path("out/dash"))
    ap.add_argument("--fps", type=int, default=16)
    ap.add_argument("--frames-per-chunk", type=int, default=9)
    ap.add_argument("--slice", type=int, default=8192, help="bytes per feed() call")
    ap.add_argument("--share", action="store_true",
                    help="do not write media; --out already holds init.mp4 and partNNNNN.m4s from llhls.py")
    args = ap.parse_args(argv)

    data = args.source.read_bytes()
    splitter = FragmentedMP4Splitter()
    packager = DASHPackager(out_dir=args.out, segment_duration=args.frames_per_chunk / args.fps,
                            write_media=not args.share)
    started = False
    for i in range(0, len(data), args.slice):
        for frag in splitter.feed(data[i:i + args.slice]):
            if not started:
                assert splitter.init_segment is not None
                packager.start(splitter.init_segment)
                started = True
            packager.add_fragment(frag)
    if not started:
        sys.exit("source produced no fragments; is it a fragmented MP4?")
    packager.finish()
    print(json.dumps(packager.report(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
