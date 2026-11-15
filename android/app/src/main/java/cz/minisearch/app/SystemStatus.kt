package cz.minisearch.app

import android.app.ActivityManager
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.BatteryManager
import android.os.Build

/**
 * Real device battery and memory facts for the ``auto`` embeddings mode.
 *
 * On Android an app cannot reliably read ``/sys/class/power_supply`` (SELinux
 * denies it), so the Python backend's sysfs probe returns nothing there. These
 * static methods are the honest source: they use the platform [BatteryManager]
 * and [ActivityManager]. Every method returns ``null`` when the value is
 * genuinely unknown, so Python can distinguish "unknown" from "0 %" and never
 * mistake a missing reading for an empty battery.
 *
 * Python reaches them through ``jclass("cz.minisearch.app.SystemStatus")`` (see
 * ``app_combined._android_battery_status`` / ``_android_memory_mb``).
 */
object SystemStatus {

    @Volatile
    private var appContext: Context? = null

    fun setContext(context: Context) {
        appContext = context.applicationContext
    }

    /** Battery charge percent, or ``null`` when the platform cannot say. */
    @JvmStatic
    fun batteryPercent(): Int? {
        val ctx = appContext ?: return null
        return try {
            val intent: Intent? = ctx.registerReceiver(
                null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
            val level = intent?.getIntExtra(BatteryManager.EXTRA_LEVEL, -1) ?: -1
            val scale = intent?.getIntExtra(BatteryManager.EXTRA_SCALE, -1) ?: -1
            if (level < 0 || scale <= 0) null else (level * 100) / scale
        } catch (t: Throwable) {
            null
        }
    }

    /**
     * Whether the device is on external power, or ``null`` when unknown.
     *
     * ``EXTRA_PLUGGED`` is 0 for battery, so it must be read as an int and
     * compared, never treated as a boolean.
     */
    @JvmStatic
    fun isCharging(): Boolean? {
        val ctx = appContext ?: return null
        return try {
            val intent: Intent? = ctx.registerReceiver(
                null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
            val status = intent?.getIntExtra(BatteryManager.EXTRA_STATUS, -1) ?: -1
            when (status) {
                BatteryManager.BATTERY_STATUS_CHARGING,
                BatteryManager.BATTERY_STATUS_FULL -> true
                BatteryManager.BATTERY_STATUS_DISCHARGING,
                BatteryManager.BATTERY_STATUS_NOT_CHARGING -> false
                else -> null
            }
        } catch (t: Throwable) {
            null
        }
    }

    /** Available RAM in MB from [ActivityManager.MemoryInfo], or ``null``. */
    @JvmStatic
    fun availableMemoryMb(): Double? {
        val ctx = appContext ?: return null
        return try {
            val am = ctx.getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager
            val info = ActivityManager.MemoryInfo()
            am.getMemoryInfo(info)
            info.availMem.toDouble() / (1024.0 * 1024.0)
        } catch (t: Throwable) {
            null
        }
    }

    /** Low-RAM device flag, used to prefer the small hash fallback. */
    @JvmStatic
    fun isLowRamDevice(): Boolean? {
        val ctx = appContext ?: return null
        return try {
            val am = ctx.getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.KITKAT) am.isLowRamDevice else null
        } catch (t: Throwable) {
            null
        }
    }
}
