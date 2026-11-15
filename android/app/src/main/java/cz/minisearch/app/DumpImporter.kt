package cz.minisearch.app

import android.content.Context
import android.net.Uri
import android.os.StatFs
import android.provider.OpenableColumns
import android.util.Log
import java.io.File
import java.io.FileOutputStream

/**
 * Copies a Wikipedia dump chosen through the system file picker into the app's
 * private storage.
 *
 * A ``content://`` URI from the Storage Access Framework is **not** a filesystem
 * path: it is a grant that can be revoked when the activity goes away, and it
 * cannot be handed to Python's ``open()``. So the picker's stream is copied into
 * ``filesDir/wiki_dumps`` (which Python *can* read) before the import starts.
 *
 * The copy is bounded by a free-space check, so a huge dump fails cleanly with a
 * readable message instead of filling the device and taking the app down with it.
 */
object DumpImporter {

    private const val TAG = "DumpImporter"
    private const val HEADROOM_BYTES = 64L * 1024 * 1024

    sealed class Result {
        data class Success(val path: String, val displayName: String) : Result()
        data class Failure(val message: String) : Result()
    }

    /** Human-readable name the picker shows for [uri], best effort. */
    private fun displayName(context: Context, uri: Uri): String {
        try {
            context.contentResolver.query(uri, null, null, null, null)?.use { cursor ->
                val index = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                if (index >= 0 && cursor.moveToFirst()) {
                    val name = cursor.getString(index)
                    if (!name.isNullOrBlank()) return name
                }
            }
        } catch (t: Throwable) {
            Log.w(TAG, "could not read display name", t)
        }
        return uri.lastPathSegment?.substringAfterLast('/') ?: "dump.xml.bz2"
    }

    /** Copy [uri] into private storage; returns the readable path or an error. */
    fun copyToPrivate(context: Context, uri: Uri): Result {
        val name = displayName(context, uri).replace(Regex("[^A-Za-z0-9._-]"), "_")
        val dir = File(context.filesDir, "wiki_dumps")
        if (!dir.exists() && !dir.mkdirs()) {
            return Result.Failure("Nepodařilo se vytvořit složku pro dump.")
        }
        val target = File(dir, name)

        val size = try {
            context.contentResolver.openAssetFileDescriptor(uri, "r")?.use { it.length }
        } catch (t: Throwable) {
            -1L
        } ?: -1L

        if (size > 0) {
            val stat = StatFs(dir.absolutePath)
            val free = stat.availableBytes
            if (free < size + HEADROOM_BYTES) {
                return Result.Failure(
                    "Málo místa: dump má ${size / (1024 * 1024)} MB, " +
                        "volno ${free / (1024 * 1024)} MB. Uvolněte prosím místo.")
            }
        }

        return try {
            context.contentResolver.openInputStream(uri).use { input ->
                if (input == null) return Result.Failure("Vybraný soubor nelze otevřít.")
                FileOutputStream(target).use { output -> input.copyTo(output) }
            }
            Log.i(TAG, "dump copied to ${target.absolutePath} (${target.length()} bytes)")
            Result.Success(target.absolutePath, name)
        } catch (t: Throwable) {
            Log.e(TAG, "copy failed", t)
            target.delete()
            Result.Failure("Kopírování dumpu selhalo: ${t.message ?: t.javaClass.simpleName}")
        }
    }
}
