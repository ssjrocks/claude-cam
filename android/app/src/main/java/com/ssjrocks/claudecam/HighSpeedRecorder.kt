package com.ssjrocks.claudecam

import android.annotation.SuppressLint
import android.content.Context
import android.hardware.camera2.CameraCaptureSession
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraConstrainedHighSpeedCaptureSession
import android.hardware.camera2.CameraDevice
import android.hardware.camera2.CameraManager
import android.hardware.camera2.CaptureRequest
import android.media.MediaRecorder
import android.os.Build
import android.os.Handler
import android.os.HandlerThread
import android.util.Range
import android.util.Size
import java.io.File

/**
 * Records a constrained high-speed clip (120/240 fps) straight through Camera2 and MediaRecorder.
 *
 * Used when CameraX doesn't offer the phone's high-speed mode: Samsung, for one, advertises it in
 * Camera2 but without the encoder profiles CameraX looks for. There is no preview while it runs;
 * a high-speed session only allows the recorder's surface (plus an optional matching preview).
 */
class HighSpeedRecorder(
    private val context: Context,
    val cameraId: String,
    val fps: Int,
    val size: Size,
    private val rotationHint: Int,
    val file: File,
) {
    private val thread = HandlerThread("high-speed-recorder").apply { start() }
    private val handler = Handler(thread.looper)
    private var device: CameraDevice? = null
    private var session: CameraCaptureSession? = null
    private var recorder: MediaRecorder? = null
    private var started = false
    private var finished = false

    /** Opens the camera (retrying while CameraX is still letting go of it) and starts recording. */
    fun start(onStarted: () -> Unit, onError: (String) -> Unit) {
        handler.post { open(0, onStarted, onError) }
    }

    @SuppressLint("MissingPermission") // the activity only records once the camera permission is granted
    private fun open(attempt: Int, onStarted: () -> Unit, onError: (String) -> Unit) {
        val manager = context.getSystemService(CameraManager::class.java)
        try {
            manager.openCamera(cameraId, object : CameraDevice.StateCallback() {
                override fun onOpened(camera: CameraDevice) {
                    device = camera
                    configure(camera, onStarted, onError)
                }

                override fun onDisconnected(camera: CameraDevice) {
                    camera.close()
                    if (!started) onError("the camera was taken by another app")
                }

                override fun onError(camera: CameraDevice, error: Int) {
                    camera.close()
                    val busy = error == ERROR_CAMERA_IN_USE || error == ERROR_MAX_CAMERAS_IN_USE || error == ERROR_CAMERA_DEVICE
                    if (!started && busy && attempt < 8) {
                        handler.postDelayed({ open(attempt + 1, onStarted, onError) }, 250)
                    } else if (!started) {
                        onError("camera error $error")
                    }
                }
            }, handler)
        } catch (e: Exception) {
            if (attempt < 8) handler.postDelayed({ open(attempt + 1, onStarted, onError) }, 250) else onError(e.toString())
        }
    }

    @Suppress("DEPRECATION") // createConstrainedHighSpeedCaptureSession: simplest call that works on every API level we support
    private fun configure(camera: CameraDevice, onStarted: () -> Unit, onError: (String) -> Unit) {
        val rec = try {
            (if (Build.VERSION.SDK_INT >= 31) MediaRecorder(context) else MediaRecorder()).apply {
                setVideoSource(MediaRecorder.VideoSource.SURFACE)
                setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
                setOutputFile(file.absolutePath)
                setVideoEncoder(MediaRecorder.VideoEncoder.H264)
                setVideoSize(size.width, size.height)
                setVideoFrameRate(fps)
                setCaptureRate(fps.toDouble()) // real-time timestamps, not slow motion
                setVideoEncodingBitRate((size.width.toLong() * size.height * fps / 5).coerceIn(8_000_000L, 80_000_000L).toInt())
                setOrientationHint(rotationHint)
                prepare()
            }
        } catch (e: Exception) {
            onError("the video encoder refused ${size.width}x${size.height} at $fps fps ($e)")
            return
        }
        recorder = rec
        val surface = rec.surface
        camera.createConstrainedHighSpeedCaptureSession(listOf(surface), object : CameraCaptureSession.StateCallback() {
            override fun onConfigured(s: CameraCaptureSession) {
                session = s
                try {
                    val request = camera.createCaptureRequest(CameraDevice.TEMPLATE_RECORD).apply {
                        addTarget(surface)
                        set(CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE, Range(fps, fps))
                    }.build()
                    val hs = s as CameraConstrainedHighSpeedCaptureSession
                    hs.setRepeatingBurst(hs.createHighSpeedRequestList(request), null, handler)
                    rec.start()
                    started = true
                    onStarted()
                } catch (e: Exception) {
                    onError("couldn't start the high-speed capture ($e)")
                }
            }

            override fun onConfigureFailed(s: CameraCaptureSession) {
                onError("the camera refused a ${size.width}x${size.height} high-speed session at $fps fps")
            }
        }, handler)
    }

    /** Stops and finalises the file; [done] gets true if it holds a usable video. Runs [done] on the recorder thread. */
    fun stop(done: (Boolean) -> Unit) {
        handler.post {
            if (finished) return@post
            finished = true
            var ok = started
            try {
                session?.stopRepeating()
            } catch (e: Exception) {
                // already gone
            }
            try {
                if (started) recorder?.stop()
            } catch (e: RuntimeException) {
                ok = false // stop() throws when no frames were recorded
            }
            recorder?.release()
            session?.close()
            device?.close()
            thread.quitSafely()
            done(ok && file.length() > 0)
        }
    }

    companion object {
        /** Camera ids apps can open that have a high-speed mode, with the fps ranges per size. */
        fun candidates(context: Context): List<Pair<String, Map<Size, List<Int>>>> {
            val manager = context.getSystemService(CameraManager::class.java) ?: return emptyList()
            return manager.cameraIdList.mapNotNull { id ->
                val chars = manager.getCameraCharacteristics(id)
                if (chars.get(CameraCharacteristics.LENS_FACING) != CameraCharacteristics.LENS_FACING_BACK) return@mapNotNull null
                val caps = chars.get(CameraCharacteristics.REQUEST_AVAILABLE_CAPABILITIES)?.toList().orEmpty()
                if (CameraCharacteristics.REQUEST_AVAILABLE_CAPABILITIES_CONSTRAINED_HIGH_SPEED_VIDEO !in caps) return@mapNotNull null
                val map = chars.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP) ?: return@mapNotNull null
                val sizes = try {
                    map.highSpeedVideoSizes.associateWith { size ->
                        map.getHighSpeedVideoFpsRangesFor(size).filter { it.lower == it.upper }.map { it.upper }.distinct().sorted()
                    }.filterValues { it.isNotEmpty() }
                } catch (e: Exception) {
                    emptyMap()
                }
                if (sizes.isEmpty()) null else id to sizes
            }
        }
    }
}
