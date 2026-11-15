package cz.minisearch.app

import org.json.JSONArray
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.BeforeClass
import org.junit.Test
import java.io.File
import kotlin.math.abs

/**
 * Verifies the whole Kotlin inference path (tokenizer + ONNX Runtime + mean
 * pooling + L2 normalisation) against embeddings produced by the reference
 * ``sentence-transformers`` pipeline.
 *
 * This is the strongest check that the phone really runs the multilingual model:
 * a tokenizer drift, a wrong tensor name/shape, a missing attention mask or a
 * different pooling method would all push the cosine similarity away from 1.
 *
 * The test is skipped (not failed) when the 118 MB model asset has not been
 * fetched, so a plain ``./gradlew test`` on a machine without it still passes.
 */
class EmbeddingBridgeTest {

    companion object {
        private var modelFile: File? = null
        private var tokenizerBytes: ByteArray? = null

        @BeforeClass
        @JvmStatic
        fun locateAssets() {
            val model = listOf(
                File("src/main/assets/embedding/model_qint8_arm64.onnx"),
                File("app/src/main/assets/embedding/model_qint8_arm64.onnx"),
            ).firstOrNull { it.exists() }
            val tokenizer = listOf(
                File("src/main/assets/ml/tokenizer_spm.bin"),
                File("app/src/main/assets/ml/tokenizer_spm.bin"),
            ).firstOrNull { it.exists() }
            modelFile = model
            tokenizerBytes = tokenizer?.readBytes()
        }
    }

    @Test
    fun embeddingsMatchReferencePipeline() {
        val model = modelFile
        val tokenizer = tokenizerBytes
        assumeTrue("embedding model asset not present", model != null && tokenizer != null)

        val bridge = EmbeddingBridge(model!!, tokenizer!!)
        try {
            val golden = JSONArray(
                javaClass.classLoader!!.getResourceAsStream("embedding_golden.json")!!
                    .use { it.readBytes().toString(Charsets.UTF_8) }
            )
            var worst = 1.0
            for (i in 0 until golden.length()) {
                val entry = golden.getJSONObject(i)
                val text = entry.getString("text")
                val expected = entry.getJSONArray("embedding").let { arr ->
                    FloatArray(arr.length()) { arr.getDouble(it).toFloat() }
                }
                val actual = bridge.encode(text)
                var dot = 0.0
                for (d in actual.indices) dot += actual[d].toDouble() * expected[d]
                // Both vectors are unit length, so the dot product is the cosine.
                if (dot < worst) worst = dot
                assertTrue("vector for '$text' not unit length",
                    abs(norm(actual) - 1.0) < 1e-3)
            }
            // The quantised arm64 export is not bit-identical to the PyTorch
            // model, but it must stay extremely close; 0.99 is a strict bound.
            assertTrue("worst cosine $worst below 0.99", worst > 0.99)
        } finally {
            bridge.close()
        }
    }

    private fun norm(v: FloatArray): Double {
        var sum = 0.0
        for (x in v) sum += x.toDouble() * x
        return Math.sqrt(sum)
    }
}
