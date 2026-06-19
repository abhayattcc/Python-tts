#!/usr/bin/env python3
"""
build.py — Fully automated build of an Odia Piper-TTS Android APK.

What this does, in order:
  1. Clones piper1-gpl (provides libpiper C++ source + bundled espeak-ng)
  2. Downloads the ONNX Runtime Android AAR and extracts the .so libs
  3. Generates a minimal Android Gradle project (Kotlin + JNI/C++)
  4. Copies your Odia voice model into app assets
  5. Runs `gradlew assembleRelease`
  6. Copies the finished .apk into ./output/

Run this from the repo root. Designed to run unattended in CI
(GitHub Actions), but also runs fine on a local Linux/macOS machine
with Android SDK + NDK installed and ANDROID_NDK_HOME set.
"""

import os
import shutil
import subprocess
import sys
import textwrap
import urllib.request
import zipfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Config — edit these if your model filenames or package name change
# ---------------------------------------------------------------------------

ROOT = Path(__file__).parent.resolve()

# Model is NOT bundled into the APK — it's downloaded on first app launch
# and cached to internal storage, so the app works fully offline after that.
MODEL_ONNX_URL = "https://hear2read.org/Hear2Read/voices-piper/or-tdil-3590v6-low.onnx"
MODEL_JSON_URL = "https://hear2read.org/Hear2Read/voices-piper/or-tdil-3590v6-low.onnx.json"

WORK_DIR = ROOT / "build_work"
PROJECT_DIR = WORK_DIR / "android-project"
OUTPUT_DIR = ROOT / "output"

PACKAGE_NAME = "org.hear2read.odiatts"
APP_NAME = "Odia TTS"

# Pinned by COMMIT SHA, not tag: from v1.3.0 onward piper1-gpl dropped its
# C++ source tree (src/cpp) entirely in favor of a pure-Python
# implementation, so the JNI bridge below (which calls
# piper::PiperConfig / loadVoice / textToAudio) has nothing to link
# against on main or any v1.3.0+ tag.
#
# v1.3.0's own release notes name the commit that removed the C++ code:
#   https://github.com/OHF-Voice/piper1-gpl/releases/tag/v1.3.0  ->  fee9b9c
# PIPER_REF is therefore fee9b9c's PARENT commit — the last commit where
# src/cpp still existed. A commit SHA is used instead of a guessed tag
# name (e.g. "v1.2.0") because there's no confirmed evidence that tag
# exists on this repo; the SHA below is directly traceable to the
# changelog line that announced the C++ removal.
#
# If this SHA ever stops resolving (force-push/history rewrite), check
# https://github.com/OHF-Voice/piper1-gpl/commits/main and pick the last
# commit before "Removed C++ code for now to focus on Python development".
PIPER_REPO = "https://github.com/OHF-Voice/piper1-gpl.git"
PIPER_REF = "fee9b9c~1"
ONNXRUNTIME_VERSION = "1.19.2"
ONNXRUNTIME_AAR_URL = (
    f"https://repo1.maven.org/maven2/com/microsoft/onnxruntime/"
    f"onnxruntime-android/{ONNXRUNTIME_VERSION}/"
    f"onnxruntime-android-{ONNXRUNTIME_VERSION}.aar"
)

# Build only arm64-v8a + armeabi-v7a by default (covers ~all real devices,
# keeps CI time down). Add "x86_64" if you need emulator testing.
ABIS = ["arm64-v8a", "armeabi-v7a"]

NDK_VERSION = "26.1.10909125"


def log(msg: str) -> None:
    print(f"\n>>> {msg}\n", flush=True)


def run(cmd, cwd=None, env=None):
    log(f"Running: {' '.join(str(c) for c in cmd)}")
    result = subprocess.run(cmd, cwd=cwd, env=env)
    if result.returncode != 0:
        sys.exit(f"Command failed ({result.returncode}): {cmd}")


# ---------------------------------------------------------------------------
# Step 0 — sanity checks
# ---------------------------------------------------------------------------

def check_inputs():
    log("Checking config")
    print(f"Model will be downloaded at runtime from:\n  {MODEL_ONNX_URL}\n  {MODEL_JSON_URL}")

    ndk_home = os.environ.get("ANDROID_NDK_HOME") or os.environ.get("ANDROID_NDK_ROOT")
    if not ndk_home:
        sys.exit(
            "ANDROID_NDK_HOME is not set. In CI this is set by the workflow;"
            " locally, export it to point at your NDK install."
        )
    print(f"Using NDK at: {ndk_home}")
    return ndk_home


# ---------------------------------------------------------------------------
# Step 1 — fetch piper1-gpl source (gives us piper.h / piper.cpp + espeak-ng)
# ---------------------------------------------------------------------------

def fetch_piper_source():
    log(f"Fetching piper1-gpl source @ {PIPER_REF}")
    piper_src = WORK_DIR / "piper1-gpl"
    if piper_src.exists():
        print("Already cloned, skipping.")
        return piper_src
    WORK_DIR.mkdir(parents=True, exist_ok=True)

    # PIPER_REF is a commit SHA expression (e.g. "fee9b9c~1"), not a
    # branch/tag name, so `git clone --branch` won't resolve it — GitHub's
    # dumb-http/smart protocol only advertises named refs that way. Instead:
    # init an empty repo, fetch the specific commit (GitHub allows fetching
    # by SHA even on repos that don't enable arbitrary commit fetch by
    # default — falls back to a full clone if that's rejected), then check
    # it out directly.
    piper_src.mkdir(parents=True)
    run(["git", "init"], cwd=piper_src)
    run(["git", "remote", "add", "origin", PIPER_REPO], cwd=piper_src)

    base_sha = PIPER_REF.split("~")[0].split("^")[0]
    fetch_result = subprocess.run(
        ["git", "fetch", "--depth", "200", "origin", base_sha],
        cwd=piper_src,
    )
    if fetch_result.returncode != 0:
        log("Shallow SHA fetch failed — retrying with full history")
        fetch_result = subprocess.run(
            ["git", "fetch", "--unshallow", "origin"], cwd=piper_src
        )
        if fetch_result.returncode != 0:
            # repo has no shallow fetch to deepen yet — do a plain full fetch
            run(["git", "fetch", "origin"], cwd=piper_src)

    checkout_result = subprocess.run(
        ["git", "checkout", PIPER_REF], cwd=piper_src
    )
    if checkout_result.returncode != 0:
        log("Checkout of pinned ref failed — fetching full history as last resort")
        run(["git", "fetch", "--unshallow", "origin"], cwd=piper_src)
        checkout_result = subprocess.run(
            ["git", "checkout", PIPER_REF], cwd=piper_src
        )

    if checkout_result.returncode != 0:
        sys.exit(
            f"Could not check out piper1-gpl commit '{PIPER_REF}'. The pin "
            "in this script may be stale (history rewritten, or it no "
            f"longer exists). Check {PIPER_REPO} manually, find the "
            "last commit before C++ removal (see comment above PIPER_REF "
            "in this file), and update PIPER_REF to that commit's exact SHA."
        )

    cpp_check = piper_src / "src" / "cpp" / "piper.hpp"
    if not cpp_check.exists():
        sys.exit(
            f"Expected {cpp_check} to exist in piper1-gpl@{PIPER_REF} but it "
            "doesn't. Upstream layout may differ from what this script "
            "assumes — check the checked-out tree under "
            f"{piper_src} and update jni_bridge.cpp / CMakeLists.txt in "
            "this script to match."
        )
    return piper_src


# ---------------------------------------------------------------------------
# Step 2 — fetch ONNX Runtime Android AAR and pull out the .so files
# ---------------------------------------------------------------------------

def fetch_onnxruntime():
    log("Downloading ONNX Runtime Android AAR")
    aar_path = WORK_DIR / "onnxruntime-android.aar"
    if not aar_path.exists():
        urllib.request.urlretrieve(ONNXRUNTIME_AAR_URL, aar_path)
    extract_dir = WORK_DIR / "onnxruntime-extracted"
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir(parents=True)
    with zipfile.ZipFile(aar_path) as z:
        z.extractall(extract_dir)
    print(f"Extracted ONNX Runtime AAR to {extract_dir}")
    return extract_dir


# ---------------------------------------------------------------------------
# Step 3 — generate the Android Gradle project
# ---------------------------------------------------------------------------

def write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip("\n"))


def generate_gradle_project(onnxruntime_dir: Path):
    log("Generating Android Gradle project")
    pkg_path = PACKAGE_NAME.replace(".", "/")

    # --- settings.gradle.kts ---
    write(PROJECT_DIR / "settings.gradle.kts", f"""
        pluginManagement {{
            repositories {{
                google()
                mavenCentral()
                gradlePluginPortal()
            }}
        }}
        dependencyResolutionManagement {{
            repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
            repositories {{
                google()
                mavenCentral()
            }}
        }}
        rootProject.name = "OdiaTTS"
        include(":app")
    """)

    # --- root build.gradle.kts ---
    write(PROJECT_DIR / "build.gradle.kts", """
        plugins {
            id("com.android.application") version "8.5.0" apply false
            id("org.jetbrains.kotlin.android") version "1.9.24" apply false
        }
    """)

    # --- gradle.properties ---
    write(PROJECT_DIR / "gradle.properties", """
        org.gradle.jvmargs=-Xmx4g
        android.useAndroidX=true
        kotlin.code.style=official
    """)

    # --- app/build.gradle.kts ---
    abi_filters = ", ".join(f'"{a}"' for a in ABIS)
    write(PROJECT_DIR / "app" / "build.gradle.kts", f"""
        plugins {{
            id("com.android.application")
            id("org.jetbrains.kotlin.android")
        }}

        android {{
            namespace = "{PACKAGE_NAME}"
            compileSdk = 34

            defaultConfig {{
                applicationId = "{PACKAGE_NAME}"
                minSdk = 24
                targetSdk = 34
                versionCode = 1
                versionName = "1.0"

                ndk {{
                    abiFilters += listOf({abi_filters})
                }}

                externalNativeBuild {{
                    cmake {{
                        cppFlags += "-std=c++17"
                        arguments += listOf("-DANDROID_STL=c++_shared")
                    }}
                }}
            }}

            externalNativeBuild {{
                cmake {{
                    path = file("src/main/cpp/CMakeLists.txt")
                    version = "3.22.1"
                }}
            }}

            buildTypes {{
                release {{
                    isMinifyEnabled = false
                }}
            }}

            compileOptions {{
                sourceCompatibility = JavaVersion.VERSION_17
                targetCompatibility = JavaVersion.VERSION_17
            }}
            kotlinOptions {{
                jvmTarget = "17"
            }}

            // Model files are large; don't let AAPT try to compress .onnx
            androidResources {{
                noCompress += listOf("onnx", "json")
            }}

            packaging {{
                jniLibs {{
                    useLegacyPackaging = true
                }}
            }}

            buildFeatures {{
                prefab = true
            }}
        }}

        dependencies {{
            implementation("androidx.core:core-ktx:1.13.1")
            implementation(files("libs/onnxruntime.aar"))
        }}
    """)

    # Copy the onnxruntime aar as a local lib dependency (simplest reliable
    # path — avoids fighting Maven coordinate resolution for the exact
    # native build in CI).
    libs_dir = PROJECT_DIR / "app" / "libs"
    libs_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(WORK_DIR / "onnxruntime-android.aar", libs_dir / "onnxruntime.aar")

    # --- AndroidManifest.xml ---
    write(PROJECT_DIR / "app" / "src" / "main" / "AndroidManifest.xml", f"""
        <?xml version="1.0" encoding="utf-8"?>
        <manifest xmlns:android="http://schemas.android.com/apk/res/android"
            xmlns:tools="http://schemas.android.com/tools">

            <uses-permission android:name="android.permission.INTERNET" />
        <uses-permission android:name="android.permission.ACCESS_NETWORK_STATE" />
        <!-- Only needed on API <= 28 for saving exported AAC files to the
             public Music folder; API 29+ uses MediaStore instead, which
             needs no storage permission. -->
        <uses-permission
            android:name="android.permission.WRITE_EXTERNAL_STORAGE"
            android:maxSdkVersion="28" />

            <application
                android:allowBackup="true"
                android:label="{APP_NAME}"
                android:icon="@android:drawable/ic_btn_speak_now"
                android:theme="@android:style/Theme.Material.Light">

                <activity
                    android:name=".MainActivity"
                    android:exported="true">
                    <intent-filter>
                        <action android:name="android.intent.action.MAIN" />
                        <category android:name="android.intent.category.LAUNCHER" />
                    </intent-filter>
                </activity>

                <!-- System-wide TTS engine registration -->
                <service
                    android:name=".OdiaTtsService"
                    android:exported="true"
                    android:permission="android.permission.BIND_TTS_ENGINE">
                    <intent-filter>
                        <action android:name="android.intent.action.TTS_SERVICE" />
                        <category android:name="android.intent.category.DEFAULT" />
                    </intent-filter>
                    <meta-data
                        android:name="android.speech.tts"
                        android:resource="@xml/tts_engine" />
                </service>
            </application>
        </manifest>
    """)

    # --- res/xml/tts_engine.xml (declares this app as a TTS engine to Android) ---
    # NOTE: android:locale / android:quality / android:latency /
    # android:requiresNetworkConnection are NOT real framework attributes —
    # there is no public per-voice metadata schema for TextToSpeechService
    # voices via this XML (that caused the AAPT "attribute not found" build
    # failures). Voice/locale availability is reported in code instead, via
    # OdiaTtsService.onGetLanguage() / onIsLanguageAvailable() / getVoices().
    # This file just needs to exist and be referenced from the manifest.
    write(PROJECT_DIR / "app" / "src" / "main" / "res" / "xml" / "tts_engine.xml", """
        <?xml version="1.0" encoding="utf-8"?>
        <voices xmlns:android="http://schemas.android.com/apk/res/android" />
    """)

    # --- JNI / C++ bridge ---
    generate_native_code()

    # --- Kotlin sources ---
    generate_kotlin_sources(pkg_path)

    # Model files are NOT copied into assets — ModelDownloader.kt fetches
    # them at first launch and caches them to internal storage instead.
    assets_dir = PROJECT_DIR / "app" / "src" / "main" / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)


def generate_native_code():
    cpp_dir = PROJECT_DIR / "app" / "src" / "main" / "cpp"

    write(cpp_dir / "CMakeLists.txt", """
        cmake_minimum_required(VERSION 3.22.1)
        project(odiatts)

        set(CMAKE_CXX_STANDARD 17)
        set(CMAKE_CXX_STANDARD_REQUIRED ON)

        add_subdirectory(piper_src/src/cpp piper_build)

        add_library(odiatts_jni SHARED jni_bridge.cpp)

        find_library(log-lib log)

        target_include_directories(odiatts_jni PRIVATE
            piper_src/src/cpp
        )

        target_link_libraries(odiatts_jni
            piper
            ${log-lib}
        )
    """)

    write(cpp_dir / "jni_bridge.cpp", """
        // Thin JNI bridge: Kotlin <-> libpiper
        //
        // Exposes three calls to Kotlin:
        //   nativeInit(modelPath, configPath, espeakDataPath) -> handle
        //   nativeSynthesize(handle, text) -> float[] PCM samples @ model sample rate
        //   nativeRelease(handle)
        //
        // NOTE: piper1-gpl's public C++ API has shifted across versions.
        // Check piper_src/src/cpp/piper.hpp at build time and adjust the
        // calls below (PiperConfig / Voice / loadVoice / textToAudio names)
        // to match exactly — this file is a template, not guaranteed to
        // compile unmodified against every piper1-gpl commit.

        #include <jni.h>
        #include <string>
        #include <vector>
        #include <android/log.h>
        #include "piper.hpp"

        #define LOG_TAG "OdiaTTS-JNI"
        #define LOGI(...) __android_log_print(ANDROID_LOG_INFO, LOG_TAG, __VA_ARGS__)
        #define LOGE(...) __android_log_print(ANDROID_LOG_ERROR, LOG_TAG, __VA_ARGS__)

        struct PiperHandle {
            piper::PiperConfig config;
            piper::Voice voice;
        };

        extern "C" JNIEXPORT jlong JNICALL
        Java_org_hear2read_odiatts_PiperBridge_nativeInit(
                JNIEnv *env, jobject /* this */,
                jstring modelPath, jstring configPath, jstring espeakDataPath) {

            const char *model = env->GetStringUTFChars(modelPath, nullptr);
            const char *cfg = env->GetStringUTFChars(configPath, nullptr);
            const char *espeak = env->GetStringUTFChars(espeakDataPath, nullptr);

            auto *handle = new PiperHandle();
            handle->config.eSpeakDataPath = std::string(espeak);

            try {
                piper::loadVoice(
                    handle->config,
                    std::string(model),
                    std::string(cfg),
                    handle->voice,
                    std::optional<piper::SpeakerId>{},
                    false
                );
                piper::initialize(handle->config);
            } catch (const std::exception &e) {
                LOGE("Failed to load voice: %s", e.what());
                env->ReleaseStringUTFChars(modelPath, model);
                env->ReleaseStringUTFChars(configPath, cfg);
                env->ReleaseStringUTFChars(espeakDataPath, espeak);
                delete handle;
                return 0;
            }

            env->ReleaseStringUTFChars(modelPath, model);
            env->ReleaseStringUTFChars(configPath, cfg);
            env->ReleaseStringUTFChars(espeakDataPath, espeak);

            return reinterpret_cast<jlong>(handle);
        }

        extern "C" JNIEXPORT jfloatArray JNICALL
        Java_org_hear2read_odiatts_PiperBridge_nativeSynthesize(
                JNIEnv *env, jobject /* this */, jlong handlePtr, jstring text) {

            auto *handle = reinterpret_cast<PiperHandle *>(handlePtr);
            if (handle == nullptr) return env->NewFloatArray(0);

            const char *textChars = env->GetStringUTFChars(text, nullptr);
            std::string input(textChars);
            env->ReleaseStringUTFChars(text, textChars);

            std::vector<int16_t> audioBuffer;
            piper::SynthesisResult result;

            try {
                piper::textToAudio(
                    handle->config, handle->voice, input,
                    audioBuffer, result, nullptr
                );
            } catch (const std::exception &e) {
                LOGE("Synthesis failed: %s", e.what());
                return env->NewFloatArray(0);
            }

            jfloatArray out = env->NewFloatArray(static_cast<jsize>(audioBuffer.size()));
            std::vector<float> floatBuf(audioBuffer.size());
            for (size_t i = 0; i < audioBuffer.size(); i++) {
                floatBuf[i] = audioBuffer[i] / 32768.0f;
            }
            env->SetFloatArrayRegion(out, 0, static_cast<jsize>(floatBuf.size()), floatBuf.data());
            return out;
        }

        extern "C" JNIEXPORT void JNICALL
        Java_org_hear2read_odiatts_PiperBridge_nativeRelease(
                JNIEnv *env, jobject /* this */, jlong handlePtr) {
            auto *handle = reinterpret_cast<PiperHandle *>(handlePtr);
            delete handle;
        }
    """)


def generate_kotlin_sources(pkg_path: str):
    kt_dir = PROJECT_DIR / "app" / "src" / "main" / "java" / pkg_path

    write(kt_dir / "ModelDownloader.kt", f"""
        package {PACKAGE_NAME}

        import android.content.Context
        import java.io.File
        import java.net.HttpURLConnection
        import java.net.URL

        /** Downloads the Odia voice model on first launch and caches it to
         * internal storage. Subsequent launches detect the cached files and
         * skip downloading, so the app works fully offline after first run. */
        object ModelDownloader {{

            const val MODEL_URL = "{MODEL_ONNX_URL}"
            const val CONFIG_URL = "{MODEL_JSON_URL}"

            fun modelDir(context: Context): File = File(context.filesDir, "model")
            fun modelFile(context: Context): File = File(modelDir(context), "model.onnx")
            fun configFile(context: Context): File = File(modelDir(context), "model.onnx.json")

            /** True if both files are already downloaded and non-empty. */
            fun isModelReady(context: Context): Boolean {{
                val m = modelFile(context)
                val c = configFile(context)
                return m.exists() && m.length() > 0 && c.exists() && c.length() > 0
            }}

            /**
             * Downloads both files if missing. Safe to call every launch —
             * it's a no-op when files are already present.
             *
             * @param onProgress called with (bytesDownloaded, totalBytes, fileLabel)
             *                   totalBytes may be -1 if the server doesn't send
             *                   Content-Length.
             * @throws Exception on network failure — caller should catch and
             *                   show a retry UI rather than crash.
             */
            fun ensureModelDownloaded(
                context: Context,
                onProgress: (Long, Long, String) -> Unit = {{ _, _, _ -> }}
            ) {{
                if (isModelReady(context)) return

                modelDir(context).mkdirs()

                if (!(modelFile(context).exists() && modelFile(context).length() > 0)) {{
                    downloadFile(MODEL_URL, modelFile(context)) {{ done, total ->
                        onProgress(done, total, "voice model")
                    }}
                }}

                if (!(configFile(context).exists() && configFile(context).length() > 0)) {{
                    downloadFile(CONFIG_URL, configFile(context)) {{ done, total ->
                        onProgress(done, total, "voice config")
                    }}
                }}
            }}

            private fun downloadFile(
                urlStr: String,
                destFile: File,
                onProgress: (Long, Long) -> Unit
            ) {{
                val tmpFile = File(destFile.parentFile, destFile.name + ".part")
                val connection = URL(urlStr).openConnection() as HttpURLConnection
                connection.connectTimeout = 15000
                connection.readTimeout = 15000
                connection.instanceFollowRedirects = true
                connection.connect()

                if (connection.responseCode !in 200..299) {{
                    throw java.io.IOException(
                        "Download failed (HTTP ${{connection.responseCode}}) for $urlStr"
                    )
                }}

                val total = connection.contentLengthLong
                var downloaded = 0L

                connection.inputStream.use {{ input ->
                    tmpFile.outputStream().use {{ output ->
                        val buffer = ByteArray(64 * 1024)
                        while (true) {{
                            val read = input.read(buffer)
                            if (read == -1) break
                            output.write(buffer, 0, read)
                            downloaded += read
                            onProgress(downloaded, total)
                        }}
                    }}
                }}

                if (!tmpFile.renameTo(destFile)) {{
                    tmpFile.copyTo(destFile, overwrite = true)
                    tmpFile.delete()
                }}
            }}
        }}
    """)

    write(kt_dir / "PiperBridge.kt", f"""
        package {PACKAGE_NAME}

        class PiperBridge {{
            companion object {{
                init {{
                    System.loadLibrary("odiatts_jni")
                }}
            }}

            private var handle: Long = 0

            fun init(modelPath: String, configPath: String, espeakDataPath: String): Boolean {{
                handle = nativeInit(modelPath, configPath, espeakDataPath)
                return handle != 0L
            }}

            fun synthesize(text: String): FloatArray {{
                if (handle == 0L) return FloatArray(0)
                return nativeSynthesize(handle, text)
            }}

            fun release() {{
                if (handle != 0L) {{
                    nativeRelease(handle)
                    handle = 0
                }}
            }}

            private external fun nativeInit(
                modelPath: String, configPath: String, espeakDataPath: String
            ): Long
            private external fun nativeSynthesize(handle: Long, text: String): FloatArray
            private external fun nativeRelease(handle: Long)
        }}
    """)

    write(kt_dir / "AssetExtractor.kt", f"""
        package {PACKAGE_NAME}

        import android.content.Context
        import java.io.File
        import java.io.FileOutputStream

        /** Copies bundled assets (model files, espeak-ng-data) out to internal
         * storage once, since native code needs real filesystem paths. */
        object AssetExtractor {{

            fun extractAssetDir(context: Context, assetPath: String, destDir: File): File {{
                if (!destDir.exists()) destDir.mkdirs()
                val files = context.assets.list(assetPath) ?: emptyArray()
                if (files.isEmpty()) {{
                    // It's a file, not a directory
                    val outFile = File(destDir.parentFile, destDir.name)
                    copyAssetFile(context, assetPath, outFile)
                    return outFile
                }}
                for (f in files) {{
                    val childAssetPath = "$assetPath/$f"
                    val childOut = File(destDir, f)
                    val children = context.assets.list(childAssetPath)
                    if (children != null && children.isNotEmpty()) {{
                        extractAssetDir(context, childAssetPath, childOut)
                    }} else {{
                        copyAssetFile(context, childAssetPath, childOut)
                    }}
                }}
                return destDir
            }}

            fun copyAssetFile(context: Context, assetPath: String, outFile: File) {{
                if (outFile.exists() && outFile.length() > 0) return
                outFile.parentFile?.mkdirs()
                context.assets.open(assetPath).use {{ input ->
                    FileOutputStream(outFile).use {{ output ->
                        input.copyTo(output)
                    }}
                }}
            }}
        }}
    """)

    write(kt_dir / "MainActivity.kt", f"""
        package {PACKAGE_NAME}

        import android.content.ContentValues
        import android.media.AudioFormat
        import android.media.AudioManager
        import android.media.AudioTrack
        import android.media.MediaCodec
        import android.media.MediaCodecInfo
        import android.media.MediaFormat
        import android.media.MediaMuxer
        import android.os.Build
        import android.os.Bundle
        import android.os.Environment
        import android.provider.MediaStore
        import android.text.InputType
        import android.view.Gravity
        import android.view.ViewGroup
        import android.widget.Button
        import android.widget.EditText
        import android.widget.LinearLayout
        import android.widget.ProgressBar
        import android.widget.ScrollView
        import android.widget.TextView
        import android.widget.Toast
        import androidx.appcompat.app.AppCompatActivity
        import java.io.File
        import java.nio.ByteBuffer
        import kotlin.concurrent.thread

        class MainActivity : AppCompatActivity() {{

            private lateinit var bridge: PiperBridge
            private lateinit var statusText: TextView
            private lateinit var progressBar: ProgressBar
            private lateinit var speakButton: Button
            private lateinit var exportButton: Button
            private lateinit var input: EditText

            private val sampleRate = 22050 // must match your model's sample_rate in model.onnx.json

            override fun onCreate(savedInstanceState: Bundle?) {{
                super.onCreate(savedInstanceState)

                val pad = (16 * resources.displayMetrics.density).toInt()

                val layout = LinearLayout(this)
                layout.orientation = LinearLayout.VERTICAL
                layout.setPadding(pad, pad, pad, pad)

                statusText = TextView(this)
                statusText.text = "Checking voice model..."

                progressBar = ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal)
                progressBar.max = 100

                // --- Unlimited-length, easy-to-read text input ---
                // No maxLength / InputFilter is set, so the field accepts text of
                // any length. It's wrapped in its own ScrollView with a fixed
                // height so long text scrolls within the box instead of pushing
                // the Speak/Export buttons off-screen, and font size + line
                // spacing are bumped up for readability.
                input = EditText(this)
                input.hint = "Type or paste Odia text here (any length)"
                input.inputType = (
                    InputType.TYPE_CLASS_TEXT or
                    InputType.TYPE_TEXT_FLAG_MULTI_LINE or
                    InputType.TYPE_TEXT_FLAG_CAP_SENTENCES
                )
                input.gravity = Gravity.TOP or Gravity.START
                input.minLines = 6
                input.textSize = 18f
                input.setLineSpacing(8f, 1.2f)
                input.setPadding(pad, pad, pad, pad)
                input.isVerticalScrollBarEnabled = true

                val inputScroll = ScrollView(this)
                inputScroll.layoutParams = LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT,
                    (260 * resources.displayMetrics.density).toInt()
                )
                inputScroll.addView(input)

                val buttonRow = LinearLayout(this)
                buttonRow.orientation = LinearLayout.HORIZONTAL

                speakButton = Button(this)
                speakButton.text = "Speak"
                speakButton.isEnabled = false
                speakButton.layoutParams = LinearLayout.LayoutParams(
                    0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f
                )

                exportButton = Button(this)
                exportButton.text = "Export to AAC"
                exportButton.isEnabled = false
                exportButton.layoutParams = LinearLayout.LayoutParams(
                    0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f
                )

                buttonRow.addView(speakButton)
                buttonRow.addView(exportButton)

                layout.addView(statusText)
                layout.addView(progressBar)
                layout.addView(inputScroll)
                layout.addView(buttonRow)
                setContentView(layout)

                bridge = PiperBridge()

                prepareModelThenInit()

                speakButton.setOnClickListener {{
                    val text = input.text.toString()
                    if (text.isBlank()) {{
                        Toast.makeText(this, "Type some text first", Toast.LENGTH_SHORT).show()
                        return@setOnClickListener
                    }}
                    setBusy(true, "Synthesizing...")
                    thread {{
                        try {{
                            val samples = bridge.synthesize(text)
                            playAudio(samples)
                            runOnUiThread {{ setBusy(false, "Ready") }}
                        }} catch (e: Exception) {{
                            runOnUiThread {{ setBusy(false, "Speak failed: ${{e.message}}") }}
                        }}
                    }}
                }}

                exportButton.setOnClickListener {{
                    val text = input.text.toString()
                    if (text.isBlank()) {{
                        Toast.makeText(this, "Type some text first", Toast.LENGTH_SHORT).show()
                        return@setOnClickListener
                    }}
                    setBusy(true, "Synthesizing for export...")
                    thread {{
                        try {{
                            val samples = bridge.synthesize(text)
                            runOnUiThread {{ statusText.text = "Encoding AAC..." }}
                            val savedPath = exportToAac(samples)
                            runOnUiThread {{
                                setBusy(false, "Saved: $savedPath")
                                Toast.makeText(
                                    this, "Saved to $savedPath", Toast.LENGTH_LONG
                                ).show()
                            }}
                        }} catch (e: Exception) {{
                            runOnUiThread {{ setBusy(false, "Export failed: ${{e.message}}") }}
                        }}
                    }}
                }}
            }}

            private fun setBusy(busy: Boolean, message: String) {{
                statusText.text = message
                speakButton.isEnabled = !busy
                exportButton.isEnabled = !busy
            }}

            /** Downloads the model on first launch (skips if already cached),
             * then loads it into the native engine. Runs off the UI thread. */
            private fun prepareModelThenInit() {{
                thread {{
                    try {{
                        if (!ModelDownloader.isModelReady(this)) {{
                            runOnUiThread {{
                                statusText.text = "Downloading Odia voice (first launch only)..."
                            }}
                        }}
                        ModelDownloader.ensureModelDownloaded(this) {{ done, total, label ->
                            runOnUiThread {{
                                if (total > 0) {{
                                    val pct = ((done * 100) / total).toInt()
                                    progressBar.progress = pct
                                    statusText.text = "Downloading $label... $pct%%"
                                }} else {{
                                    statusText.text = "Downloading $label... ${{done / 1024}} KB"
                                }}
                            }}
                        }}

                        runOnUiThread {{ statusText.text = "Loading voice engine..." }}

                        val espeakDir = File(filesDir, "espeak-ng-data")
                        AssetExtractor.extractAssetDir(this, "espeak-ng-data", espeakDir)

                        val ok = bridge.init(
                            ModelDownloader.modelFile(this).absolutePath,
                            ModelDownloader.configFile(this).absolutePath,
                            espeakDir.absolutePath
                        )

                        runOnUiThread {{
                            if (ok) {{
                                statusText.text = "Ready"
                                progressBar.progress = 100
                                speakButton.isEnabled = true
                                exportButton.isEnabled = true
                            }} else {{
                                statusText.text = "Failed to load voice engine"
                            }}
                        }}
                    }} catch (e: Exception) {{
                        runOnUiThread {{
                            statusText.text = "Download failed: ${{e.message}}. Check connection and reopen the app."
                        }}
                    }}
                }}
            }}

            private fun playAudio(samples: FloatArray) {{
                if (samples.isEmpty()) return
                val track = AudioTrack.Builder()
                    .setAudioFormat(
                        AudioFormat.Builder()
                            .setEncoding(AudioFormat.ENCODING_PCM_FLOAT)
                            .setSampleRate(sampleRate)
                            .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                            .build()
                    )
                    .setBufferSizeInBytes(samples.size * 4)
                    .setTransferMode(AudioTrack.MODE_STATIC)
                    .setUsage(AudioManager.STREAM_MUSIC)
                    .build()
                track.write(samples, 0, samples.size, AudioTrack.WRITE_BLOCKING)
                track.play()
            }}

            /**
             * Encodes float PCM samples to AAC (.m4a, ADTS-free / MP4 container
             * via MediaMuxer) using the platform AAC-LC encoder, then saves the
             * result. On API 29+ this goes through MediaStore into the public
             * Music/OdiaTTS folder; on older versions it falls back to the
             * app's external files directory (no extra permission needed
             * either way). Returns a human-readable path/description for the
             * "Saved: ..." status message.
             */
            private fun exportToAac(samples: FloatArray): String {{
                if (samples.isEmpty()) throw IllegalStateException("No audio produced")

                // Convert float [-1, 1] PCM to 16-bit PCM bytes (little-endian),
                // which is what the AAC encoder's input format expects.
                val pcm16 = ShortArray(samples.size)
                for (i in samples.indices) {{
                    val v = (samples[i] * 32767.0f).toInt().coerceIn(-32768, 32767)
                    pcm16[i] = v.toShort()
                }}
                val pcmBytes = ByteArray(pcm16.size * 2)
                for (i in pcm16.indices) {{
                    pcmBytes[i * 2] = (pcm16[i].toInt() and 0xFF).toByte()
                    pcmBytes[i * 2 + 1] = ((pcm16[i].toInt() shr 8) and 0xFF).toByte()
                }}

                val fileName = "odia_tts_${{System.currentTimeMillis()}}.m4a"
                val tempFile = File(cacheDir, fileName)

                encodePcmToAacFile(pcmBytes, tempFile)

                return saveAacToPublicStorage(tempFile, fileName)
            }}

            private fun encodePcmToAacFile(pcmBytes: ByteArray, outFile: File) {{
                val format = MediaFormat.createAudioFormat(
                    MediaFormat.MIMETYPE_AUDIO_AAC, sampleRate, 1
                )
                format.setInteger(
                    MediaFormat.KEY_AAC_PROFILE,
                    MediaCodecInfo.CodecProfileLevel.AACObjectLC
                )
                format.setInteger(MediaFormat.KEY_BIT_RATE, 128000)

                val codec = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_AUDIO_AAC)
                codec.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
                codec.start()

                val muxer = MediaMuxer(outFile.absolutePath, MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4)
                var trackIndex = -1
                var muxerStarted = false

                var offset = 0
                var sawInputEOS = false
                var sawOutputEOS = false
                val bufferInfo = MediaCodec.BufferInfo()
                val timeoutUs = 10000L

                while (!sawOutputEOS) {{
                    if (!sawInputEOS) {{
                        val inIndex = codec.dequeueInputBuffer(timeoutUs)
                        if (inIndex >= 0) {{
                            val inBuffer: ByteBuffer = codec.getInputBuffer(inIndex)!!
                            inBuffer.clear()
                            val remaining = pcmBytes.size - offset
                            val toWrite = minOf(inBuffer.capacity(), remaining)
                            if (toWrite <= 0) {{
                                codec.queueInputBuffer(
                                    inIndex, 0, 0, 0, MediaCodec.BUFFER_FLAG_END_OF_STREAM
                                )
                                sawInputEOS = true
                            }} else {{
                                inBuffer.put(pcmBytes, offset, toWrite)
                                val presentationTimeUs =
                                    (offset.toLong() / 2) * 1_000_000L / sampleRate
                                codec.queueInputBuffer(
                                    inIndex, 0, toWrite, presentationTimeUs, 0
                                )
                                offset += toWrite
                            }}
                        }}
                    }}

                    var outIndex = codec.dequeueOutputBuffer(bufferInfo, timeoutUs)
                    while (outIndex >= 0) {{
                        val outBuffer = codec.getOutputBuffer(outIndex)
                        if (outBuffer != null && bufferInfo.size > 0 && muxerStarted) {{
                            outBuffer.position(bufferInfo.offset)
                            outBuffer.limit(bufferInfo.offset + bufferInfo.size)
                            muxer.writeSampleData(trackIndex, outBuffer, bufferInfo)
                        }}
                        val isEOS = (bufferInfo.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM) != 0
                        codec.releaseOutputBuffer(outIndex, false)
                        if (isEOS) {{
                            sawOutputEOS = true
                            outIndex = -2
                        }} else {{
                            outIndex = codec.dequeueOutputBuffer(bufferInfo, timeoutUs)
                        }}
                    }}
                    if (outIndex == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {{
                        trackIndex = muxer.addTrack(codec.outputFormat)
                        muxer.start()
                        muxerStarted = true
                    }}
                }}

                codec.stop()
                codec.release()
                if (muxerStarted) {{
                    muxer.stop()
                }}
                muxer.release()
            }}

            /** Copies the encoded .m4a into public storage so it shows up in
             * the user's Music app / file manager, then deletes the temp copy. */
            private fun saveAacToPublicStorage(tempFile: File, fileName: String): String {{
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {{
                    val values = ContentValues().apply {{
                        put(MediaStore.Audio.Media.DISPLAY_NAME, fileName)
                        put(MediaStore.Audio.Media.MIME_TYPE, "audio/mp4")
                        put(MediaStore.Audio.Media.RELATIVE_PATH, "Music/OdiaTTS")
                        put(MediaStore.Audio.Media.IS_PENDING, 1)
                    }}
                    val resolver = contentResolver
                    val uri = resolver.insert(MediaStore.Audio.Media.EXTERNAL_CONTENT_URI, values)
                        ?: throw IllegalStateException("Could not create MediaStore entry")
                    resolver.openOutputStream(uri)?.use {{ out ->
                        tempFile.inputStream().use {{ input -> input.copyTo(out) }}
                    }}
                    values.clear()
                    values.put(MediaStore.Audio.Media.IS_PENDING, 0)
                    resolver.update(uri, values, null, null)
                    tempFile.delete()
                    return "Music/OdiaTTS/$fileName"
                }} else {{
                    val musicDir = Environment.getExternalStoragePublicDirectory(
                        Environment.DIRECTORY_MUSIC
                    )
                    val destDir = File(musicDir, "OdiaTTS")
                    destDir.mkdirs()
                    val destFile = File(destDir, fileName)
                    tempFile.copyTo(destFile, overwrite = true)
                    tempFile.delete()
                    return destFile.absolutePath
                }}
            }}

            override fun onDestroy() {{
                bridge.release()
                super.onDestroy()
            }}
        }}
    """)

    write(kt_dir / "OdiaTtsService.kt", f"""
        package {PACKAGE_NAME}

        import android.speech.tts.SynthesisCallback
        import android.speech.tts.SynthesisRequest
        import android.speech.tts.TextToSpeechService
        import java.io.File
        import kotlin.concurrent.thread

        /** Registers this app as a system-wide TTS engine so any app
         * (screen readers, e-book readers, etc.) can use the Odia voice.
         * The model is downloaded on first use and cached; if a synthesis
         * request comes in before the download finishes, this returns an
         * error for that one request rather than blocking indefinitely —
         * the caller (e.g. a screen reader) will typically retry shortly,
         * by which point the background download has usually completed. */
        class OdiaTtsService : TextToSpeechService() {{

            private lateinit var bridge: PiperBridge
            @Volatile private var ready = false
            @Volatile private var preparing = false

            override fun onCreate() {{
                super.onCreate()
                bridge = PiperBridge()
                prepareInBackground()
            }}

            private fun prepareInBackground() {{
                if (preparing || ready) return
                preparing = true
                thread {{
                    try {{
                        ModelDownloader.ensureModelDownloaded(this)

                        val espeakDir = File(filesDir, "espeak-ng-data")
                        AssetExtractor.extractAssetDir(this, "espeak-ng-data", espeakDir)

                        ready = bridge.init(
                            ModelDownloader.modelFile(this).absolutePath,
                            ModelDownloader.configFile(this).absolutePath,
                            espeakDir.absolutePath
                        )
                    }} catch (e: Exception) {{
                        ready = false
                    }} finally {{
                        preparing = false
                    }}
                }}
            }}

            override fun onIsLanguageAvailable(lang: String?, country: String?, variant: String?): Int {{
                return if (lang == "ori" || lang == "or") {{
                    android.speech.tts.TextToSpeech.LANG_AVAILABLE
                }} else {{
                    android.speech.tts.TextToSpeech.LANG_NOT_SUPPORTED
                }}
            }}

            override fun onGetLanguage(): Array<String> = arrayOf("ori", "IND", "")

            override fun onLoadLanguage(lang: String?, country: String?, variant: String?): Int {{
                return onIsLanguageAvailable(lang, country, variant)
            }}

            override fun onStop() {{ /* no-op for now */ }}

            override fun onSynthesizeText(request: SynthesisRequest?, callback: SynthesisCallback?) {{
                if (request == null || callback == null) return

                if (!ready) {{
                    // Kick off (or continue) preparing in the background, but
                    // don't make this request wait forever.
                    prepareInBackground()
                    callback.error()
                    return
                }}

                val sampleRate = 22050 // must match model.onnx.json sample_rate
                callback.start(sampleRate, AudioFormatCompat.ENCODING_PCM_16BIT, 1)

                val samples = bridge.synthesize(request.charSequenceText.toString())
                val pcm16 = ShortArray(samples.size)
                for (i in samples.indices) {{
                    val v = (samples[i] * 32767.0f).toInt().coerceIn(-32768, 32767)
                    pcm16[i] = v.toShort()
                }}
                val bytes = ByteArray(pcm16.size * 2)
                for (i in pcm16.indices) {{
                    bytes[i * 2] = (pcm16[i].toInt() and 0xFF).toByte()
                    bytes[i * 2 + 1] = ((pcm16[i].toInt() shr 8) and 0xFF).toByte()
                }}
                val maxBuf = callback.maxBufferSize
                var offset = 0
                while (offset < bytes.size) {{
                    val chunk = minOf(maxBuf, bytes.size - offset)
                    callback.audioAvailable(bytes, offset, chunk)
                    offset += chunk
                }}
                callback.done()
            }}

            override fun onDestroy() {{
                bridge.release()
                super.onDestroy()
            }}
        }}

        private object AudioFormatCompat {{
            const val ENCODING_PCM_16BIT = android.media.AudioFormat.ENCODING_PCM_16BIT
        }}
    """)


# ---------------------------------------------------------------------------
# Step 4 — wire in piper source + espeak-ng-data as native build inputs
# ---------------------------------------------------------------------------

def link_piper_into_project(piper_src: Path):
    log("Linking piper1-gpl source into the native build tree")
    cpp_dir = PROJECT_DIR / "app" / "src" / "main" / "cpp"
    dest = cpp_dir / "piper_src"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(piper_src, dest)

    # Bundle espeak-ng-data as an asset (it's data files, not code — gets
    # extracted to internal storage at first run by AssetExtractor.kt)
    espeak_data_src = None
    for candidate in piper_src.rglob("espeak-ng-data"):
        if candidate.is_dir():
            espeak_data_src = candidate
            break

    assets_dir = PROJECT_DIR / "app" / "src" / "main" / "assets"
    if espeak_data_src:
        shutil.copytree(espeak_data_src, assets_dir / "espeak-ng-data", dirs_exist_ok=True)
        print(f"Bundled espeak-ng-data from {espeak_data_src}")
    else:
        print(
            "WARNING: espeak-ng-data not found inside piper1-gpl checkout. "
            "Download it manually from "
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/espeak-ng-data.tar.bz2 "
            "and place it at app/src/main/assets/espeak-ng-data/ before building."
        )


# ---------------------------------------------------------------------------
# Step 5 — run the Gradle build
# ---------------------------------------------------------------------------

def run_gradle_build(ndk_home: str):
    log("Bootstrapping Gradle wrapper")
    env = os.environ.copy()
    env["ANDROID_NDK_HOME"] = ndk_home

    # gradle wrapper isn't generated yet — use system gradle (available
    # on GitHub Actions runners / typical dev machines) to create it,
    # then use the wrapper from here on for reproducibility.
    run(["gradle", "wrapper", "--gradle-version", "8.7"], cwd=PROJECT_DIR, env=env)

    log("Building release APK (this can take 10-20 min the first time)")
    gradlew = PROJECT_DIR / "gradlew"
    run(["chmod", "+x", str(gradlew)])
    run([str(gradlew), "assembleRelease", "--no-daemon", "--stacktrace"], cwd=PROJECT_DIR, env=env)


def collect_output():
    log("Collecting APK")
    apk_dir = PROJECT_DIR / "app" / "build" / "outputs" / "apk" / "release"
    apks = list(apk_dir.glob("*.apk"))
    if not apks:
        sys.exit(f"No APK found in {apk_dir} — build likely failed earlier.")
    OUTPUT_DIR.mkdir(exist_ok=True)
    for apk in apks:
        dest = OUTPUT_DIR / "odia-tts.apk"
        shutil.copy(apk, dest)
        print(f"APK ready at: {dest}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ndk_home = check_inputs()
    piper_src = fetch_piper_source()
    onnxruntime_dir = fetch_onnxruntime()
    generate_gradle_project(onnxruntime_dir)
    link_piper_into_project(piper_src)
    run_gradle_build(ndk_home)
    collect_output()
    log("Done.")


if __name__ == "__main__":
    main()
