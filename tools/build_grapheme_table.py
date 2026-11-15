#!/usr/bin/env python3
"""Regenerate the UAX #29 grapheme-break tables used by the tokenizer.

The XLM-R normalizer segments text into extended grapheme clusters before it
applies the sentencepiece charsmap, so both the Python tokenizer
(``app_combined._grapheme_clusters``) and the Kotlin tokenizer
(``SpmTokenizer.kt``) need the same property tables. Rather than hand-maintain
two copies, this script derives both from the Unicode Character Database:

  * ``GraphemeBreakProperty.txt``  (classes CR/LF/Control/Extend/ZWJ/RI/Prepend/
    SpacingMark/L/V/T/LV/LVT)
  * ``emoji-data.txt``             (Extended_Pictographic, for GB11)

It prints the packed base64 blob plus the two literals, so a Unicode bump is a
matter of re-running it and pasting the output.

Usage:
    python3 tools/build_grapheme_table.py GraphemeBreakProperty.txt emoji-data.txt
"""

import base64
import struct
import sys
import textwrap
import zlib

CLASS_CODES = {
    'CR': 1, 'LF': 2, 'Control': 3, 'Extend': 4, 'ZWJ': 5,
    'Regional_Indicator': 6, 'Prepend': 7, 'SpacingMark': 8,
    'L': 9, 'V': 10, 'T': 11, 'LV': 12, 'LVT': 13,
}


def _parse_ranges(path, wanted):
    out = {}
    with open(path, encoding='utf-8') as handle:
        for line in handle:
            line = line.split('#')[0].strip()
            if not line or ';' not in line:
                continue
            span, prop = (x.strip() for x in line.split(';'))
            if wanted is not None and prop != wanted:
                continue
            if '..' in span:
                start, end = (int(x, 16) for x in span.split('..'))
            else:
                start = end = int(span, 16)
            out.setdefault(prop, []).append((start, end))
    return out


def _merge(ranges):
    ranges = sorted(ranges)
    merged = []
    start, end = ranges[0]
    for a, b in ranges[1:]:
        if a <= end + 1:
            end = max(end, b)
        else:
            merged.append((start, end))
            start, end = a, b
    merged.append((start, end))
    return merged


def _varint(value, out):
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            break


def build(grapheme_path, emoji_path):
    gbp = _parse_ranges(grapheme_path, None)
    items = []
    for prop, code in CLASS_CODES.items():
        for a, b in gbp.get(prop, []):
            items.append((a, b, code))
    items.sort()
    # Coalesce neighbouring code points that share a class.
    gbr = []
    start, end, cls = items[0][0], items[0][1], items[0][2]
    for a, b, c in items[1:]:
        if a == end + 1 and c == cls:
            end = b
        else:
            gbr.append((start, end, cls))
            start, end, cls = a, b, c
    gbr.append((start, end, cls))

    ep_raw = _parse_ranges(emoji_path, 'Extended_Pictographic')['Extended_Pictographic']
    ep = _merge(ep_raw)

    blob = bytearray()
    _varint(len(gbr), blob)
    prev = 0
    for a, b, c in gbr:
        _varint(a - prev, blob)
        _varint(b - a, blob)
        blob.append(c)
        prev = b + 1
    _varint(len(ep), blob)
    prev = 0
    for a, b in ep:
        _varint(a - prev, blob)
        _varint(b - a, blob)
        prev = b + 1

    encoded = base64.b64encode(zlib.compress(bytes(blob), 9)).decode()
    print(f"# {len(gbr)} grapheme ranges, {len(ep)} emoji ranges, "
          f"{len(encoded)} base64 chars")
    print("# --- Python literal (app_combined._GRAPHEME_TABLE_B64) ---")
    print("_GRAPHEME_TABLE_B64 = (")
    for line in textwrap.wrap(encoded, 88):
        print(f'    "{line}"')
    print(")")
    print()
    print("// --- Kotlin constant (SpmTokenizer.GRAPHEME_TABLE_B64) ---")
    for line in textwrap.wrap(encoded, 88):
        print(f'        "{line}" +')
    return encoded


if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    build(sys.argv[1], sys.argv[2])
