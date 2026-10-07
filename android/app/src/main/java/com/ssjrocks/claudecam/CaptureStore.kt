package com.ssjrocks.claudecam

import android.content.Context
import java.io.File

/**
 * Photos and videos the user takes with the app's own buttons. They're kept in the app's private
 * storage, never the phone's gallery, until Claude fetches them; then they're deleted.
 *
 * A capture is just a file named "<kind>-<taken at, ms>.<ext>". Files still being written start
 * with "tmp-" and aren't listed.
 */
class CaptureStore(context: Context) {

    val dir: File = File(context.filesDir, "captures").apply { mkdirs() }

    class Capture(val id: String, val kind: String, val file: File, val takenAt: Long)

    /** A new (id, final file, temporary file to write first). */
    fun newCapture(kind: String): Triple<String, File, File> {
        val id = "$kind-${System.currentTimeMillis()}"
        val name = id + if (kind == "photo") ".jpg" else ".mp4"
        return Triple(id, File(dir, name), File(dir, "tmp-$name"))
    }

    fun list(): List<Capture> = dir.listFiles().orEmpty()
        .filter { it.isFile && it.length() > 0 && !it.name.startsWith("tmp-") && (it.extension == "jpg" || it.extension == "mp4") }
        .map { f ->
            val id = f.nameWithoutExtension
            Capture(id, id.substringBefore('-'), f, id.substringAfter('-').toLongOrNull() ?: f.lastModified())
        }
        .sortedByDescending { it.takenAt }

    fun find(id: String): Capture? = list().firstOrNull { it.id == id }

    fun delete(id: String) {
        find(id)?.file?.delete()
    }

    /** Leftovers from a recording that never finished, e.g. if the app was killed. */
    fun cleanTemporary() {
        dir.listFiles().orEmpty().filter { it.name.startsWith("tmp-") && System.currentTimeMillis() - it.lastModified() > 60_000 }
            .forEach { it.delete() }
    }

    /** e.g. "2 photos and 1 video", or "" when there's nothing. */
    fun describeWaiting(): String {
        val all = list()
        val photos = all.count { it.kind == "photo" }
        val videos = all.size - photos
        val parts = listOfNotNull(
            if (photos > 0) "$photos photo${if (photos > 1) "s" else ""}" else null,
            if (videos > 0) "$videos video${if (videos > 1) "s" else ""}" else null,
        )
        return parts.joinToString(" and ")
    }
}
