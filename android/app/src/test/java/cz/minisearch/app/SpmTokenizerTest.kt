package cz.minisearch.app

import org.json.JSONArray
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.BeforeClass
import org.junit.Test
import java.io.File

/**
 * Pins the Kotlin tokenizer to the exact behaviour of the reference
 * implementation.
 *
 * The golden vectors in ``tokenizer_golden.json`` were produced by the
 * Python tokenizer in ``app_combined._SpmTokenizer``, which is itself validated
 * byte-for-byte against HuggingFace ``tokenizers`` 0.20.3 on a Czech corpus. If
 * this test fails, the Kotlin and Python sides have diverged and the ONNX model
 * would receive different ids than the desktop reference.
 */
class SpmTokenizerTest {

    companion object {
        @BeforeClass
        @JvmStatic
        fun loadTokenizer() {
            // Prefer the built asset, fall back to the source tree so the test
            // also runs straight from a checkout.
            val candidates = listOf(
                File("src/main/assets/ml/tokenizer_spm.bin"),
                File("app/src/main/assets/ml/tokenizer_spm.bin"),
            )
            val asset = candidates.firstOrNull { it.exists() }
                ?: error("tokenizer_spm.bin not found in ${candidates.map { it.absolutePath }}")
            SpmTokenizer.loadFromBytes(asset.readBytes())
        }
    }

    private fun golden(): JSONArray {
        val text = javaClass.classLoader!!.getResourceAsStream("tokenizer_golden.json")!!
            .use { it.readBytes().toString(Charsets.UTF_8) }
        return JSONArray(text)
    }

    @Test
    fun idsMatchReferenceForEveryGoldenVector() {
        val vectors = golden()
        var checked = 0
        for (i in 0 until vectors.length()) {
            val entry = vectors.getJSONObject(i)
            val text = entry.getString("text")
            val expected = entry.getJSONArray("ids").let { arr ->
                IntArray(arr.length()) { arr.getInt(it) }
            }
            val actual = SpmTokenizer.encode(text)
            assertEquals("ids mismatch for ${text.take(30)}",
                expected.toList(), actual.toList())
            checked++
        }
        assertTrue("no vectors checked", checked >= 20)
    }

    @Test
    fun piecesMatchReference() {
        val vectors = golden()
        for (i in 0 until vectors.length()) {
            val entry = vectors.getJSONObject(i)
            val text = entry.getString("text")
            val expected = entry.getJSONArray("pieces").let { arr ->
                (0 until arr.length()).map { arr.getString(it) }
            }
            assertEquals("pieces mismatch for ${text.take(30)}", expected, SpmTokenizer.tokenize(text))
        }
    }

    @Test
    fun addsSpecialTokensAndTruncates() {
        val ids = SpmTokenizer.encode("Praha je hlavní město České republiky.")
        assertEquals(SpmTokenizer.BOS_ID, ids.first())
        assertEquals(SpmTokenizer.EOS_ID, ids.last())

        val long = SpmTokenizer.encode("Dlouhý text ".repeat(200))
        assertEquals(SpmTokenizer.MAX_LENGTH, long.size)
        assertEquals(SpmTokenizer.EOS_ID, long.last())
    }

    @Test
    fun emptyAndWhitespaceProduceBareSpecialTokens() {
        assertEquals(listOf(0, 2), SpmTokenizer.encode("").toList())
        assertEquals(listOf(0, 2), SpmTokenizer.encode("   \t\n ").toList())
    }

    @Test
    fun graphemeClustersHandleEmojiAndCombiningMarks() {
        // Family ZWJ sequence stays one cluster; a combining acute joins its base.
        assertEquals(1, SpmTokenizer.graphemeClusters("\uD83D\uDC68\u200D\uD83D\uDC69\u200D\uD83D\uDC67").size)
        assertEquals(1, SpmTokenizer.graphemeClusters("e\u0301").size)
        assertEquals(2, SpmTokenizer.graphemeClusters("\r\n\u0041").size)
    }

    @Test
    fun normalizationRewritesFullwidthAndLigatures() {
        // The charsmap maps compatibility forms onto their base characters.
        assertEquals("fi", SpmTokenizer.normalize("\uFB01"))
        assertEquals("full", SpmTokenizer.normalize("\uFF46\uFF55\uFF4C\uFF4C"))
    }
}
