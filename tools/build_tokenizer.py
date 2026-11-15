#!/usr/bin/env python3
"""Build the compact sentencepiece tokenizer asset for the Android build.

The multilingual MiniLM model uses a XLM-R sentencepiece Unigram tokenizer. We
only need two things from the upstream ``tokenizer.json``: the precompiled
normalizer charsmap (a Darts double-array trie plus rewrite strings) and the
Unigram vocabulary (piece -> log-probability). Both are packed here into a small
binary that ``app_combined._SpmTokenizer`` reads directly, so the APK does not
carry the 9 MB HuggingFace JSON.

Usage:
    python3 tools/build_tokenizer.py <tokenizer.json> <out.bin>

The input is the pinned revision of
``sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2``.
"""

import base64
import json
import struct
import sys

MAGIC = b"MSPT"  # Mini Search Piece Tokenizer
VERSION = 1


def build(src, dst):
    with open(src, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    norm = data["normalizer"]
    if norm.get("type") != "Precompiled":
        raise SystemExit(f"unsupported normalizer: {norm.get('type')}")
    charsmap = base64.b64decode(norm["precompiled_charsmap"])

    model = data["model"]
    if model.get("type") != "Unigram":
        raise SystemExit(f"unsupported model: {model.get('type')}")
    vocab = model["vocab"]
    unk_id = int(model["unk_id"])

    # Longest piece, in UTF-8 bytes: bounds the Viterbi inner loop.
    max_piece = max(len(p.encode("utf-8")) for p, _ in vocab)

    out = bytearray()
    out += MAGIC
    out += struct.pack("<H", VERSION)
    out += struct.pack("<i", unk_id)
    out += struct.pack("<i", max_piece)
    out += struct.pack("<I", len(charsmap))
    out += charsmap
    out += struct.pack("<I", len(vocab))
    for piece, score in vocab:
        pb = piece.encode("utf-8")
        out += struct.pack("<H", len(pb))
        out += pb
        out += struct.pack("<f", float(score))

    with open(dst, "wb") as fh:
        fh.write(out)
    print(f"{dst}: {len(out)} bytes, {len(vocab)} pieces, "
          f"charsmap {len(charsmap)} bytes, unk_id={unk_id}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    build(sys.argv[1], sys.argv[2])
