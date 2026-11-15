package cz.minisearch.app

import android.content.Context
import java.util.Base64
import java.util.zip.Inflater
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * Pure-Kotlin port of the XLM-R sentencepiece Unigram tokenizer used by
 * ``paraphrase-multilingual-MiniLM-L12-v2``.
 *
 * It is a byte-for-byte port of ``app_combined._SpmTokenizer`` (itself validated
 * against HuggingFace ``tokenizers`` 0.20.3): UAX #29 grapheme segmentation, the
 * precompiled charsmap normalizer (Darts double-array trie) and Unigram Viterbi
 * with fused unknown tokens. The golden vectors in
 * ``src/test/resources/tokenizer_golden.json`` pin the behaviour.
 *
 * Tokenization is done here rather than in Python because the ONNX model needs
 * integer ids: keeping the tokenizer in Kotlin avoids one Chaquopy call per
 * token and keeps the inference path self-contained.
 */
object SpmTokenizer {

    private const val MAGIC = 0x5450534D // "MSPT" read as a little-endian int
    private const val VERSION = 1

    private const val CLASS_OTHER = 0
    private const val CLASS_CR = 1
    private const val CLASS_LF = 2
    private const val CLASS_CONTROL = 3
    private const val CLASS_EXTEND = 4
    private const val CLASS_ZWJ = 5
    private const val CLASS_RI = 6
    private const val CLASS_PREPEND = 7
    private const val CLASS_SPACINGMARK = 8
    private const val CLASS_L = 9
    private const val CLASS_V = 10
    private const val CLASS_T = 11
    private const val CLASS_LV = 12
    private const val CLASS_LVT = 13

    @Volatile
    private var loadFailed = false

    private lateinit var trie: IntArray
    private lateinit var pool: ByteArray
    private lateinit var pieces: Array<String>
    private lateinit var scores: HashMap<String, Float>
    private lateinit var pieceIds: HashMap<String, Int>
    private var unkId = 0
    private var unkScore = 0f
    private var maxPieceChars = 0

    private lateinit var gbrStarts: IntArray
    private lateinit var gbrEnds: IntArray
    private lateinit var gbrClasses: ByteArray
    private lateinit var epStarts: IntArray
    private lateinit var epEnds: IntArray

    /** True once [load] has successfully read the asset. */
    @Volatile
    var isReady: Boolean = false
        private set

    /** Ids for ``<s>`` / ``</s>`` in the pinned vocabulary. */
    const val BOS_ID = 0
    const val EOS_ID = 2
    const val MAX_LENGTH = 128

    /**
     * Load the packed tokenizer asset. Safe to call from any thread; the heavy
     * work is done once and guarded by `isReady`/`loadFailed`.
     */
    @Synchronized
    fun load(context: Context): Boolean {
        if (isReady) return true
        if (loadFailed) return false
        return try {
            val raw = context.assets.open("ml/tokenizer_spm.bin").use { it.readBytes() }
            loadFromBytes(raw)
            true
        } catch (t: Throwable) {
            android.util.Log.e("SpmTokenizer", "tokenizer load failed", t)
            loadFailed = true
            false
        }
    }

    /**
     * Load from an in-memory copy of the packed asset. Used by the JVM tests,
     * which have no Android [Context] and read the file straight from disk.
     */
    @Synchronized
    fun loadFromBytes(raw: ByteArray) {
        parse(raw)
        loadGraphemeTables()
        isReady = true
    }

    private fun parse(data: ByteArray) {
        val buf = ByteBuffer.wrap(data).order(ByteOrder.LITTLE_ENDIAN)
        require(buf.int == MAGIC) { "tokenizer_spm.bin: bad magic" }
        val version = buf.short.toInt() and 0xFFFF
        require(version == VERSION) { "tokenizer_spm.bin: bad version $version" }
        unkId = buf.int
        val maxPieceBytes = buf.int
        val charsmapLen = buf.int
        val charsmap = ByteArray(charsmapLen)
        buf.get(charsmap)
        val vocabLen = buf.int

        val cm = ByteBuffer.wrap(charsmap).order(ByteOrder.LITTLE_ENDIAN)
        val trieSize = cm.int
        trie = IntArray(trieSize / 4)
        cm.asIntBuffer().get(trie)
        pool = ByteArray(charsmap.size - 4 - trieSize)
        System.arraycopy(charsmap, 4 + trieSize, pool, 0, pool.size)

        pieces = Array(vocabLen) { "" }
        scores = HashMap(vocabLen * 2)
        pieceIds = HashMap(vocabLen * 2)
        var minScore = Float.MAX_VALUE
        for (i in 0 until vocabLen) {
            val plen = buf.short.toInt() and 0xFFFF
            val pieceBytes = ByteArray(plen)
            buf.get(pieceBytes)
            val piece = String(pieceBytes, Charsets.UTF_8)
            val score = buf.float
            pieces[i] = piece
            scores[piece] = score
            pieceIds[piece] = i
            if (score < minScore) minScore = score
        }
        unkScore = minScore - 10f
        // The reference bounds the Viterbi lookahead by the longest piece in
        // UTF-8 bytes; a character bound of the same number is a safe, exact
        // superset because any longer piece cannot exist in the vocabulary.
        maxPieceChars = maxPieceBytes
    }

    private fun decodeGraphemeTable(): ByteArray {
        val b64 = GRAPHEME_TABLE_B64.joinToString("")
        val compressed = Base64.getDecoder().decode(b64)
        val inflater = Inflater()
        inflater.setInput(compressed)
        val out = java.io.ByteArrayOutputStream(compressed.size * 4)
        val buffer = ByteArray(4096)
        while (!inflater.finished()) {
            val n = inflater.inflate(buffer)
            if (n == 0 && inflater.needsInput()) break
            out.write(buffer, 0, n)
        }
        inflater.end()
        return out.toByteArray()
    }

    private fun loadGraphemeTables() {
        val raw = decodeGraphemeTable()
        var pos = 0
        fun readVarint(): Int {
            var shift = 0
            var value = 0
            while (true) {
                val b = raw[pos++].toInt() and 0xFF
                value = value or ((b and 0x7F) shl shift)
                if (b and 0x80 == 0) return value
                shift += 7
            }
        }
        val gbrCount = readVarint()
        gbrStarts = IntArray(gbrCount)
        gbrEnds = IntArray(gbrCount)
        gbrClasses = ByteArray(gbrCount)
        var start = 0
        for (i in 0 until gbrCount) {
            start += readVarint()
            val len = readVarint()
            gbrStarts[i] = start
            gbrEnds[i] = start + len
            gbrClasses[i] = raw[pos++]
            start += len + 1
        }
        val epCount = readVarint()
        epStarts = IntArray(epCount)
        epEnds = IntArray(epCount)
        start = 0
        for (i in 0 until epCount) {
            start += readVarint()
            val len = readVarint()
            epStarts[i] = start
            epEnds[i] = start + len
            start += len + 1
        }
    }

    private fun graphemeClass(cp: Int): Int {
        var lo = 0
        var hi = gbrStarts.size - 1
        while (lo <= hi) {
            val mid = (lo + hi) ushr 1
            when {
                cp < gbrStarts[mid] -> hi = mid - 1
                cp > gbrEnds[mid] -> lo = mid + 1
                else -> return gbrClasses[mid].toInt()
            }
        }
        return CLASS_OTHER
    }

    private fun isExtendedPictographic(cp: Int): Boolean {
        var lo = 0
        var hi = epStarts.size - 1
        while (lo <= hi) {
            val mid = (lo + hi) ushr 1
            when {
                cp < epStarts[mid] -> hi = mid - 1
                cp > epEnds[mid] -> lo = mid + 1
                else -> return true
            }
        }
        return false
    }

    /** Split [text] into extended grapheme clusters (UAX #29 GB1-GB13). */
    fun graphemeClusters(text: String): List<String> {
        if (text.isEmpty()) return emptyList()
        val cps = text.codePoints().toArray()
        val n = cps.size
        val classes = IntArray(n) { graphemeClass(cps[it]) }
        val picto = BooleanArray(n) { isExtendedPictographic(cps[it]) }
        val out = ArrayList<String>()
        var i = 0
        while (i < n) {
            val start = i
            i++
            while (i < n) {
                val prev = classes[i - 1]
                val cur = classes[i]
                if (prev == CLASS_CR && cur == CLASS_LF) { i++; continue }
                if (cur == CLASS_CR || cur == CLASS_LF || cur == CLASS_CONTROL) break
                if (prev == CLASS_CR || prev == CLASS_LF || prev == CLASS_CONTROL) break
                if (prev == CLASS_L &&
                    (cur == CLASS_L || cur == CLASS_V || cur == CLASS_LV || cur == CLASS_LVT)) { i++; continue }
                if ((prev == CLASS_LV || prev == CLASS_V) && (cur == CLASS_V || cur == CLASS_T)) { i++; continue }
                if ((prev == CLASS_LVT || prev == CLASS_T) && cur == CLASS_T) { i++; continue }
                if (cur == CLASS_EXTEND || cur == CLASS_ZWJ) { i++; continue }
                if (cur == CLASS_SPACINGMARK) { i++; continue }
                if (prev == CLASS_PREPEND) { i++; continue }
                if (prev == CLASS_ZWJ && picto[i]) {
                    var k = i - 2
                    while (k >= start && classes[k] == CLASS_EXTEND) k--
                    if (k >= start && picto[k]) { i++; continue }
                }
                if (prev == CLASS_RI && cur == CLASS_RI) {
                    var count = 0
                    var k = i - 1
                    while (k >= start && classes[k] == CLASS_RI) { count++; k-- }
                    if (count % 2 == 1) { i++; continue }
                }
                break
            }
            out.add(String(cps, start, i - start))
        }
        return out
    }

    private fun trieCommonPrefix(key: ByteArray): Int {
        var node = 0
        var unit = trie[node]
        node = node xor dartsOffset(unit)
        for (byte in key) {
            val b = byte.toInt() and 0xFF
            if (b == 0) break
            node = node xor b
            if (node >= trie.size) return -1
            unit = trie[node]
            if (unit and (1 shl 31) != 0 || (unit and 0xFF) != b) return -1
            node = node xor dartsOffset(unit)
            if ((unit shr 8) and 1 == 1) return trie[node] and 0x7FFFFFFF
        }
        return -1
    }

    private fun dartsOffset(unit: Int): Int =
        (unit ushr 10) shl ((unit and (1 shl 9)) ushr 6)

    private fun transform(chunk: String): String? {
        val index = trieCommonPrefix(chunk.toByteArray(Charsets.UTF_8))
        if (index < 0) return null
        var end = index
        while (end < pool.size && pool[end].toInt() != 0) end++
        return String(pool, index, end - index, Charsets.UTF_8)
    }

    /** Apply the sentencepiece precompiled charsmap normalizer. */
    fun normalize(text: String): String {
        val sb = StringBuilder(text.length)
        for (grapheme in graphemeClusters(text)) {
            if (grapheme.toByteArray(Charsets.UTF_8).size < 6) {
                val replaced = transform(grapheme)
                if (replaced != null) { sb.append(replaced); continue }
            }
            for (ch in grapheme.codePoints().toArray()) {
                val piece = String(Character.toChars(ch))
                val replaced = transform(piece)
                sb.append(replaced ?: piece)
            }
        }
        return sb.toString()
    }

    private fun unigram(sentence: String): IntArray {
        if (sentence.isEmpty()) return IntArray(0)
        val n = sentence.length
        val bestScore = FloatArray(n + 1) { Float.NEGATIVE_INFINITY }
        val bestStart = IntArray(n + 1)
        val bestId = IntArray(n + 1)
        bestScore[0] = 0f
        var pos = 0
        while (pos < n) {
            val here = bestScore[pos]
            val singleBytes = sentence.substring(pos, pos + 1).toByteArray(Charsets.UTF_8).size
            var hasSingle = false
            val limit = minOf(n - pos, maxPieceChars)
            for (k in 1..limit) {
                val piece = sentence.substring(pos, pos + k)
                val score = scores[piece] ?: continue
                val target = pos + k
                val candidate = score + here
                if (candidate > bestScore[target]) {
                    bestScore[target] = candidate
                    bestStart[target] = pos
                    bestId[target] = pieceIds[piece]!!
                }
                if (piece.toByteArray(Charsets.UTF_8).size == singleBytes) hasSingle = true
            }
            if (!hasSingle) {
                val target = pos + 1
                val candidate = unkScore + here
                if (candidate > bestScore[target]) {
                    bestScore[target] = candidate
                    bestStart[target] = pos
                    bestId[target] = unkId
                }
            }
            pos++
        }
        // Backtrack and fuse adjacent unknown tokens into one, matching the
        // reference's ``fuse_unk`` post-processing.
        val nodes = ArrayList<IntArray>()
        var end = n
        while (end > 0) {
            nodes.add(intArrayOf(bestStart[end], end, bestId[end]))
            end = bestStart[end]
        }
        nodes.reverse()
        val fused = ArrayList<IntArray>()
        for (node in nodes) {
            val id = node[2]
            if (id == unkId && fused.isNotEmpty() && fused.last()[2] == unkId) {
                fused[fused.size - 1] = intArrayOf(fused.last()[0], node[1], unkId)
            } else {
                fused.add(node)
            }
        }
        return IntArray(fused.size) { fused[it][2] }
    }

    /** Ids with ``<s>``/``</s>``, truncated to [maxLength] tokens. */
    fun encode(text: String, maxLength: Int = MAX_LENGTH): IntArray {
        val words = normalize(text).split(Regex("\\s+")).filter { it.isNotEmpty() }
        val content = if (words.isEmpty()) IntArray(0) else {
            unigram("\u2581" + words.joinToString("\u2581"))
        }
        // Truncate the content, never the special tokens: the model must always
        // see a leading <s> and a trailing </s> (reference max_seq_length rule).
        val budget = maxOf(maxLength - 2, 0)
        val kept = minOf(content.size, budget)
        val ids = IntArray(kept + 2)
        ids[0] = BOS_ID
        for (i in 0 until kept) ids[i + 1] = content[i]
        ids[kept + 1] = EOS_ID
        return ids
    }

    /** Content pieces without special tokens (tests and diagnostics). */
    fun tokenize(text: String): List<String> {
        val words = normalize(text).split(Regex("\\s+")).filter { it.isNotEmpty() }
        if (words.isEmpty()) return emptyList()
        return unigram("\u2581" + words.joinToString("\u2581")).map { pieces[it] }
    }

    // --- UAX #29 tables (regenerate with tools/build_grapheme_table.py) ------
    private val GRAPHEME_TABLE_B64 = arrayOf(
        "eNrtV02IHFUQrnr1db/unp6Z3bjZTXY3s6suYROzm9momzExRBRWcpEkIoIKG4IRFIIIxkMuWREUkaCggVUxiOBB" +
        "PelJkIAnPUbEg7oR1IMeouSiCP5Xvde9M3j1OsxQvH5dr6q+etXwfRsF5ULkiNUyjcrqrDRJPsfjeNmleM7tAROY" +
        "w5/QS3xR6EIWxzBCOJWCyHMCp28F1+ua0JnEgwUOZMgJ2wScgR0YuMnhFvapx+4JO0UzarNlMkuawmXkQWJr4oxT" +
        "FIwdYd0jS60LEk2U6V99NLg6TJKl1gi9GEHTMJyDaBlstjH4FoZCHzlEGGOMJ1XkHlWvBlN4Zyk6hIN1keopLrNQ" +
        "oYB53Qnr/RbfijekAs+Wd6D46mwFLVq2aqNnm7KSbXM/95FynUh7lVG/GysKI/q4cDWp+Wzh7FBMlKL0OBnXGRop" +
        "jjCmQgPZ8GdLzYAltCLRq7zRgDwiYTMJTQiFTQQLhxFBM0ZjpKSl4uRqTncXdH/jG230B9okPTodmreDret3xEbq" +
        "aPgad2FJFsxTpwxnGDcQngoN06jaQsTeJIZpo6qBcFvsW4gV63c+OOVwhG4HS6HsvWl9O6jy7cxQhiI64UIjIvNx" +
        "6MWAXF1c3FSHrg/jFzzX7RbLKrFGVdgunPmED+MFBAxEiX4y21JZasuJWfw94XBeCzwxg3eR4FHGF1d1EDjHLOMo" +
        "4yUtWMLFTdTF6Ppezva17XFy1Jq802N7YTWU07mEtu6th6GC4LI5wl1J/RhsabDNeZFwc5hD/SKSMEi7qHJLQn9+" +
        "Yv7PIIbRoZImmzS0Qzu0Qzu0Qzu0Qzu0/9uW4wW6jbeUsH+XtDHSxmUVTheV3h5tyNNK/75XinZBSfazTRfYuGDe" +
        "GUV8Wx8uQfCMqKvDSmFE92xNhm9tKa8zjlcEirjg+vzQGYVFQX6fwxwCgfUY4WwPVXy460wUBPXhNV0kgA+5igNT" +
        "zY0D4cUbHOWNx9ZBcRLrENNIzmVBhmWqFAX4uNJwQZpEvwnCscg4k76E2swT7dW+uJP+7hTj+Ga4+sBjA0JsM+Ip" +
        "ZbxSiZo1FxEG7185aAI2PVefU91pi2iVsruo+CrhKapkcsxXkb2+0qB5UkGaTzyVVewLEmVJXcTxrZX6GkR2NjHS" +
        "b8o5jZlxp5UD3oRE2HChuTlXtXXZcMiAx8+72qY40hZeOw0cTLGuHrycebHeNjh771WLKXJlbgFuHBdR6xoJPSEv" +
        "2ka9oo7gFZUHH7WXgSVD1iKMqxBs4bNCoYyYj0UCHia8qTX/plNwSPCnCL71KU6neH10e/qHXvXaendGaBV0Tuia" +
        "lv7itNzzjrX7/GEd/S+ZxumBpM3XHJe0SiukusZ9yvQjc0E5HRBOuMFPFGdcwcSUqqRKqeA2Obtam25qqF7MqaW/" +
        "XxxfFncdq7ihr3MN/5X5/PB7+g833QzdnpTcIpdv6U21ChrTiWXJ35fkksued91zTzbu211KzxfJvO8cKxc55w9l" +
        "zf3l/wWrP8HG"
    )
}
