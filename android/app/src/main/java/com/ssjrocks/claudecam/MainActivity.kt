@file:OptIn(ExperimentalSessionConfig::class, ExperimentalHighSpeedVideo::class, ExperimentalCamera2Interop::class)

package com.ssjrocks.claudecam

import android.Manifest
import android.annotation.SuppressLint
import android.app.AlertDialog
import android.content.Intent
import android.content.pm.PackageManager
import android.content.res.ColorStateList
import android.graphics.Bitmap
import android.graphics.Matrix
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraManager
import android.hardware.camera2.CameraMetadata
import android.net.Uri
import android.os.BatteryManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.os.SystemClock
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import android.provider.OpenableColumns
import android.provider.Settings
import android.text.InputType
import android.util.Range
import android.util.Size
import android.view.GestureDetector
import android.view.MotionEvent
import android.view.OrientationEventListener
import android.view.ScaleGestureDetector
import android.view.Surface
import android.view.View
import android.view.WindowManager
import android.widget.EditText
import android.widget.ImageButton
import android.widget.LinearLayout
import android.widget.TextView
import androidx.activity.ComponentActivity
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.camera.camera2.interop.Camera2CameraInfo
import androidx.camera.camera2.interop.ExperimentalCamera2Interop
import androidx.camera.core.Camera
import androidx.camera.core.CameraInfo
import androidx.camera.core.CameraSelector
import androidx.camera.core.CameraState
import androidx.camera.core.DynamicRange
import androidx.camera.core.ExperimentalSessionConfig
import androidx.camera.core.FocusMeteringAction
import androidx.camera.core.FocusMeteringResult
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageCapture
import androidx.camera.core.ImageCaptureException
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.core.SessionConfig
import androidx.camera.core.SurfaceOrientedMeteringPointFactory
import androidx.camera.core.TorchState
import androidx.camera.core.resolutionselector.AspectRatioStrategy
import androidx.camera.core.resolutionselector.ResolutionSelector
import androidx.camera.core.resolutionselector.ResolutionStrategy
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.video.ExperimentalHighSpeedVideo
import androidx.camera.video.FallbackStrategy
import androidx.camera.video.FileOutputOptions
import androidx.camera.video.HighSpeedVideoSessionConfig
import androidx.camera.video.Quality
import androidx.camera.video.QualitySelector
import androidx.camera.video.Recorder
import androidx.camera.video.Recording
import androidx.camera.video.VideoCapture
import androidx.camera.video.VideoRecordEvent
import androidx.camera.view.PreviewView
import androidx.core.content.ContextCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.updatePadding
import com.google.common.util.concurrent.ListenableFuture
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.File
import java.util.concurrent.Executors
import kotlin.math.abs
import kotlin.math.roundToInt

private const val USER_RECORDING = "user" // recordingToken while the user records their own video
private const val RELEASES_URL = "https://github.com/ssjrocks/claude-cam/releases"

class MainActivity : ComponentActivity(), CamLink.Listener {

    private lateinit var link: CamLink
    private lateinit var previewView: PreviewView
    private lateinit var statusDot: View
    private lateinit var statusText: TextView
    private lateinit var statsText: TextView
    private lateinit var lookBadge: TextView
    private lateinit var recBadge: TextView
    private lateinit var shutterButton: View
    private lateinit var videoButton: ImageButton
    private lateinit var capturesText: TextView
    private lateinit var updateBanner: TextView
    private lateinit var torchButton: ImageButton
    private lateinit var zoomButton: TextView
    private lateinit var afButton: TextView
    private lateinit var messageCard: View
    private lateinit var messageText: TextView
    private lateinit var doneButton: TextView
    private lateinit var focusRing: View
    private lateinit var permissionView: View
    private lateinit var orientationListener: OrientationEventListener

    private val main = Handler(Looper.getMainLooper())
    private val analysisExecutor = Executors.newSingleThreadExecutor()
    private val photoExecutor = Executors.newSingleThreadExecutor()

    private var cameraProvider: ProcessCameraProvider? = null
    private var camera: Camera? = null
    private var imageCapture: ImageCapture? = null
    private var imageAnalysis: ImageAnalysis? = null
    private var targetRotation = Surface.ROTATION_0

    // Stream settings from the server
    @Volatile private var streamIntervalMs = 333L
    @Volatile private var jpegQuality = 70
    private var streamSize = 1920

    // Camera settings that survive the camera closing and reopening
    private var torchOn = false
    private var zoomRatio = 1f
    private var exposureIndex = 0
    private var focusLocked: String? = null // null = continuous autofocus
    private var settingsApplied = false

    // Stream stats; written on the analysis thread
    @Volatile private var lastSentAt = 0L
    @Volatile private var fpsActual = 0f
    @Volatile private var lastFrameKb = 0
    @Volatile private var streamW = 0
    @Volatile private var streamH = 0
    private val sentTimes = ArrayDeque<Long>()
    private val jpegBuffer = ByteArrayOutputStream(512 * 1024)

    private var currentMessageId: String? = null

    // Video recording (see the "video" section)
    private var activeRecording: Recording? = null
    private var recordingToken: String? = null
    private var recordingInfo: JSONObject? = null
    private var recordingStartedAt = 0L
    private var videoCaps: JSONObject? = null
    private var backCameras: List<BackCamera> = emptyList()
    private var pendingImport: Uri? = null // a video shared to Claude Cam, waiting to be sent
    private var highSpeedRecorder: HighSpeedRecorder? = null // a Camera2 high-speed clip in progress
    private var camera2Fast: List<Pair<String, Map<Size, List<Int>>>> = emptyList()
    private val highSpeedLimit = Runnable { finishHighSpeed() }

    // Photos and videos the user takes with the app's own buttons, held until Claude fetches them
    private lateinit var captures: CaptureStore
    private var userVideo: Recording? = null

    // App updates from GitHub Releases
    private lateinit var updates: UpdateChecker
    private var availableUpdate: UpdateChecker.Update? = null
    private var pendingInstall: File? = null
    private var tick = 0

    private val hideBadge = Runnable {
        lookBadge.animate().alpha(0f).setDuration(400).withEndAction { lookBadge.visibility = View.GONE }
    }

    private val ticker = object : Runnable {
        override fun run() {
            updateStats()
            if (highSpeedRecorder != null) updateRecBadge()
            if (link.isConnected && tick++ % 3 == 0) sendStatus()
            main.postDelayed(this, 1000)
        }
    }

    private val requestCamera = registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        if (granted) startCamera() else permissionView.visibility = View.VISIBLE
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        enableEdgeToEdge()
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        previewView = findViewById(R.id.preview)
        statusDot = findViewById(R.id.status_dot)
        statusText = findViewById(R.id.status_text)
        statsText = findViewById(R.id.stats_text)
        lookBadge = findViewById(R.id.look_badge)
        recBadge = findViewById(R.id.rec_badge)
        recBadge.setOnClickListener { if (userVideo != null) userVideo?.stop() else stopRecordingFromPhone() }
        shutterButton = findViewById(R.id.shutter_button)
        videoButton = findViewById(R.id.video_button)
        capturesText = findViewById(R.id.captures_text)
        updateBanner = findViewById(R.id.update_banner)
        captures = CaptureStore(this)
        captures.cleanTemporary()
        updates = UpdateChecker(this)
        shutterButton.setOnClickListener { takeUserPhoto() }
        videoButton.setOnClickListener { toggleUserVideo() }
        updateBanner.setOnClickListener { availableUpdate?.let { showUpdateDialog(it) } }
        refreshCapturesUi()
        torchButton = findViewById(R.id.torch_button)
        zoomButton = findViewById(R.id.zoom_button)
        afButton = findViewById(R.id.af_button)
        messageCard = findViewById(R.id.message_card)
        messageText = findViewById(R.id.message_text)
        doneButton = findViewById(R.id.done_button)
        focusRing = findViewById(R.id.focus_ring)
        permissionView = findViewById(R.id.permission_view)

        // Show the whole frame: what the user sees is exactly what Claude gets.
        previewView.scaleType = PreviewView.ScaleType.FIT_CENTER

        val topBar = findViewById<View>(R.id.top_bar)
        val bottomArea = findViewById<View>(R.id.bottom_area)
        ViewCompat.setOnApplyWindowInsetsListener(findViewById(R.id.root)) { _, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout())
            topBar.updatePadding(top = bars.top)
            bottomArea.updatePadding(bottom = bars.bottom + dp(12))
            insets
        }

        link = CamLink(this, this)
        onLinkState(CamLink.State.SEARCHING, "")

        setupGestures()
        torchButton.setOnClickListener { setTorch(!torchOn) }
        zoomButton.setOnClickListener { cycleZoom() }
        afButton.setOnClickListener { resetFocus() }
        findViewById<View>(R.id.settings_button).setOnClickListener { showServerDialog() }
        findViewById<View>(R.id.status_row).setOnClickListener { link.reconnectNow() }
        doneButton.setOnClickListener { acknowledgeMessage() }
        findViewById<View>(R.id.permission_button).setOnClickListener { askForCamera() }

        orientationListener = object : OrientationEventListener(this) {
            override fun onOrientationChanged(orientation: Int) {
                if (orientation == ORIENTATION_UNKNOWN) return
                val rotation = when (orientation) {
                    in 50 until 130 -> Surface.ROTATION_270
                    in 140 until 220 -> Surface.ROTATION_180
                    in 230 until 310 -> Surface.ROTATION_90
                    in 0 until 40, in 320 until 360 -> Surface.ROTATION_0
                    else -> return // near a boundary: keep the current rotation
                }
                if (rotation != targetRotation) {
                    targetRotation = rotation
                    imageAnalysis?.targetRotation = rotation
                    imageCapture?.targetRotation = rotation
                }
            }
        }

        if (hasCameraPermission()) startCamera() else requestCamera.launch(Manifest.permission.CAMERA)
        handleShare(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleShare(intent)
    }

    override fun onStart() {
        super.onStart()
        // Back from the settings page with the permission granted
        if (permissionView.visibility == View.VISIBLE && hasCameraPermission()) startCamera()
        link.start()
        maybeCheckForUpdates()
        orientationListener.enable()
        main.post(ticker)
    }

    override fun onResume() {
        super.onResume()
        // Back from "Install unknown apps" in Settings with permission now granted
        val file = pendingInstall
        if (file != null && (Build.VERSION.SDK_INT < 26 || packageManager.canRequestPackageInstalls())) {
            pendingInstall = null
            startActivity(updates.installIntent(file))
        }
    }

    override fun onStop() {
        super.onStop()
        finishHighSpeed() // Android takes the camera away from background apps anyway
        userVideo?.stop()
        link.stop()
        orientationListener.disable()
        main.removeCallbacks(ticker)
    }

    override fun onDestroy() {
        super.onDestroy()
        analysisExecutor.shutdown()
        photoExecutor.shutdown()
    }

    // --- camera -------------------------------------------------------------------------------

    private fun hasCameraPermission() =
        ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED

    private fun askForCamera() {
        if (shouldShowRequestPermissionRationale(Manifest.permission.CAMERA)) {
            requestCamera.launch(Manifest.permission.CAMERA)
        } else {
            // Denied for good: Android won't ask again, so open the app's settings page.
            startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.fromParts("package", packageName, null)))
        }
    }

    private fun startCamera() {
        permissionView.visibility = View.GONE
        val future = ProcessCameraProvider.getInstance(this)
        future.addListener({
            cameraProvider = future.get()
            bindUseCases()
        }, ContextCompat.getMainExecutor(this))
    }

    private val ratio = AspectRatioStrategy.RATIO_4_3_FALLBACK_AUTO_STRATEGY

    private fun buildPreview(): Preview =
        Preview.Builder()
            .setResolutionSelector(ResolutionSelector.Builder().setAspectRatioStrategy(ratio).build())
            .build()
            .also { it.setSurfaceProvider(previewView.surfaceProvider) }

    private fun buildAnalysis(): ImageAnalysis =
        ImageAnalysis.Builder()
            .setResolutionSelector(
                ResolutionSelector.Builder()
                    .setAspectRatioStrategy(ratio)
                    .setResolutionStrategy(
                        ResolutionStrategy(
                            Size(streamSize, streamSize * 3 / 4),
                            ResolutionStrategy.FALLBACK_RULE_CLOSEST_LOWER_THEN_HIGHER,
                        ),
                    )
                    .build(),
            )
            .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
            .setTargetRotation(targetRotation)
            .build()
            .also { it.setAnalyzer(analysisExecutor, ::analyze) }

    private fun buildCapture(): ImageCapture =
        ImageCapture.Builder()
            .setCaptureMode(ImageCapture.CAPTURE_MODE_MINIMIZE_LATENCY)
            .setJpegQuality(92)
            .setResolutionSelector(
                ResolutionSelector.Builder()
                    .setAspectRatioStrategy(ratio)
                    .setResolutionStrategy(ResolutionStrategy.HIGHEST_AVAILABLE_STRATEGY)
                    .build(),
            )
            .setTargetRotation(targetRotation)
            .build()

    /** The normal setup: preview, the live stream and full-resolution photos. */
    private fun bindUseCases() {
        if (recordingToken != null) return // never pull the camera out from under a recording
        val provider = cameraProvider ?: return
        val preview = buildPreview()
        val analysis = buildAnalysis()
        val capture = buildCapture()
        rebind(analysis, capture) { provider.bindToLifecycle(this, CameraSelector.DEFAULT_BACK_CAMERA, preview, analysis, capture) }
    }

    /** Unbinds everything, runs [bind], and wires the new camera up to the UI. False if binding failed. */
    private fun rebind(analysis: ImageAnalysis?, capture: ImageCapture?, bind: () -> Camera): Boolean {
        val provider = cameraProvider ?: return false
        camera?.cameraInfo?.let { info ->
            info.cameraState.removeObservers(this)
            info.zoomState.removeObservers(this)
            info.torchState.removeObservers(this)
        }
        provider.unbindAll()
        val cam = try {
            bind()
        } catch (e: Exception) {
            statsText.text = "Camera error: ${e.message}"
            return false
        }
        camera = cam
        imageAnalysis = analysis
        imageCapture = capture
        settingsApplied = false
        if (videoCaps == null) videoCaps = try { computeVideoCaps() } catch (e: Exception) { JSONObject().put("camera_probe_error", e.toString()) }

        cam.cameraInfo.cameraState.observe(this) { state ->
            if (state.type == CameraState.Type.OPEN && !settingsApplied) {
                settingsApplied = true
                applyCameraSettings()
            } else if (state.type == CameraState.Type.CLOSED) {
                settingsApplied = false
                focusLocked = null
            }
            state.error?.let { statsText.text = "Camera error ${it.code}" }
        }
        cam.cameraInfo.zoomState.observe(this) { zoomButton.text = "%.1f×".format(it.zoomRatio) }
        cam.cameraInfo.torchState.observe(this) {
            torchButton.setImageResource(if (it == TorchState.ON) R.drawable.ic_flash_on else R.drawable.ic_flash_off)
        }
        updateAfButton()
        return true
    }

    private fun applyCameraSettings() {
        val cam = camera ?: return
        cam.cameraControl.setZoomRatio(zoomRatio)
        if (cam.cameraInfo.exposureState.isExposureCompensationSupported) {
            cam.cameraControl.setExposureCompensationIndex(exposureIndex)
        }
        if (cam.cameraInfo.hasFlashUnit()) cam.cameraControl.enableTorch(torchOn)
        updateAfButton()
    }

    /** Runs for every camera frame; encodes and sends one at the rate the server asked for. */
    private fun analyze(image: ImageProxy) {
        image.use {
            val now = SystemClock.elapsedRealtime()
            if (!link.isConnected || now - lastSentAt < streamIntervalMs - 15 || link.queuedBytes() > 768 * 1024) return
            lastSentAt = now
            var bitmap = image.toBitmap()
            val rotation = image.imageInfo.rotationDegrees
            if (rotation != 0) {
                val m = Matrix().apply { postRotate(rotation.toFloat()) }
                val rotated = Bitmap.createBitmap(bitmap, 0, 0, bitmap.width, bitmap.height, m, false)
                bitmap.recycle()
                bitmap = rotated
            }
            jpegBuffer.reset()
            bitmap.compress(Bitmap.CompressFormat.JPEG, jpegQuality, jpegBuffer)
            streamW = bitmap.width
            streamH = bitmap.height
            bitmap.recycle()
            val bytes = jpegBuffer.toByteArray()
            // Capture time: sensor timestamps share the elapsedRealtime clock on most phones.
            val sensorAge = (SystemClock.elapsedRealtimeNanos() - image.imageInfo.timestamp) / 1_000_000
            val age = if (sensorAge in 0..1000) sensorAge else SystemClock.elapsedRealtime() - now
            link.sendFrame("stream", null, bytes, age)

            lastFrameKb = bytes.size / 1024
            sentTimes.addLast(now)
            while (sentTimes.isNotEmpty() && now - sentTimes.first() > 2000) sentTimes.removeFirst()
            fpsActual = sentTimes.size / 2f
        }
    }

    private fun takePhoto(req: String) {
        val capture = imageCapture
        if (capture == null || camera == null) {
            link.sendError(
                req,
                if (recordingToken != null) "photos are unavailable while a video is recording; use the live frames or stop the recording"
                else "the camera is not running on the phone",
            )
            return
        }
        flashPreview()
        val requestedAt = SystemClock.elapsedRealtime()
        val out = ByteArrayOutputStream(4 * 1024 * 1024)
        val options = ImageCapture.OutputFileOptions.Builder(out).build()
        capture.takePicture(options, photoExecutor, object : ImageCapture.OnImageSavedCallback {
            override fun onImageSaved(output: ImageCapture.OutputFileResults) {
                // The exposure happened somewhere between the request and now.
                val age = (SystemClock.elapsedRealtime() - requestedAt) / 2
                link.sendFrame("photo", req, out.toByteArray(), age)
            }

            override fun onError(exception: ImageCaptureException) {
                link.sendError(req, exception.message ?: "capture failed")
            }
        })
    }

    private fun setTorch(on: Boolean) {
        val cam = camera ?: return
        if (!cam.cameraInfo.hasFlashUnit()) return
        torchOn = on
        cam.cameraControl.enableTorch(on)
        main.postDelayed({ sendStatus() }, 300)
    }

    private fun cycleZoom() {
        val cam = camera ?: return
        val zs = cam.cameraInfo.zoomState.value ?: return
        val presets = listOf(zs.minZoomRatio, 1f, 2f, 5f).filter { it in zs.minZoomRatio..zs.maxZoomRatio }.distinct()
        val next = presets.firstOrNull { it > zs.zoomRatio + 0.05f } ?: presets.first()
        zoomRatio = next
        cam.cameraControl.setZoomRatio(next)
        main.postDelayed({ sendStatus() }, 300)
    }

    private fun focusAt(x: Float, y: Float) {
        val cam = camera ?: return
        val point = previewView.meteringPointFactory.createPoint(x, y)
        val action = FocusMeteringAction.Builder(point, FocusMeteringAction.FLAG_AF or FocusMeteringAction.FLAG_AE)
            .disableAutoCancel()
            .build()
        cam.cameraControl.startFocusAndMetering(action)
        focusLocked = "locked (tapped on the phone)"
        showFocusRing(x, y)
        updateAfButton()
        sendStatus()
    }

    private fun resetFocus() {
        camera?.cameraControl?.cancelFocusAndMetering()
        focusLocked = null
        updateAfButton()
        sendStatus()
    }

    /** Maps a point in the upright image Claude sees to sensor coordinates (both 0-1). */
    private fun uprightToSensor(u: Float, v: Float): Pair<Float, Float> {
        val rot = camera?.cameraInfo?.getSensorRotationDegrees(targetRotation) ?: 0
        return when (rot) {
            90 -> v to 1 - u
            180 -> 1 - u to 1 - v
            270 -> 1 - v to u
            else -> u to v
        }
    }

    // --- video ----------------------------------------------------------------------------------

    private val qualityOrder = listOf(Quality.SD, Quality.HD, Quality.FHD, Quality.UHD)

    private fun qualityName(q: Quality) = when (q) {
        Quality.SD -> "480p"
        Quality.HD -> "720p"
        Quality.FHD -> "1080p"
        Quality.UHD -> "2160p"
        else -> null
    }

    private fun qualityFrom(name: String) = when (name) {
        "480p" -> Quality.SD
        "720p" -> Quality.HD
        "2160p" -> Quality.UHD
        else -> Quality.FHD
    }

    /** The requested quality if supported, else the best one below it, else the lowest available. */
    private fun pickQuality(supported: List<Quality>, wanted: Quality): Quality {
        if (wanted in supported) return wanted
        val limit = qualityOrder.indexOf(wanted)
        return supported.filter { qualityOrder.indexOf(it) in 0..limit }.maxByOrNull { qualityOrder.indexOf(it) }
            ?: supported.minBy { qualityOrder.indexOf(it).let { i -> if (i < 0) 99 else i } }
    }

    /** A fixed frame rate as close to [fps] as possible (rounding down on a tie), else any range. */
    private fun bestRange(ranges: Set<Range<Int>>, fps: Int): Range<Int>? =
        ranges.filter { it.lower == it.upper }.minByOrNull { abs(it.upper - fps) * 2 + if (it.upper > fps) 1 else 0 }
            ?: ranges.minByOrNull { abs(it.upper - fps) }

    /** A back camera this app can open, and what it can record. */
    private class BackCamera(
        val id: String,
        val info: CameraInfo,
        val selector: CameraSelector,
        val zoom: Float,
        val fps: List<Int>,
        val fast: List<Quality>,
        val fastFps: List<Int>,
    ) {
        val label: String
            get() = when {
                zoom < 0.9f -> "ultra-wide camera"
                zoom > 1.5f -> "telephoto camera"
                else -> "main camera"
            } + " (id $id)"
    }

    /** Every back camera apps can open, and (for diagnostics) what each physical sensor advertises. */
    private fun computeVideoCaps(): JSONObject {
        val provider = cameraProvider ?: return JSONObject()
        val cams = provider.availableCameraInfos.filter { it.lensFacing == CameraSelector.LENS_FACING_BACK }.map { info ->
            val id = Camera2CameraInfo.from(info).cameraId
            val fast = try {
                Recorder.getHighSpeedVideoCapabilities(info)?.getSupportedQualities(DynamicRange.SDR).orEmpty()
            } catch (e: Exception) {
                emptyList()
            }
            val fastFps = if (fast.isEmpty()) emptyList() else try {
                val probe = HighSpeedVideoSessionConfig(
                    VideoCapture.withOutput(Recorder.Builder().setQualitySelector(QualitySelector.from(fast.first())).build()),
                )
                info.getSupportedFrameRateRanges(probe).map { it.upper }.filter { it > 60 }.distinct().sorted()
            } catch (e: Exception) {
                emptyList()
            }
            BackCamera(
                id,
                info,
                CameraSelector.Builder().addCameraFilter { list -> list.filter { Camera2CameraInfo.from(it).cameraId == id } }.build(),
                info.intrinsicZoomRatio,
                info.supportedFrameRateRanges.map { it.upper }.filter { it >= 24 }.distinct().sorted(),
                fast,
                fastFps,
            )
        }
        backCameras = cams
        val o = JSONObject()
        val main = mainCamera()
        if (main != null) {
            val qualities = Recorder.getVideoCapabilities(main.info).getSupportedQualities(DynamicRange.SDR)
            o.put("video_qualities", JSONArray(qualities.sortedBy { qualityOrder.indexOf(it) }.mapNotNull(::qualityName)))
        }
        o.put("video_fps", JSONArray(cams.flatMap { it.fps }.distinct().sorted()))
        val fastest = cams.flatMap { it.fastFps }.distinct().sorted()
        camera2Fast = try {
            HighSpeedRecorder.candidates(this)
        } catch (e: Exception) {
            emptyList()
        }
        if (fastest.isNotEmpty()) {
            o.put("high_speed_fps", JSONArray(fastest))
            o.put("high_speed_qualities", JSONArray(cams.flatMap { it.fast }.distinct().sortedBy { qualityOrder.indexOf(it) }.mapNotNull(::qualityName)))
        } else if (camera2Fast.isNotEmpty()) {
            val sizes = camera2Fast.flatMap { it.second.entries }
            o.put("high_speed_fps", JSONArray(sizes.flatMap { it.value }.distinct().sorted()))
            o.put("high_speed_qualities", JSONArray(sizes.map { it.key }.sortedBy { it.width * it.height }.map { "${it.width}x${it.height}" }.distinct()))
            o.put("high_speed_via", "camera2")
        }
        o.put("cameras", JSONArray(cams.map { c ->
            JSONObject().put("id", c.id).put("label", c.label).put("zoom", c.zoom.toDouble())
                .put("fps", JSONArray(c.fps)).put("high_speed_fps", JSONArray(c.fastFps))
        }))
        o.put("camera2", camera2Report())
        return o
    }

    /** Raw Camera2 facts for every camera id and physical sensor, so we can see what the phone hides. */
    private fun camera2Report(): JSONArray {
        val out = JSONArray()
        val manager = getSystemService(CameraManager::class.java) ?: return out
        fun describe(id: String, chars: CameraCharacteristics, physicalOf: String?): JSONObject {
            val caps = chars.get(CameraCharacteristics.REQUEST_AVAILABLE_CAPABILITIES)?.toList().orEmpty()
            val map = chars.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP)
            val highSpeed = try {
                map?.highSpeedVideoFpsRanges?.map { it.upper }?.distinct()?.sorted().orEmpty()
            } catch (e: Exception) {
                emptyList()
            }
            return JSONObject().put("id", id).put("physical_of", physicalOf ?: JSONObject.NULL)
                .put("facing", when (chars.get(CameraCharacteristics.LENS_FACING)) {
                    CameraMetadata.LENS_FACING_BACK -> "back"
                    CameraMetadata.LENS_FACING_FRONT -> "front"
                    else -> "external"
                })
                .put("ae_fps", JSONArray(chars.get(CameraCharacteristics.CONTROL_AE_AVAILABLE_TARGET_FPS_RANGES)?.map { "${it.lower}-${it.upper}" }.orEmpty()))
                .put("high_speed_capability", CameraMetadata.REQUEST_AVAILABLE_CAPABILITIES_CONSTRAINED_HIGH_SPEED_VIDEO in caps)
                .put("high_speed_fps", JSONArray(highSpeed))
                .put("level", chars.get(CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL) ?: -1)
        }
        try {
            for (id in manager.cameraIdList) {
                val chars = manager.getCameraCharacteristics(id)
                out.put(describe(id, chars, null))
                for (phys in chars.physicalCameraIds) {
                    out.put(describe(phys, manager.getCameraCharacteristics(phys), id))
                }
            }
        } catch (e: Exception) {
            out.put(JSONObject().put("error", e.toString()))
        }
        return out
    }

    private fun mainCamera(): BackCamera? = backCameras.firstOrNull { it.zoom in 0.9f..1.5f } ?: backCameras.firstOrNull()

    /** The back camera to record [fps] with: the main one unless another can get closer to the rate. */
    private fun cameraFor(fps: Int): BackCamera? {
        val main = mainCamera() ?: return null
        if (fps > 60) {
            backCameras.filter { it.fastFps.isNotEmpty() }
                .minByOrNull { cam -> cam.fastFps.minOf { abs(it - fps) } * 10 + if (cam === main) 0 else 1 }
                ?.let { return it }
        }
        fun reach(cam: BackCamera) = (cam.fps.maxOrNull() ?: 0).coerceAtMost(fps)
        val best = backCameras.maxByOrNull { reach(it) * 10 + if (it === main) 1 else 0 } ?: main
        return if (reach(best) > reach(main)) best else main
    }

    /**
     * Rebinds the camera for recording and starts it. Above 60 fps it uses the phone's high-speed
     * session (preview + video only); otherwise it keeps the live stream if the phone can run it at
     * that frame rate alongside the video, and drops it if not. Photos are off while recording.
     */
    private fun startRecording(cmd: JSONObject) {
        val req = cmd.optString("req")
        val token = cmd.optString("token")
        val fps = cmd.optInt("fps", 30)
        val wanted = qualityFrom(cmd.optString("quality", "1080p"))
        val maxSeconds = cmd.optDouble("max_seconds", 600.0)
        val provider = cameraProvider
        val chosenCamera = cameraFor(fps)
        if (provider == null || camera == null || chosenCamera == null) {
            link.sendError(req, "the camera is not running on the phone")
            return
        }
        if (recordingToken != null) {
            link.sendError(req, if (userVideo != null) "the user is recording a video on the phone right now" else "already recording")
            return
        }
        val info = chosenCamera.info
        val selector = chosenCamera.selector
        val notes = JSONArray()
        if (chosenCamera !== mainCamera()) notes.put("recorded with the ${chosenCamera.label}, the only one that can do this frame rate (a different view)")
        var recorder: Recorder? = null
        var video: VideoCapture<Recorder>? = null
        var rate: Range<Int>? = null
        var highSpeed = false
        var liveStream = true
        recordingToken = token // blocks bindUseCases() from undoing this

        if (fps > 60) {
            val fast = Recorder.getHighSpeedVideoCapabilities(info)?.getSupportedQualities(DynamicRange.SDR).orEmpty()
            if (fast.isEmpty()) {
                val pick = pickCamera2HighSpeed(fps, wanted)
                if (pick != null) {
                    startCamera2HighSpeed(req, token, pick.first, pick.second, pick.third, maxSeconds, notes)
                    return
                }
                notes.put("this phone has no high-speed video mode, so it recorded at its fastest normal frame rate")
            } else {
                val r = Recorder.Builder().setQualitySelector(QualitySelector.from(pickQuality(fast, wanted))).build()
                val v = VideoCapture.Builder(r).setTargetRotation(targetRotation).build()
                val preview = buildPreview()
                val chosen = try {
                    bestRange(info.getSupportedFrameRateRanges(HighSpeedVideoSessionConfig(v, preview)), fps)
                } catch (e: Exception) {
                    null
                }
                if (chosen != null && rebind(null, null) {
                        provider.bindToLifecycle(this, selector, HighSpeedVideoSessionConfig(v, preview, chosen, false))
                    }
                ) {
                    recorder = r
                    video = v
                    rate = chosen
                    highSpeed = true
                    liveStream = false
                } else {
                    notes.put("the high-speed mode couldn't start, so it recorded at a normal frame rate")
                }
            }
        }

        if (recorder == null) {
            val session = bindVideoSession(info, selector, wanted, fps)
            if (session != null) {
                recorder = session.recorder
                video = session.video
                rate = session.rate
                liveStream = session.liveStream
            }
        }

        if (recorder == null || video == null) {
            recordingToken = null
            bindUseCases()
            link.sendError(req, "couldn't set up video recording on this phone")
            return
        }
        if (rate != null && rate.upper < fps && notes.length() == 0) notes.put("${rate.upper} fps is the fastest this phone records at this setting")
        if (!liveStream) notes.put("the live stream pauses during this recording; it comes back when the recording stops")

        val file = File(cacheDir, "rec-$token.mp4")
        val output = FileOutputOptions.Builder(file).setDurationLimitMillis((maxSeconds * 1000).toLong() + 5_000).build()
        val v = video
        var replied = false
        activeRecording = recorder.prepareRecording(this, output).start(ContextCompat.getMainExecutor(this)) { event ->
            when (event) {
                is VideoRecordEvent.Start -> {
                    recordingStartedAt = SystemClock.elapsedRealtime()
                    val res = v.resolutionInfo
                    var w = res?.resolution?.width ?: 0
                    var h = res?.resolution?.height ?: 0
                    if (res != null && res.rotationDegrees % 180 != 0) w = h.also { h = w }
                    val started = JSONObject()
                        .put("camera", chosenCamera.label)
                        .put("fps", rate?.upper ?: 30)
                        .put("width", w)
                        .put("height", h)
                        .put("high_speed", highSpeed)
                        .put("live_stream", liveStream)
                    recordingInfo = started
                    link.sendJson(JSONObject(started.toString()).put("type", "result").put("req", req).put("ok", true).put("notes", notes))
                    replied = true
                    updateRecBadge()
                }
                is VideoRecordEvent.Status -> updateRecBadge()
                is VideoRecordEvent.Finalize -> {
                    activeRecording = null
                    recordingInfo = null
                    val usable = file.exists() && file.length() > 0 && event.error in setOf(
                        VideoRecordEvent.Finalize.ERROR_NONE,
                        VideoRecordEvent.Finalize.ERROR_DURATION_LIMIT_REACHED,
                        VideoRecordEvent.Finalize.ERROR_FILE_SIZE_LIMIT_REACHED,
                        VideoRecordEvent.Finalize.ERROR_SOURCE_INACTIVE,
                    )
                    val why = event.cause?.message ?: "error ${event.error}"
                    if (!replied) {
                        link.sendError(req, "recording failed to start ($why)")
                    } else if (usable) {
                        sendRecording(file, token)
                    } else {
                        file.delete()
                        link.sendJson(JSONObject().put("type", "record_error").put("token", token).put("error", why))
                        showRecBadge("Recording failed: $why", hideAfterMs = 4000)
                    }
                    recordingToken = null
                    bindUseCases() // back to the normal setup
                }
            }
        }
    }

    private class VideoSession(val recorder: Recorder, val video: VideoCapture<Recorder>, val rate: Range<Int>?, val liveStream: Boolean)

    /**
     * Binds preview + video (up to 60 fps) on [selector], keeping the live stream unless dropping it
     * gets a faster frame rate. Null if the phone can't record at all.
     */
    private fun bindVideoSession(info: CameraInfo, selector: CameraSelector, wanted: Quality, fps: Int): VideoSession? {
        val provider = cameraProvider ?: return null
        val normal = Recorder.getVideoCapabilities(info).getSupportedQualities(DynamicRange.SDR)
        val q = if (normal.isEmpty()) wanted else pickQuality(normal, wanted)
        val r = Recorder.Builder()
            .setQualitySelector(QualitySelector.from(q, FallbackStrategy.lowerQualityOrHigherThan(q)))
            .build()
        val v = VideoCapture.Builder(r).setTargetRotation(targetRotation).build()
        val want = fps.coerceAtMost(60)
        val preview = buildPreview()
        val analysis = buildAnalysis()
        val options = listOf(listOf(preview, analysis, v), listOf(preview, v)).mapNotNull { useCases ->
            val chosen = try {
                bestRange(info.getSupportedFrameRateRanges(SessionConfig(useCases = useCases)), want)
            } catch (e: Exception) {
                null
            }
            chosen?.let { useCases to it }
        }.sortedWith(compareBy({ abs(it.second.upper - want) }, { -it.first.size }))
        for ((useCases, chosen) in options) {
            val withStream = analysis in useCases
            if (rebind(if (withStream) analysis else null, null) {
                    provider.bindToLifecycle(this, selector, SessionConfig(useCases = useCases, frameRateRange = chosen))
                }
            ) {
                return VideoSession(r, v, chosen, withStream)
            }
        }
        if (rebind(analysis, null) { provider.bindToLifecycle(this, selector, preview, analysis, v) }) {
            return VideoSession(r, v, null, true) // the phone picks the frame rate
        }
        return null
    }

    /** The Camera2 high-speed mode closest to [fps]: (camera id, size, fps), preferring the main camera and [wanted] size. */
    private fun pickCamera2HighSpeed(fps: Int, wanted: Quality): Triple<String, Size, Int>? {
        val maxHeight = when (wanted) {
            Quality.UHD -> 2160
            Quality.FHD -> 1080
            Quality.HD -> 720
            else -> 480
        }
        val mainId = mainCamera()?.id
        return camera2Fast.flatMap { (id, sizes) ->
            sizes.mapNotNull { (size, rates) ->
                val rate = rates.minByOrNull { abs(it - fps) * 2 + if (it > fps) 1 else 0 } ?: return@mapNotNull null
                Triple(id, size, rate)
            }
        }.minWithOrNull(
            compareBy<Triple<String, Size, Int>>(
                { abs(it.third - fps) },
                { if (minOf(it.second.width, it.second.height) <= maxHeight) 0 else 1 },
                { if (it.first == mainId) 0 else 1 },
                { -(it.second.width * it.second.height) },
            ),
        )
    }

    private fun startCamera2HighSpeed(req: String, token: String, id: String, size: Size, rate: Int, maxSeconds: Double, notes: JSONArray) {
        val provider = cameraProvider ?: return
        camera?.cameraInfo?.let { info ->
            info.cameraState.removeObservers(this)
            info.zoomState.removeObservers(this)
            info.torchState.removeObservers(this)
        }
        provider.unbindAll() // Camera2 needs the camera to itself
        imageAnalysis = null
        imageCapture = null
        val cam = backCameras.firstOrNull { it.id == id }
        val hint = (cam ?: mainCamera())?.info?.getSensorRotationDegrees(targetRotation) ?: 90
        val hs = HighSpeedRecorder(this, id, rate, size, hint, File(cacheDir, "rec-$token.mp4"))
        highSpeedRecorder = hs
        showRecBadge("Starting a $rate fps clip…")
        hs.start(
            onStarted = {
                main.post {
                    recordingStartedAt = SystemClock.elapsedRealtime()
                    var w = size.width
                    var h = size.height
                    if (hint % 180 != 0) w = h.also { h = w }
                    val started = JSONObject()
                        .put("camera", (cam?.label ?: "camera $id") + ", Camera2 high-speed")
                        .put("fps", rate)
                        .put("width", w)
                        .put("height", h)
                        .put("high_speed", true)
                        .put("live_stream", false)
                    recordingInfo = started
                    notes.put("the preview and live stream pause during a high-speed clip and come back when it stops")
                    link.sendJson(JSONObject(started.toString()).put("type", "result").put("req", req).put("ok", true).put("notes", notes))
                    updateRecBadge()
                    main.postDelayed(highSpeedLimit, (maxSeconds * 1000).toLong() + 3_000)
                }
            },
            onError = { message ->
                main.post {
                    if (highSpeedRecorder === hs) highSpeedRecorder = null
                    hs.stop { }
                    hs.file.delete()
                    recordingToken = null
                    bindUseCases()
                    link.sendError(req, "high-speed recording failed: $message")
                    showRecBadge("High-speed recording failed", hideAfterMs = 4000)
                }
            },
        )
    }

    private fun finishHighSpeed() {
        val hs = highSpeedRecorder ?: return
        val token = recordingToken ?: return
        highSpeedRecorder = null
        main.removeCallbacks(highSpeedLimit)
        showRecBadge("Finishing the clip…")
        hs.stop { ok ->
            main.post {
                recordingInfo = null
                if (ok) {
                    sendRecording(hs.file, token)
                } else {
                    hs.file.delete()
                    link.sendJson(JSONObject().put("type", "record_error").put("token", token).put("error", "the high-speed clip came out empty"))
                    showRecBadge("Recording failed", hideAfterMs = 4000)
                }
                recordingToken = null
                bindUseCases()
            }
        }
    }

    // --- photos and videos the user takes for Claude ----------------------------------------------

    private fun toast(text: String) = android.widget.Toast.makeText(this, text, android.widget.Toast.LENGTH_SHORT).show()

    private fun refreshCapturesUi() {
        val waiting = captures.describeWaiting()
        capturesText.visibility = if (waiting.isEmpty()) View.GONE else View.VISIBLE
        capturesText.text = "$waiting waiting for Claude · tell Claude you took ${if (captures.list().size == 1) "it" else "them"}"
    }

    private fun takeUserPhoto() {
        val capture = imageCapture
        if (capture == null) {
            toast(if (recordingToken != null) "Wait for the recording to finish" else "The camera isn't ready yet")
            return
        }
        val (_, file, tmp) = captures.newCapture("photo")
        flashPreview()
        capture.takePicture(ImageCapture.OutputFileOptions.Builder(tmp).build(), photoExecutor, object : ImageCapture.OnImageSavedCallback {
            override fun onImageSaved(output: ImageCapture.OutputFileResults) {
                tmp.renameTo(file)
                main.post {
                    refreshCapturesUi()
                    toast("Photo saved for Claude")
                    sendStatus()
                }
            }

            override fun onError(exception: ImageCaptureException) {
                tmp.delete()
                main.post { toast("Couldn't take the photo: ${exception.message}") }
            }
        })
    }

    private fun toggleUserVideo() {
        userVideo?.let {
            it.stop()
            return
        }
        if (recordingToken != null) {
            toast("Claude is recording right now")
            return
        }
        val cam = mainCamera()
        if (cameraProvider == null || cam == null) {
            toast("The camera isn't ready yet")
            return
        }
        recordingToken = USER_RECORDING
        val session = bindVideoSession(cam.info, cam.selector, Quality.FHD, 30)
        if (session == null) {
            recordingToken = null
            bindUseCases()
            toast("This phone can't record video here")
            return
        }
        val (_, file, tmp) = captures.newCapture("video")
        userVideo = session.recorder.prepareRecording(this, FileOutputOptions.Builder(tmp).build())
            .start(ContextCompat.getMainExecutor(this)) { event ->
                when (event) {
                    is VideoRecordEvent.Start -> {
                        recordingStartedAt = SystemClock.elapsedRealtime()
                        videoButton.setImageResource(R.drawable.ic_stop)
                        updateRecBadge()
                    }
                    is VideoRecordEvent.Status -> updateRecBadge()
                    is VideoRecordEvent.Finalize -> {
                        userVideo = null
                        videoButton.setImageResource(R.drawable.ic_record)
                        val usable = tmp.length() > 0 && event.error in setOf(
                            VideoRecordEvent.Finalize.ERROR_NONE,
                            VideoRecordEvent.Finalize.ERROR_SOURCE_INACTIVE,
                        )
                        if (usable) {
                            tmp.renameTo(file)
                            showRecBadge("Video saved for Claude", hideAfterMs = 2500)
                        } else {
                            tmp.delete()
                            showRecBadge("The video couldn't be saved", hideAfterMs = 4000)
                        }
                        recordingToken = null
                        bindUseCases()
                        refreshCapturesUi()
                        sendStatus()
                    }
                }
            }
    }

    private fun capturesJson(): JSONArray = JSONArray(captures.list().map {
        JSONObject().put("id", it.id).put("kind", it.kind).put("taken_at", it.takenAt).put("size", it.file.length())
    })

    /** The server wants one of the user's captures: upload it, then delete it from the phone. */
    private fun sendCapture(id: String, token: String) {
        val cap = captures.find(id)
        if (cap == null) {
            link.sendJson(JSONObject().put("type", "upload_failed").put("token", token).put("error", "it's no longer on the phone"))
            return
        }
        showRecBadge("Sending your ${cap.kind} to Claude…")
        link.upload(cap.file, token) { ok, message ->
            if (ok) {
                captures.delete(id)
                showRecBadge("Sent to Claude", hideAfterMs = 2000)
            } else {
                link.sendJson(JSONObject().put("type", "upload_failed").put("token", token).put("error", message))
                showRecBadge("Couldn't send it: $message", hideAfterMs = 5000)
            }
            refreshCapturesUi()
            sendStatus()
        }
    }

    // --- app updates ------------------------------------------------------------------------------

    private fun maybeCheckForUpdates() {
        val prefs = getSharedPreferences("claudecam", MODE_PRIVATE)
        if (System.currentTimeMillis() - prefs.getLong("update_checked_at", 0) < 20 * 3600_000L) return
        prefs.edit().putLong("update_checked_at", System.currentTimeMillis()).apply()
        updates.check { update, _ -> if (update != null) offerUpdate(update) }
    }

    private fun offerUpdate(update: UpdateChecker.Update) {
        availableUpdate = update
        updateBanner.text = "Update available: Claude Cam ${update.versionName} · tap to see"
        updateBanner.visibility = View.VISIBLE
    }

    private fun checkForUpdatesNow() {
        toast("Checking GitHub for updates…")
        updates.check { update, error ->
            when {
                update != null -> {
                    offerUpdate(update)
                    showUpdateDialog(update)
                }
                error != null -> AlertDialog.Builder(this, android.R.style.Theme_DeviceDefault_Dialog_Alert)
                    .setTitle("Couldn't check for updates")
                    .setMessage("$error\n\nCheck the phone is online, or see the releases page on GitHub.")
                    .setPositiveButton("OK", null)
                    .setNeutralButton("Open GitHub") { _, _ -> openUrl(RELEASES_URL) }
                    .show()
                else -> AlertDialog.Builder(this, android.R.style.Theme_DeviceDefault_Dialog_Alert)
                    .setTitle("You're up to date")
                    .setMessage("Claude Cam ${BuildConfig.VERSION_NAME} is the latest version on GitHub.")
                    .setPositiveButton("OK", null)
                    .show()
            }
        }
    }

    private fun showUpdateDialog(update: UpdateChecker.Update) {
        val size = if (update.size > 0) " (%.1f MB)".format(update.size / 1e6) else ""
        AlertDialog.Builder(this, android.R.style.Theme_DeviceDefault_Dialog_Alert)
            .setTitle("Update to Claude Cam ${update.versionName}?")
            .setMessage(
                "Published by ${update.author} on GitHub, ${update.published}. You have ${BuildConfig.VERSION_NAME}.\n\n" +
                    update.notes + "\n\n" +
                    "It downloads$size from the official release on github.com and is checked against the release's " +
                    "checksum. Android only installs it if it's signed by the same developer as the app you have now.",
            )
            .setPositiveButton("Update") { _, _ -> startUpdate(update) }
            .setNeutralButton("View on GitHub") { _, _ -> openUrl(update.releaseUrl.ifBlank { RELEASES_URL }) }
            .setNegativeButton("Later", null)
            .show()
    }

    private fun startUpdate(update: UpdateChecker.Update) {
        showRecBadge("Downloading the update… 0%")
        updates.download(update, progress = { showRecBadge("Downloading the update… $it%") }) { file, error ->
            if (file == null) {
                showRecBadge("Update failed: $error", hideAfterMs = 6000)
                return@download
            }
            showRecBadge("Update downloaded", hideAfterMs = 2000)
            if (Build.VERSION.SDK_INT >= 26 && !packageManager.canRequestPackageInstalls()) {
                pendingInstall = file
                AlertDialog.Builder(this, android.R.style.Theme_DeviceDefault_Dialog_Alert)
                    .setTitle("Allow updates")
                    .setMessage("Android needs your OK once for Claude Cam to install its own updates. Turn on \"Allow from this source\", then come back.")
                    .setPositiveButton("Open settings") { _, _ ->
                        startActivity(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:$packageName")))
                    }
                    .setNegativeButton("Cancel") { _, _ -> pendingInstall = null }
                    .show()
            } else {
                startActivity(updates.installIntent(file))
            }
        }
    }

    private fun openUrl(url: String) {
        try {
            startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
        } catch (e: Exception) {
            toast("No browser found")
        }
    }

    /** A video shared to Claude Cam from another app (e.g. Samsung's camera in Slow motion). */
    private fun handleShare(intent: Intent?) {
        if (intent?.action != Intent.ACTION_SEND) return
        val uri = if (Build.VERSION.SDK_INT >= 33) {
            intent.getParcelableExtra(Intent.EXTRA_STREAM, Uri::class.java)
        } else {
            @Suppress("DEPRECATION")
            intent.getParcelableExtra(Intent.EXTRA_STREAM)
        } ?: return
        setIntent(Intent()) // don't handle it again if the activity is recreated
        val readable = try {
            contentResolver.openInputStream(uri)?.close() != null
        } catch (e: Exception) {
            false
        }
        if (!readable) {
            showRecBadge("Claude Cam wasn't allowed to read that video. Try sharing it from the Gallery.", hideAfterMs = 6000)
            return
        }
        pendingImport = uri
        if (link.isConnected) offerImport() else showRecBadge("Video ready to send; waiting for the computer…")
    }

    private fun shareInfo(uri: Uri): Pair<String, Long> {
        var name = "video.mp4"
        var size = -1L
        try {
            contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE), null, null, null)?.use { c ->
                if (c.moveToFirst()) {
                    if (!c.isNull(0)) name = c.getString(0)
                    if (!c.isNull(1)) size = c.getLong(1)
                }
            }
        } catch (e: Exception) {
            // keep the defaults
        }
        return name to size
    }

    private fun offerImport() {
        val uri = pendingImport ?: return
        val (name, size) = shareInfo(uri)
        link.sendJson(JSONObject().put("type", "import_offer").put("name", name).put("size", size))
        showRecBadge("Sending $name to the computer…")
    }

    private fun sendImport(token: String) {
        val uri = pendingImport ?: return
        pendingImport = null
        val (name, size) = shareInfo(uri)
        showRecBadge("Sending $name to the computer… %.1f MB".format(size / 1e6))
        link.uploadUri(uri, contentResolver, size, token) { ok, message ->
            if (ok) showRecBadge("Video sent to Claude", hideAfterMs = 3000)
            else showRecBadge("Couldn't send the video: $message", hideAfterMs = 6000)
        }
    }

    private fun stopRecordingFromPhone() {
        val token = recordingToken ?: return
        if (activeRecording == null && highSpeedRecorder == null) return
        link.sendJson(JSONObject().put("type", "record_stopped").put("token", token).put("by", "the user (tapped REC on the phone)"))
        activeRecording?.stop()
        finishHighSpeed()
    }

    private fun sendRecording(file: File, token: String) {
        showRecBadge("Sending video to the computer… %.1f MB".format(file.length() / 1e6))
        link.upload(file, token) { ok, message ->
            file.delete()
            if (ok) {
                showRecBadge("Video saved on the computer", hideAfterMs = 2500)
            } else {
                link.sendJson(JSONObject().put("type", "record_error").put("token", token).put("error", "upload failed: $message"))
                showRecBadge("Couldn't send the video: $message", hideAfterMs = 5000)
            }
        }
    }

    private val hideRecBadge = Runnable { recBadge.visibility = View.GONE }

    private fun updateRecBadge() {
        val secs = (SystemClock.elapsedRealtime() - recordingStartedAt) / 1000
        if (userVideo != null) {
            showRecBadge("● Your video %d:%02d · tap to stop".format(secs / 60, secs % 60))
            return
        }
        val info = recordingInfo ?: return
        showRecBadge("● REC %d:%02d · %d fps · tap to stop".format(secs / 60, secs % 60, info.optInt("fps")))
    }

    private fun showRecBadge(text: String, hideAfterMs: Long = 0) {
        main.removeCallbacks(hideRecBadge)
        recBadge.text = text
        recBadge.visibility = View.VISIBLE
        if (hideAfterMs > 0) main.postDelayed(hideRecBadge, hideAfterMs)
    }

    // --- commands from the server ---------------------------------------------------------------

    override fun onCommand(cmd: JSONObject) {
        when (cmd.optString("type")) {
            "config" -> applyConfig(cmd)
            "photo" -> takePhoto(cmd.optString("req"))
            "control" -> applyControl(cmd)
            "message" -> showMessage(cmd)
            "clear_message" -> hideMessage()
            "activity" -> showActivity(cmd.optString("what"), cmd.optDouble("seconds", 0.0))
            "record_start" -> startRecording(cmd)
            "import_ready" -> sendImport(cmd.optString("token"))
            "captures_list" -> link.sendJson(
                JSONObject().put("type", "result").put("req", cmd.optString("req")).put("ok", true).put("captures", capturesJson()),
            )
            "capture_send" -> sendCapture(cmd.optString("id"), cmd.optString("token"))
            "record_stop" -> {
                if (cmd.optString("token") == recordingToken) {
                    activeRecording?.stop()
                    finishHighSpeed()
                }
                link.sendJson(JSONObject().put("type", "result").put("req", cmd.optString("req")).put("ok", true))
            }
        }
    }

    private fun applyConfig(cmd: JSONObject) {
        val fps = cmd.optDouble("fps", 3.0).coerceIn(0.2, 30.0)
        streamIntervalMs = (1000 / fps).toLong()
        jpegQuality = cmd.optInt("quality", 70).coerceIn(30, 95)
        val size = cmd.optInt("size", 1920).coerceIn(320, 1920)
        if (size != streamSize) {
            streamSize = size
            // While recording, bindUseCases() waits; the new size applies when the recording ends.
            if (cameraProvider != null && hasCameraPermission()) bindUseCases()
        }
    }

    private fun applyControl(cmd: JSONObject) {
        val req = cmd.optString("req")
        val cam = camera
        if (cam == null) {
            link.sendError(req, "the camera is not running on the phone")
            return
        }
        val notes = JSONArray()
        val futures = mutableListOf<ListenableFuture<*>>()
        var focusFuture: ListenableFuture<FocusMeteringResult>? = null

        if (cmd.has("torch")) {
            if (cam.cameraInfo.hasFlashUnit()) {
                torchOn = cmd.optBoolean("torch")
                futures += cam.cameraControl.enableTorch(torchOn)
            } else {
                notes.put("this camera has no flash unit")
            }
        }
        if (cmd.has("zoom")) {
            val zs = cam.cameraInfo.zoomState.value
            val wanted = cmd.optDouble("zoom", 1.0).toFloat()
            val r = if (zs != null) wanted.coerceIn(zs.minZoomRatio, zs.maxZoomRatio) else wanted
            if (r != wanted) notes.put("zoom limited to %.2f (range %.2f-%.2f)".format(r, zs?.minZoomRatio, zs?.maxZoomRatio))
            zoomRatio = r
            futures += cam.cameraControl.setZoomRatio(r)
        }
        if (cmd.has("exposure")) {
            val es = cam.cameraInfo.exposureState
            if (es.isExposureCompensationSupported) {
                val range = es.exposureCompensationRange
                val wanted = cmd.optInt("exposure", 0)
                val i = wanted.coerceIn(range.lower, range.upper)
                if (i != wanted) notes.put("exposure limited to $i (range ${range.lower}..${range.upper})")
                exposureIndex = i
                futures += cam.cameraControl.setExposureCompensationIndex(i)
            } else {
                notes.put("this camera does not support exposure compensation")
            }
        }
        if (cmd.has("focus")) {
            val f = cmd.opt("focus")
            if (f is JSONArray && f.length() == 2) {
                val (s, t) = uprightToSensor(f.optDouble(0).toFloat(), f.optDouble(1).toFloat())
                val point = SurfaceOrientedMeteringPointFactory(1f, 1f).createPoint(s.coerceIn(0f, 1f), t.coerceIn(0f, 1f))
                val action = FocusMeteringAction.Builder(point, FocusMeteringAction.FLAG_AF or FocusMeteringAction.FLAG_AE)
                    .disableAutoCancel()
                    .build()
                val fut = cam.cameraControl.startFocusAndMetering(action)
                focusFuture = fut
                futures += fut
                focusLocked = "locked at (%.2f, %.2f)".format(f.optDouble(0), f.optDouble(1))
            } else {
                futures += cam.cameraControl.cancelFocusAndMetering()
                focusLocked = null
            }
            updateAfButton()
        }

        var replied = false
        var remaining = futures.size
        fun reply() {
            if (replied) return
            replied = true
            focusFuture?.let { fut ->
                val ok = try {
                    fut.isDone && fut.get().isFocusSuccessful
                } catch (e: Exception) {
                    false
                }
                if (!ok) notes.put("focus did not lock (too dark, too close, or no texture there)")
            }
            link.sendJson(
                JSONObject().put("type", "result").put("req", req).put("ok", true)
                    .put("state", statusJson()).put("notes", notes),
            )
        }
        if (remaining == 0) {
            reply()
        } else {
            val mainExecutor = ContextCompat.getMainExecutor(this)
            futures.forEach { it.addListener({ if (--remaining == 0) reply() }, mainExecutor) }
            main.postDelayed({ reply() }, 3000)
        }
    }

    private fun showMessage(cmd: JSONObject) {
        currentMessageId = cmd.optString("id")
        messageText.text = cmd.optString("text")
        doneButton.text = if (cmd.optBoolean("ack")) "Done" else "OK"
        messageCard.visibility = View.VISIBLE
        messageCard.alpha = 0f
        messageCard.animate().alpha(1f).setDuration(200)
        if (cmd.optBoolean("vibrate", true)) vibrate()
    }

    private fun hideMessage() {
        currentMessageId = null
        messageCard.visibility = View.GONE
    }

    private fun acknowledgeMessage() {
        currentMessageId?.let { link.sendJson(JSONObject().put("type", "ack").put("id", it)) }
        hideMessage()
    }

    private fun showActivity(what: String, seconds: Double) {
        main.removeCallbacks(hideBadge)
        if (what == "idle") {
            main.postDelayed(hideBadge, 800)
            return
        }
        lookBadge.text = when (what) {
            "watching" -> "Claude is watching"
            "photo" -> "Claude took a photo"
            else -> "Claude is looking"
        }
        lookBadge.animate().cancel()
        lookBadge.alpha = 1f
        lookBadge.visibility = View.VISIBLE
        val showMs = if (what == "watching") (seconds * 1000).toLong() + 1000 else 2000
        main.postDelayed(hideBadge, showMs)
    }

    // --- link state ---------------------------------------------------------------------------

    override fun onLinkState(state: CamLink.State, detail: String) {
        val (color, text) = when (state) {
            CamLink.State.SEARCHING -> R.color.idle to "Looking for the Claude Cam server…"
            CamLink.State.CONNECTING -> R.color.warn to "Connecting to $detail…"
            CamLink.State.CONNECTED -> R.color.ok to "Connected · ${detail.substringBeforeLast(':')}"
            CamLink.State.DISCONNECTED -> R.color.bad to "Can't reach ${link.target ?: "server"} · retrying"
            CamLink.State.REPLACED -> R.color.idle to "Another phone took over · tap to reconnect"
        }
        statusText.text = text
        statusDot.backgroundTintList = ColorStateList.valueOf(ContextCompat.getColor(this, color))
        if (state == CamLink.State.CONNECTED) {
            sendStatus()
            if (pendingImport != null) offerImport()
        }
        if (state != CamLink.State.CONNECTED) {
            statsText.text = if (state == CamLink.State.DISCONNECTED) detail else ""
        }
    }

    private fun sendStatus() {
        if (link.isConnected) link.sendJson(statusJson().put("type", "status"))
    }

    private fun statusJson(): JSONObject {
        val o = JSONObject()
        val battery = getSystemService(BatteryManager::class.java)
        o.put("battery", battery.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY))
        o.put("charging", battery.isCharging)
        if (Build.VERSION.SDK_INT >= 29) {
            val thermal = getSystemService(PowerManager::class.java).currentThermalStatus
            if (thermal > PowerManager.THERMAL_STATUS_NONE) {
                o.put("thermal", listOf("none", "light", "moderate", "severe", "critical", "emergency", "shutdown").getOrElse(thermal) { "$thermal" })
            }
        }
        val cam = camera
        o.put("camera_ready", cam != null && hasCameraPermission() && cam.cameraInfo.cameraState.value?.type == CameraState.Type.OPEN)
        if (cam != null) {
            cam.cameraInfo.zoomState.value?.let {
                o.put("zoom", it.zoomRatio.toDouble())
                o.put("zoom_min", it.minZoomRatio.toDouble())
                o.put("zoom_max", it.maxZoomRatio.toDouble())
            }
            val es = cam.cameraInfo.exposureState
            if (es.isExposureCompensationSupported) {
                o.put("exposure", es.exposureCompensationIndex)
                o.put("exposure_min", es.exposureCompensationRange.lower)
                o.put("exposure_max", es.exposureCompensationRange.upper)
                o.put("exposure_step", es.exposureCompensationStep.toDouble())
            }
            o.put("has_flash", cam.cameraInfo.hasFlashUnit())
            o.put("torch", cam.cameraInfo.torchState.value == TorchState.ON)
        }
        o.put("focus", focusLocked ?: "auto")
        o.put(
            "orientation",
            when (targetRotation) {
                Surface.ROTATION_90 -> "landscape (turned left)"
                Surface.ROTATION_180 -> "upside down"
                Surface.ROTATION_270 -> "landscape (turned right)"
                else -> "portrait"
            },
        )
        o.put("fps_actual", fpsActual.toDouble())
        o.put("captures_waiting", captures.list().size)
        videoCaps?.let { caps -> caps.keys().forEach { o.put(it, caps.get(it)) } }
        recordingInfo?.let {
            o.put("recording", JSONObject(it.toString()).put("seconds", (SystemClock.elapsedRealtime() - recordingStartedAt) / 1000.0))
        }
        return o
    }

    // --- UI helpers -----------------------------------------------------------------------------

    @SuppressLint("ClickableViewAccessibility")
    private fun setupGestures() {
        val scale = ScaleGestureDetector(this, object : ScaleGestureDetector.SimpleOnScaleGestureListener() {
            override fun onScale(detector: ScaleGestureDetector): Boolean {
                val cam = camera ?: return false
                val zs = cam.cameraInfo.zoomState.value ?: return false
                zoomRatio = (zs.zoomRatio * detector.scaleFactor).coerceIn(zs.minZoomRatio, zs.maxZoomRatio)
                cam.cameraControl.setZoomRatio(zoomRatio)
                return true
            }

            override fun onScaleEnd(detector: ScaleGestureDetector) = sendStatus()
        })
        val taps = GestureDetector(this, object : GestureDetector.SimpleOnGestureListener() {
            override fun onSingleTapUp(e: MotionEvent): Boolean {
                focusAt(e.x, e.y)
                return true
            }
        })
        previewView.setOnTouchListener { _, e ->
            scale.onTouchEvent(e)
            if (!scale.isInProgress && e.pointerCount == 1) taps.onTouchEvent(e)
            true
        }
    }

    private fun showFocusRing(x: Float, y: Float) {
        focusRing.animate().cancel()
        focusRing.translationX = x - focusRing.width.coerceAtLeast(dp(72)) / 2f
        focusRing.translationY = y - focusRing.height.coerceAtLeast(dp(72)) / 2f
        focusRing.alpha = 1f
        focusRing.scaleX = 1.4f
        focusRing.scaleY = 1.4f
        focusRing.visibility = View.VISIBLE
        focusRing.animate().scaleX(1f).scaleY(1f).setDuration(200).withEndAction {
            focusRing.animate().alpha(0f).setStartDelay(900).setDuration(400)
        }
    }

    private fun updateAfButton() {
        afButton.text = if (focusLocked != null) "AF-L" else "AF"
        afButton.setTextColor(ContextCompat.getColor(this, if (focusLocked != null) R.color.accent else android.R.color.white))
    }

    private fun flashPreview() {
        previewView.animate().cancel()
        previewView.alpha = 0.3f
        previewView.animate().alpha(1f).setDuration(250)
    }

    private fun updateStats() {
        if (!link.isConnected) return
        statsText.text = if (streamW > 0) {
            "%.1f fps · %d KB · %d×%d".format(fpsActual, lastFrameKb, streamW, streamH)
        } else {
            "Waiting for camera…"
        }
    }

    private fun vibrate() {
        val vibrator = if (Build.VERSION.SDK_INT >= 31) {
            getSystemService(VibratorManager::class.java).defaultVibrator
        } else {
            @Suppress("DEPRECATION")
            getSystemService(Vibrator::class.java)
        }
        vibrator.vibrate(VibrationEffect.createWaveform(longArrayOf(0, 90, 70, 90), -1))
    }

    private fun showServerDialog() {
        val pad = dp(20)
        val layout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, dp(8), pad, 0)
        }
        val info = TextView(this).apply {
            text = if (link.discovered.isEmpty()) {
                "No server found on this Wi-Fi yet. Enter your computer's local IP address, e.g. 192.168.1.20 " +
                    "(port 8777 is added for you). " +
                    "Over USB, run `adb reverse tcp:8777 tcp:8777` and use 127.0.0.1:8777."
            } else {
                "Found on this Wi-Fi: " + link.discovered.joinToString(", ")
            }
        }
        val input = EditText(this).apply {
            hint = "host:port"
            setText(link.target.orEmpty())
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
            isSingleLine = true
        }
        val version = TextView(this).apply {
            text = "Claude Cam ${BuildConfig.VERSION_NAME}"
            setPadding(0, dp(12), 0, 0)
            alpha = 0.7f
        }
        layout.addView(info)
        layout.addView(input)
        layout.addView(version)
        AlertDialog.Builder(this, android.R.style.Theme_DeviceDefault_Dialog_Alert)
            .setTitle("Claude Cam server")
            .setView(layout)
            .setPositiveButton("Connect") { _, _ -> link.setManualTarget(input.text.toString()) }
            .setNeutralButton("Check for updates") { _, _ -> checkForUpdatesNow() }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun dp(v: Int) = (v * resources.displayMetrics.density).roundToInt()
}
