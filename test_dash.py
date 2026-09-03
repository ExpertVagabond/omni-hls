# SPDX-License-Identifier: Apache-2.0
"""DASH packager tests. No GPU, no server, no ffmpeg.

Builds a minimal but structurally real init segment in memory, with the nested
moov/trak/mdia/minf/stbl/stsd/avc1/avcC chain, so the codec probe is exercised
on the layout a real encoder produces rather than on a stub.
"""

from __future__ import annotations

import struct
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from dash import DASHPackager, media_name, probe_init_segment  # noqa: E402
from fmp4 import FragmentedMP4Splitter  # noqa: E402

NS = {"m": "urn:mpeg:dash:schema:mpd:2011"}


def box(box_type: str, payload: bytes = b"") -> bytes:
    return struct.pack(">I", 8 + len(payload)) + box_type.encode("latin-1") + payload


def avc1_entry(width: int, height: int, profile: int, compat: int, level: int) -> bytes:
    body = b"\x00" * 6 + struct.pack(">H", 1)          # reserved, data_reference_index
    body += b"\x00" * 16                                # pre_defined / reserved
    body += struct.pack(">HH", width, height)
    body += b"\x00" * (78 - len(body))                  # rest of the visual sample entry
    avcc = box("avcC", bytes([1, profile, compat, level]) + b"\xff\xe1\x00\x00")
    return box("avc1", body + avcc)


def init_segment(width: int = 640, height: int = 384) -> bytes:
    stsd = box("stsd", b"\x00" * 4 + struct.pack(">I", 1) + avc1_entry(width, height, 0x42, 0xC0, 0x1E))
    stbl = box("stbl", stsd)
    minf = box("minf", stbl)
    mdia = box("mdia", minf)
    trak = box("trak", mdia)
    moov = box("moov", box("mvhd", b"\x00" * 100) + trak)
    return box("ftyp", b"isom" * 2) + moov


def fragments(n: int = 5, payload: int = 512) -> list[bytes]:
    return [box("moof", struct.pack(">I", i) + b"\x00" * 24) + box("mdat", bytes([i % 251]) * payload) for i in range(n)]


def test_probe_reads_codec_and_dimensions():
    info = probe_init_segment(init_segment(640, 384))
    assert info.codecs == "avc1.42C01E"
    assert (info.width, info.height) == (640, 384)


def test_probe_is_lenient_on_garbage():
    info = probe_init_segment(b"\x00" * 32)
    assert info.codecs is None and info.width is None


def test_manifest_lists_exactly_the_files_written(tmp_path: Path):
    pkg = DASHPackager(out_dir=tmp_path, segment_duration=9 / 16)
    pkg.start(init_segment())
    for frag in fragments(6):
        pkg.add_fragment(frag)
    pkg.finish()

    root = ET.parse(tmp_path / "stream.mpd").getroot()
    assert root.get("type") == "static"
    assert root.get("mediaPresentationDuration") == f"PT{6 * 9 / 16:.4f}S"
    rep = root.find(".//m:Representation", NS)
    assert rep is not None and rep.get("codecs") == "avc1.42C01E"
    assert (rep.get("width"), rep.get("height")) == ("640", "384")

    tmpl = root.find(".//m:SegmentTemplate", NS)
    assert tmpl is not None
    assert tmpl.get("duration") is None, "timeline and @duration are mutually exclusive"
    assert (tmp_path / tmpl.get("initialization")).exists()
    assert tmpl.get("media") == "part$Number%05d$.m4s"
    start = int(tmpl.get("startNumber"))
    s_entries = tmpl.findall("m:SegmentTimeline/m:S", NS)
    assert len(s_entries) == 1
    s = s_entries[0]
    assert int(s.get("d")) == round(9 / 16 * int(tmpl.get("timescale")))
    count = int(s.get("r")) + 1
    assert count == 6, "timeline must state exactly the segment count"

    # Expand the template the way a player would and compare to the directory.
    urls = [media_name(start + i) for i in range(count)]
    assert urls == [f"part{i:05d}.m4s" for i in range(6)]
    media_on_disk = sorted(p.name for p in tmp_path.glob("part*.m4s"))
    assert media_on_disk == urls


def test_manifest_is_dynamic_until_finished(tmp_path: Path):
    pkg = DASHPackager(out_dir=tmp_path, segment_duration=0.5)
    pkg.start(init_segment())
    pkg.add_fragment(fragments(1)[0])
    root = ET.parse(tmp_path / "stream.mpd").getroot()
    assert root.get("type") == "dynamic"
    assert root.get("availabilityStartTime")
    assert root.get("minimumUpdatePeriod") == "PT0.5000S"
    assert root.get("mediaPresentationDuration") is None
    pkg.finish()
    assert ET.parse(tmp_path / "stream.mpd").getroot().get("type") == "static"


def test_share_mode_writes_no_media(tmp_path: Path):
    pkg = DASHPackager(out_dir=tmp_path, segment_duration=0.5, write_media=False)
    pkg.start(init_segment())
    pkg.add_fragment(fragments(1)[0])
    pkg.finish()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["stream.mpd"]


def test_media_bytes_match_llhls_packager(tmp_path: Path):
    """The point of sharing a directory: both packagers write identical files."""
    from llhls import LLHLSPackager

    init, frags = init_segment(), fragments(4)
    hls_dir, dash_dir = tmp_path / "hls", tmp_path / "dash"
    hls = LLHLSPackager(out_dir=hls_dir, part_duration=0.5)
    dash = DASHPackager(out_dir=dash_dir, segment_duration=0.5)
    hls.start(init)
    dash.start(init)
    for f in frags:
        hls.add_fragment(f)
        dash.add_fragment(f)
    hls.finish()
    dash.finish()
    for name in ["init.mp4"] + [f"part{i:05d}.m4s" for i in range(4)]:
        assert (hls_dir / name).read_bytes() == (dash_dir / name).read_bytes()


@pytest.mark.parametrize("slice_size", [None, 17, 1])
def test_end_to_end_through_splitter(tmp_path: Path, slice_size):
    stream = init_segment() + b"".join(fragments(3))
    splitter = FragmentedMP4Splitter()
    pkg = DASHPackager(out_dir=tmp_path, segment_duration=0.5)
    started = False
    step = slice_size or len(stream)
    for i in range(0, len(stream), step):
        for frag in splitter.feed(stream[i:i + step]):
            if not started:
                pkg.start(splitter.init_segment)
                started = True
            pkg.add_fragment(frag)
    pkg.finish()
    assert pkg.report()["segments"] == 3
    assert pkg.report()["codecs"] == "avc1.42C01E"
