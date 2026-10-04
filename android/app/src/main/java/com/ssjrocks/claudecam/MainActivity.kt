package com.ssjrocks.claudecam

import android.Manifest
import android.annotation.SuppressLint
import android.app.AlertDialog
import android.content.Intent
import android.content.pm.PackageManager
import android.content.res.ColorStateList
import android.graphics.Bitmap
import android.graphics.Matrix
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
import android.provider.Settings
import android.text.InputType
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
import androidx.camera.core.Camera
import androidx.camera.core.CameraSelector
import androidx.camera.core.CameraState
import androidx.camera.core.FocusMeteringAction
import androidx.camera.core.FocusMeteringResult
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageCapture
import androidx.camera.core.ImageCaptureException
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.core.SurfaceOrientedMeteringPointFactory
import androidx.camera.core.TorchState
import androidx.camera.core.resolutionselector.AspectRatioStrategy
import androidx.camera.core.resolutionselector.ResolutionSelector
import androidx.camera.core.resolutionselector.ResolutionStrategy
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.core.content.ContextCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.updatePadding
import com.google.common.util.concurrent.ListenableFuture
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.util.concurrent.Executors
import kotlin.math.roundToInt

class MainActivity : ComponentActivity(), CamLink.Listener {

    private lateinit var link: CamLink
    private lateinit var previewView: PreviewView
    private lateinit var statusDot: View
    private lateinit var statusText: TextView
    private lateinit var statsText: TextView
    private lateinit var lookBadge: TextView
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
    private var tick = 0

    private val hideBadge = Runnable {
        lookBadge.animate().alpha(0f).setDuration(400).withEndAction { lookBadge.visibility = View.GONE }
    }

    private val ticker = object : Runnable {
        override fun run() {
            updateStats()
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
    }

    override fun onStart() {
        super.onStart()
        // Back from the settings page with the permission granted
        if (permissionView.visibility == View.VISIBLE && hasCameraPermission()) startCamera()
        link.start()
        orientationListener.enable()
        main.post(ticker)
    }

    override fun onStop() {
        super.onStop()
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

    private fun bindUseCases() {
        val provider = cameraProvider ?: return
        val ratio = AspectRatioStrategy.RATIO_4_3_FALLBACK_AUTO_STRATEGY

        val preview = Preview.Builder()
            .setResolutionSelector(ResolutionSelector.Builder().setAspectRatioStrategy(ratio).build())
            .build()
        preview.setSurfaceProvider(previewView.surfaceProvider)

        val analysis = ImageAnalysis.Builder()
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
        analysis.setAnalyzer(analysisExecutor, ::analyze)

        val capture = ImageCapture.Builder()
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

        camera?.cameraInfo?.let { info ->
            info.cameraState.removeObservers(this)
            info.zoomState.removeObservers(this)
            info.torchState.removeObservers(this)
        }
        provider.unbindAll()
        val cam = try {
            provider.bindToLifecycle(this, CameraSelector.DEFAULT_BACK_CAMERA, preview, analysis, capture)
        } catch (e: Exception) {
            statsText.text = "Camera error: ${e.message}"
            return
        }
        camera = cam
        imageAnalysis = analysis
        imageCapture = capture
        settingsApplied = false

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
            link.sendError(req, "the camera is not running on the phone")
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

    // --- commands from the server ---------------------------------------------------------------

    override fun onCommand(cmd: JSONObject) {
        when (cmd.optString("type")) {
            "config" -> applyConfig(cmd)
            "photo" -> takePhoto(cmd.optString("req"))
            "control" -> applyControl(cmd)
            "message" -> showMessage(cmd)
            "clear_message" -> hideMessage()
            "activity" -> showActivity(cmd.optString("what"), cmd.optDouble("seconds", 0.0))
        }
    }

    private fun applyConfig(cmd: JSONObject) {
        val fps = cmd.optDouble("fps", 3.0).coerceIn(0.2, 30.0)
        streamIntervalMs = (1000 / fps).toLong()
        jpegQuality = cmd.optInt("quality", 70).coerceIn(30, 95)
        val size = cmd.optInt("size", 1920).coerceIn(320, 1920)
        if (size != streamSize) {
            streamSize = size
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
        if (state == CamLink.State.CONNECTED) sendStatus()
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
        layout.addView(info)
        layout.addView(input)
        AlertDialog.Builder(this, android.R.style.Theme_DeviceDefault_Dialog_Alert)
            .setTitle("Claude Cam server")
            .setView(layout)
            .setPositiveButton("Connect") { _, _ -> link.setManualTarget(input.text.toString()) }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun dp(v: Int) = (v * resources.displayMetrics.density).roundToInt()
}
