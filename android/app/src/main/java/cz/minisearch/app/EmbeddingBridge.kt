package cz.minisearch.app

import android.content.Context
import android.util.Log
import ai.onnxruntime.OnnxTensor
import ai.onnxruntime.OrtEnvironment
import ai.onnxruntime.OrtSession
import java.io.File
import java.nio.LongBuffer
import java.security.MessageDigest

/**
 * Real multilingual sentence embeddings via ONNX Runtime for Android.
 *
 * Runs ``paraphrase-multilingual-MiniLM-L12-v2`` (pinned revision, qint8 arm64
 * export) entirely on the device. The tokenizer ([SpmTokenizer]) and the pooling
 * here reproduce the reference ``sentence-transformers`` pipeline: tokenize with
 * the sentencepiece normalizer, feed ``input_ids`` / ``attention_mask`` /
 * ``token_type_ids``, mean-pool the last hidden state with the attention mask and
 * L2-normalise. That makes the vectors directly comparable with the desktop
 * model.
 *
 * The model ships inside the APK (see ``app/build.gradle``); it is copied once
 * from assets into private storage because ONNX Runtime needs a real file path.
 * Nothing is downloaded at runtime, so there is no hidden traffic.
 *
 * Python reaches the shared instance through the static [get] bridge (see
 * ``app_combined._android_onnx_model``).
 */
class EmbeddingBridge internal constructor(modelFile: File, tokenizerBytes: ByteArray) {

    private var env: OrtEnvironment? = null
    private var session: OrtSession? = null

    @Volatile
    private var ready = false

    init {
        SpmTokenizer.loadFromBytes(tokenizerBytes)
        init(modelFile)
    }

    private constructor(context: Context) : this(
        modelFile = ensureModelFile(context),
        tokenizerBytes = loadTokenizerAsset(context),
    )

    private fun init(modelFile: File) {
        val environment = OrtEnvironment.getEnvironment()
        val options = OrtSession.SessionOptions().apply {
            setIntraOpNumThreads(Runtime.getRuntime().availableProcessors().coerceIn(1, 4))
            setOptimizationLevel(OrtSession.SessionOptions.OptLevel.ALL_OPT)
        }
        val created = environment.createSession(modelFile.absolutePath, options)
        env = environment
        session = created
        ready = true
        Log.i(TAG, "ONNX model loaded (${modelFile.length()} bytes)")
    }

    fun isReady(): Boolean = ready

    /**
     * Mean-pool the last hidden state with the attention mask, then L2-normalise.
     */
    fun encode(text: String): FloatArray {
        val sess = session ?: throw IllegalStateException("session not ready")
        val envRef = env ?: throw IllegalStateException("environment not ready")
        val ids = SpmTokenizer.encode(text)
        val n = ids.size
        val shape = longArrayOf(1, n.toLong())
        val inputIds = LongArray(n) { ids[it].toLong() }
        val mask = LongArray(n) { 1L }
        val types = LongArray(n)

        val inputIdsTensor = OnnxTensor.createTensor(envRef, LongBuffer.wrap(inputIds), shape)
        val attentionTensor = OnnxTensor.createTensor(envRef, LongBuffer.wrap(mask), shape)
        val typeTensor = OnnxTensor.createTensor(envRef, LongBuffer.wrap(types), shape)
        try {
            val inputs = mapOf(
                "input_ids" to inputIdsTensor,
                "attention_mask" to attentionTensor,
                "token_type_ids" to typeTensor,
            )
            sess.run(inputs).use { result ->
                @Suppress("UNCHECKED_CAST")
                val hidden = result[0].value as Array<Array<FloatArray>>
                val seq = hidden[0].size
                val dim = hidden[0][0].size
                val pooled = FloatArray(dim)
                var count = 0
                for (t in 0 until seq) {
                    if (mask[t] == 0L) continue
                    count++
                    val row = hidden[0][t]
                    for (d in 0 until dim) pooled[d] += row[d]
                }
                if (count > 0) {
                    val divisor = count.toFloat()
                    for (d in 0 until dim) pooled[d] = pooled[d] / divisor
                }
                var sumSquares = 0.0
                for (v in pooled) sumSquares += v.toDouble() * v
                val norm = Math.sqrt(sumSquares)
                if (norm > 0) {
                    for (d in 0 until dim) pooled[d] = (pooled[d] / norm).toFloat()
                }
                return pooled
            }
        } finally {
            inputIdsTensor.close()
            attentionTensor.close()
            typeTensor.close()
        }
    }

    /** Release the session so the memory comes back on stop. */
    @Synchronized
    fun close() {
        try {
            session?.close()
        } catch (t: Throwable) {
            Log.w(TAG, "session close failed", t)
        }
        session = null
        ready = false
    }

    companion object {
        private const val TAG = "EmbeddingBridge"
        private const val ASSET_MODEL = "embedding/model_qint8_arm64.onnx"
        private const val ASSET_TOKENIZER = "ml/tokenizer_spm.bin"
        private const val FILE_NAME = "minilm-l12-v2.onnx"

        /** Embedding dimension of the pinned model. */
        const val DIMENSION = 384

        /** Vector-space tag stored with every embedding; matches the Python side. */
        const val SPACE = "minilm-l12-v2:e8f8c211226b894fcb81acc59f3b34ba3efd5f42"

        @Volatile
        private var instance: EmbeddingBridge? = null

        @Volatile
        private var initFailed = false

        /** Application context, supplied once by the service/controller. */
        @Volatile
        private var appContext: Context? = null

        fun setContext(context: Context) {
            appContext = context.applicationContext
        }

        /**
         * Return the shared bridge, or ``null`` when the model or ONNX Runtime is
         * unavailable (the Python side then falls back to the hash embedder).
         * Loading is lazy and runs on the calling thread — call it from the
         * server thread, never from the UI thread.
         */
        @Synchronized
        @JvmStatic
        fun get(): EmbeddingBridge? {
            instance?.let { return it }
            if (initFailed) return null
            val ctx = appContext ?: return null
            return try {
                EmbeddingBridge(ctx).also { instance = it }
            } catch (t: Throwable) {
                Log.w(TAG, "ONNX embedding model unavailable: ${t.message}")
                initFailed = true
                null
            }
        }

        /** Drop the cached instance so a restart rebuilds it cleanly. */
        @Synchronized
        fun reset() {
            instance?.close()
            instance = null
            initFailed = false
        }

        /** SHA-256 of the installed model file (diagnostics / integrity checks). */
        fun modelSha256(context: Context): String? {
            val file = File(context.filesDir, FILE_NAME)
            if (!file.exists()) return null
            val digest = MessageDigest.getInstance("SHA-256")
            file.inputStream().use { stream ->
                val buffer = ByteArray(1 shl 16)
                var read = stream.read(buffer)
                while (read > 0) {
                    digest.update(buffer, 0, read)
                    read = stream.read(buffer)
                }
            }
            return digest.digest().joinToString("") { "%02x".format(it) }
        }

        private fun loadTokenizerAsset(context: Context): ByteArray =
            context.assets.open(ASSET_TOKENIZER).use { it.readBytes() }

        /**
         * Copy the bundled model from assets into private storage once.
         *
         * The model is stored *compressed* in the APK (a 118 MB asset would
         * bloat the download otherwise), and ``AssetManager.openFd()`` throws
         * ``FileNotFoundException`` for a compressed asset because there is no
         * uncompressed region to mmap. The size therefore comes from
         * ``AssetFileDescriptor.getLength()`` only when the asset is stored
         * uncompressed; otherwise the stream is copied and the real byte count
         * is taken from the copy itself. A partial copy from an interrupted
         * run is detected by comparing against the expected size and replaced,
         * so a failed install self-heals instead of loading a corrupt graph.
         */
        private fun ensureModelFile(context: Context): File {
            val target = File(context.filesDir, FILE_NAME)
            val expectedSize = modelAssetSize(context)
            if (target.exists() && (expectedSize <= 0 || target.length() == expectedSize)) {
                return target
            }
            val tmp = File(context.filesDir, "$FILE_NAME.tmp")
            var copied = 0L
            context.assets.open(ASSET_MODEL).use { input ->
                tmp.outputStream().use { output ->
                    val buffer = ByteArray(1 shl 16)
                    var read = input.read(buffer)
                    while (read >= 0) {
                        output.write(buffer, 0, read)
                        copied += read
                        read = input.read(buffer)
                    }
                }
            }
            if (copied <= 0L || (expectedSize > 0 && copied != expectedSize)) {
                tmp.delete()
                throw IllegalStateException(
                    "embedding model copy truncated: $copied bytes (expected $expectedSize)")
            }
            if (!tmp.renameTo(target)) {
                target.delete()
                if (!tmp.renameTo(target)) {
                    tmp.copyTo(target, overwrite = true)
                    tmp.delete()
                }
            }
            Log.i(TAG, "embedding model installed ($copied bytes)")
            return target
        }

        /**
         * Size of the bundled model asset, or ``-1`` when it cannot be read
         * from the archive metadata (a compressed asset has no entry length).
         */
        private fun modelAssetSize(context: Context): Long =
            try {
                context.assets.openFd(ASSET_MODEL).use { it.length }
            } catch (t: Throwable) {
                -1L
            }
    }
}
