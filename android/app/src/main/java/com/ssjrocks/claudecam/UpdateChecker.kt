package com.ssjrocks.claudecam

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Handler
import android.os.Looper
import androidx.core.content.FileProvider
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.io.File
import java.security.MessageDigest
import java.time.OffsetDateTime
import java.time.format.DateTimeFormatter
import java.util.Locale
import java.util.concurrent.TimeUnit

/**
 * Checks the project's GitHub releases for a newer version of the app.
 *
 * Each release carries the APK and a small `claude-cam.json` naming the app version inside it (the
 * release number and the app version don't always match). Downloads must come from the same place as
 * the release, are checked against the SHA-256 in that file, and Android itself only installs an
 * update signed by the same developer as the installed app.
 */
class UpdateChecker(private val context: Context) {

    class Update(
        val versionName: String,
        val versionCode: Int,
        val apkUrl: String,
        val sha256: String,
        val size: Long,
        val releaseUrl: String,
        val published: String,
        val author: String,
        val notes: String,
    )

    private val main = Handler(Looper.getMainLooper())
    private val client = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)
        .build()

    /** Downloads must come from the host the release lives on (github.com for GitHub's API). */
    private val trustedHost: String = Uri.parse(BuildConfig.UPDATE_API).host.let { if (it == "api.github.com") "github.com" else it.orEmpty() }

    /** Calls [done] on the main thread with an update, or (null, null) when up to date, or (null, error). */
    fun check(done: (Update?, String?) -> Unit) {
        Thread {
            val result: Pair<Update?, String?> = try {
                findUpdate() to null
            } catch (e: Exception) {
                null to (e.message ?: e.javaClass.simpleName)
            }
            main.post { done(result.first, result.second) }
        }.start()
    }

    private fun findUpdate(): Update? {
        val release = getJson(BuildConfig.UPDATE_API)
        val assets = release.optJSONArray("assets") ?: return null
        fun asset(name: String): JSONObject? =
            (0 until assets.length()).map { assets.getJSONObject(it) }.firstOrNull { it.optString("name") == name }
        val meta = asset("claude-cam.json") ?: return null // an older release with no version file
        val apk = asset("claude-cam.apk") ?: return null
        val info = getJson(trusted(meta.getString("browser_download_url")))
        val code = info.optInt("versionCode")
        if (code <= BuildConfig.VERSION_CODE) return null
        return Update(
            versionName = info.optString("versionName", "?"),
            versionCode = code,
            apkUrl = trusted(apk.getString("browser_download_url")),
            sha256 = info.optString("sha256").lowercase(Locale.ROOT),
            size = apk.optLong("size"),
            releaseUrl = release.optString("html_url"),
            published = formatDate(release.optString("published_at")),
            author = release.optJSONObject("author")?.optString("login").orEmpty().ifBlank { "the author" },
            notes = plainText(release.optString("body")),
        )
    }

    /** Downloads the APK, checking its SHA-256. [progress] gets 0-100. Callbacks run on the main thread. */
    fun download(update: Update, progress: (Int) -> Unit, done: (File?, String?) -> Unit) {
        Thread {
            val folder = File(context.cacheDir, "updates").apply { mkdirs() }
            folder.listFiles()?.forEach { it.delete() }
            val file = File(folder, "claude-cam-${update.versionName}.apk")
            val result: Pair<File?, String?> = try {
                val request = Request.Builder().url(update.apkUrl).header("User-Agent", userAgent()).build()
                client.newCall(request).execute().use { response ->
                    if (!response.isSuccessful) throw Exception("download failed (HTTP ${response.code})")
                    val body = response.body ?: throw Exception("empty download")
                    val total = body.contentLength().takeIf { it > 0 } ?: update.size
                    val digest = MessageDigest.getInstance("SHA-256")
                    var read = 0L
                    var lastPercent = -1
                    body.byteStream().use { input ->
                        file.outputStream().use { out ->
                            val buffer = ByteArray(64 * 1024)
                            while (true) {
                                val n = input.read(buffer)
                                if (n < 0) break
                                out.write(buffer, 0, n)
                                digest.update(buffer, 0, n)
                                read += n
                                val percent = if (total > 0) (read * 100 / total).toInt() else 0
                                if (percent != lastPercent) {
                                    lastPercent = percent
                                    main.post { progress(percent) }
                                }
                            }
                        }
                    }
                    val sha = digest.digest().joinToString("") { "%02x".format(it) }
                    if (update.sha256.isNotBlank() && sha != update.sha256) {
                        file.delete()
                        throw Exception("the download doesn't match the release's checksum, so it wasn't installed")
                    }
                }
                file to null
            } catch (e: Exception) {
                file.delete()
                null to (e.message ?: e.javaClass.simpleName)
            }
            main.post { done(result.first, result.second) }
        }.start()
    }

    /** An intent that hands the downloaded APK to Android's installer. */
    fun installIntent(file: File): Intent {
        val uri = FileProvider.getUriForFile(context, "${context.packageName}.files", file)
        return Intent(Intent.ACTION_VIEW).apply {
            setDataAndType(uri, "application/vnd.android.package-archive")
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
        }
    }

    private fun getJson(url: String): JSONObject {
        val request = Request.Builder()
            .url(url)
            .header("Accept", "application/vnd.github+json")
            .header("User-Agent", userAgent())
            .build()
        client.newCall(request).execute().use { response ->
            if (!response.isSuccessful) throw Exception("GitHub answered HTTP ${response.code}")
            return JSONObject(response.body?.string().orEmpty())
        }
    }

    private fun trusted(url: String): String {
        if (Uri.parse(url).host != trustedHost) throw Exception("the release points somewhere other than $trustedHost")
        return url
    }

    private fun userAgent() = "ClaudeCam/${BuildConfig.VERSION_NAME} (Android)"

    private fun formatDate(iso: String): String = try {
        OffsetDateTime.parse(iso).format(DateTimeFormatter.ofPattern("d MMM yyyy", Locale.getDefault()))
    } catch (e: Exception) {
        iso
    }

    /** The release notes without Markdown, trimmed to a readable length. */
    private fun plainText(markdown: String): String {
        val text = markdown
            .replace(Regex("<details>.*?</details>", RegexOption.DOT_MATCHES_ALL), "")
            .replace(Regex("```.*?```", RegexOption.DOT_MATCHES_ALL), "")
            .replace(Regex("\\[([^\\]]+)]\\([^)]+\\)"), "$1")
            .replace(Regex("(?m)^#+\\s*"), "")
            .replace(Regex("(?m)^>\\s*"), "")
            .replace(Regex("[*_`]"), "")
            .replace(Regex("\n{3,}"), "\n\n")
            .trim()
        return if (text.length > 700) text.take(700).substringBeforeLast(' ') + "…" else text
    }
}
