/*
 * photodiode_sync_v2_2.ino  —  Teensy 4.x photodiode sync-pulse detector   (VERSION v2_2)
 *
 * v2_2 replaces v2_1's 100 Hz bare-V stream with PER-PULSE amplitude reports. The old
 * stream sampled at random phase while the optical pulses are ~1-2 ms wide, so most
 * samples landed in the dark between pulses and percentile monitoring on the Pi was
 * aliased garbage (bench 2026-08-22: p99 varied 0.12-2.7 V across identical runs).
 * The detection loop samples at full rate and sees every sample of every pulse, so the
 * amplitude statistics are computed HERE and shipped as one line per accepted pulse:
 *
 *     P <median_V> <peak_V> <baseline_V> <width_us>
 *
 *   median_V   robust pulse level — the "is the signal too low/too high" number
 *   peak_V     clipping check: the pin saturates at 3.3 V; flag peaks >= ~3.0 V
 *   baseline_V floor-follower level at pulse start; median-baseline = true pulse height
 *   width_us   time above threshold — a real optical sub-pulse is ~1-1.5 ms; mains-hum
 *              crossings are ~10 ms (bench-proven fault discriminator)
 *
 * Plus one idle line per second (covers amp-off / ground-fault / ambient-light faults,
 * which live OUTSIDE pulses — bench-proven three times):
 *
 *     B <floor_V> <ceil_V>
 *
 * The Pi-side rules this enables: peak_V >= 3.0 -> "too high / clipping" safety warning;
 * median_V - baseline_V small (approaching MIN_PULSE_V) -> "too low" warning; no P lines
 * while flashes are commanded -> optical path dead; P lines with width ~10 ms -> hum.
 * NOTE: the v2_1/main-branch parser (devices/photodiode.py float(first-field)) skips
 * tagged lines silently — update the parser alongside deploying this firmware.
 *
 * Detection logic (adaptive thresholds, steady filter, hold-off, output pulse) is
 * UNCHANGED from v2_1. DEBUG mode (4-trace Serial Plotter) is unchanged.
 *
 * ── ADAPTIVE THRESHOLDS ──────────────────────────────────────────────────────
 * The photodiode baseline drifts. With ADAPTIVE=1 the two Schmitt thresholds RIDE on a
 * live estimate of the signal:
 *   baseline follower : asymmetric 1-pole filter chasing the FLOOR fast, rising slowly.
 *   peak follower     : the opposite — jumps to a new pulse TIP fast, relaxes slowly.
 *   thresholds        : a fixed FRACTION of the baseline->peak span (START_FRAC/END_FRAC).
 *   MIN_PULSE guard   : a real pulse must rise >= MIN_PULSE_V above baseline; pins the
 *                       START threshold so it can't sink into noise when flashes pause.
 *   baseline freeze   : pulse samples are not fed to the baseline follower.
 *
 * ⚠️  HARDWARE: Teensy 4 analog pins are 3.3V MAX and NOT 5V-tolerant. Divide/clamp the
 *     photodiode below 3.3V before PD_PIN. The TTL output is Teensy pin 16 (OUT_PIN),
 *     wired to the leader Pi's GPIO16 — scope-verified on the rig 2026-08-23 (the old
 *     docs said Teensy pin 1; the physical wire is on pin 16).
 */

// ── Pins ─────────────────────────────────────────────────────────────────────
const int PD_PIN  = A1;   // photodiode analog input  (MUST be <= 3.3 V at the pin)
const int OUT_PIN = 16;    // TEENSY pin 16: square-pulse output -> leader Pi GPIO16 (idles LOW)
const int LED_PIN = 13;   // onboard LED mirrors the output — DEBUG builds only

// ── ADC / scaling ────────────────────────────────────────────────────────────
const int   ADC_BITS = 12;     // 12-bit -> 0..4095
const float ADC_VREF = 3.3f;   // Teensy 4 analog reference (volts, fixed)
const float DIVIDER  = 1.0f;   // input scale-down ratio (reported voltages are pin_V * DIVIDER)

// ── Detection: adaptive vs fixed ─────────────────────────────────────────────
const bool  ACTIVE_HIGH = true;
const bool  ADAPTIVE    = true;

// Fixed thresholds — used ONLY when ADAPTIVE == false.
const float THRESHOLD_HI_V = 1.1f;
const float THRESHOLD_LO_V = 0.3f;

// ── Adaptive tracker tuning ──────────────────────────────────────────────────
const float BASELINE_FALL_TC_S = 0.05f;
const float BASELINE_RISE_TC_S = 4.0f;
const float PEAK_RISE_TC_S     = 0.01f;
const float PEAK_FALL_TC_S     = 2.0f;
const float START_FRAC  = 0.60f;
const float END_FRAC    = 0.30f;
const float MIN_PULSE_V = 0.30f;

// ── Filters ──────────────────────────────────────────────────────────────────
const uint32_t STEADY_US  = 150;
// One TTL edge per COMMANDED FLASH, not per DLP colour sub-pulse.
//
// Measured on the rig 2026-08-24 (DEBUG capture, 58k samples at 4.8 kHz, every_n=5):
// one sync-square frame produces **4 optical sub-pulses spaced 4.34 ms** (230 Hz red
// segment rate), so the burst spans ~14 ms; flashes repeat every 87.0 ms (= 5/57.46).
// HOLDOFF_US was 5000 — i.e. INSIDE that 4.34 ms train — so it accepted sub-pulses 1
// and 3 and dropped 2 and 4: exactly 2 edges per flash, by accident of 5 ms vs 4.34 ms.
//
// That mattered because devices/photodiode.py's init verify is "missing-only"
// (detected >= emitted - 1). At 2 edges per flash, HALF the flashes could fail and the
// count still passed. At 1:1 a single missed flash is visible.
//
// Constraint: sub-pulse train (<=4 x 4.34 + 1.2 ~= 18.6 ms) < HOLDOFF < flash period
// (every_n / 57.46). 30 ms gives 1.6x over the train and 2.9x under the 87 ms period.
// ⚠️ If photodiode_sync_every_n drops below 3 (52 ms) this must come down with it.
const uint32_t HOLDOFF_US = 30000;

// ── Output pulse to the RPi ──────────────────────────────────────────────────
const uint32_t OUT_PULSE_US = 5000;

// ── Debug ────────────────────────────────────────────────────────────────────
#define DEBUG 0                       // 1 = 4-trace Serial Plotter stream; 0 = production (P/B lines)
const uint32_t DEBUG_PERIOD_US = 200;

// ── v2_2 reporting ───────────────────────────────────────────────────────────
const uint32_t BASELINE_PERIOD_US = 1000000;  // "B <floor_V> <ceil_V>" once per second
const int      PULSE_BUF_MAX      = 256;      // samples kept for the median (a ~1.3 ms pulse
                                              // at the ~15-20 kHz loop rate is ~20-30 samples;
                                              // a 10 ms hum crossing still fits)

// ── State ────────────────────────────────────────────────────────────────────
int      hiCounts, loCounts;
float    floorCounts = 0;
float    ceilCounts  = 0;
uint32_t lastSampleUs = 0;
bool     hyst         = false;
bool     steadyActive = false;
uint32_t hystChangeUs = 0;
uint32_t lastAcceptUs = 0;
bool     outActive    = false;
uint32_t outStartUs   = 0;
uint32_t lastDebugUs  = 0;
uint32_t lastBaselineUs = 0;   // v2_2: gates the 1 Hz baseline line
uint32_t pulseCount   = 0;

// v2_2 per-pulse statistics (collected while an ACCEPTED pulse is active)
bool     pulseTracking   = false;   // between accept and steady-level fall
uint32_t pulseStartUs    = 0;
float    pulseBaseline   = 0;       // floor follower at accept time (ADC counts)
int      pulsePeak       = 0;       // max sample during the pulse
uint16_t pulseBufN       = 0;
int16_t  pulseBuf[PULSE_BUF_MAX];   // samples for the median

static inline float countsToVolts(int c)   { return c * ADC_VREF / ((1 << ADC_BITS) - 1); }
static inline float countsToVolts(float c) { return c * ADC_VREF / ((1 << ADC_BITS) - 1); }

// Median of the collected pulse samples (in-place insertion sort; <=256 values at pulse
// end on a 600 MHz part — negligible). Returns ADC counts.
static int pulseMedianCounts() {
  for (uint16_t i = 1; i < pulseBufN; i++) {
    int16_t v = pulseBuf[i];
    int j = i - 1;
    while (j >= 0 && pulseBuf[j] > v) { pulseBuf[j + 1] = pulseBuf[j]; j--; }
    pulseBuf[j + 1] = v;
  }
  return pulseBufN ? pulseBuf[pulseBufN / 2] : 0;
}

void setup() {
  pinMode(OUT_PIN, OUTPUT);  digitalWriteFast(OUT_PIN, LOW);
  pinMode(LED_PIN, OUTPUT);  digitalWriteFast(LED_PIN, LOW);
  analogReadResolution(ADC_BITS);
  analogReadAveraging(8);
  Serial.begin(115200);

  // Seed the followers near the real signal (~50 ms average). Best seeded in darkness.
  double acc = 0; const int N = 500;
  for (int i = 0; i < N; i++) { acc += analogRead(PD_PIN); delayMicroseconds(100); }
  floorCounts = ceilCounts = (float)(acc / N);

  hiCounts = (int)(THRESHOLD_HI_V / ADC_VREF * ((1 << ADC_BITS) - 1));
  loCounts = (int)(THRESHOLD_LO_V / ADC_VREF * ((1 << ADC_BITS) - 1));

  lastSampleUs = micros();
}

void loop() {
  const uint32_t now = micros();
  const int counts   = analogRead(PD_PIN);

  // ── Adaptive tracking (unchanged from v2_1) ─────────────────────────────────
  if (ADAPTIVE) {
    const float dt = (now - lastSampleUs) * 1e-6f;
    lastSampleUs = now;
    const float x = (float)counts;

    const float aFloorDown = dt / (BASELINE_FALL_TC_S + dt);
    const float aFloorUp   = dt / (BASELINE_RISE_TC_S + dt);
    const float aCeilUp    = dt / (PEAK_RISE_TC_S     + dt);
    const float aCeilDown  = dt / (PEAK_FALL_TC_S     + dt);

    const bool inPulse = hyst;
    if (ACTIVE_HIGH) {
      if (!inPulse || x < floorCounts)
        floorCounts += ((x < floorCounts) ? aFloorDown : aFloorUp) * (x - floorCounts);
      ceilCounts += ((x > ceilCounts) ? aCeilUp : aCeilDown) * (x - ceilCounts);
    } else {
      if (!inPulse || x > ceilCounts)
        ceilCounts += ((x > ceilCounts) ? aCeilUp : aCeilDown) * (x - ceilCounts);
      floorCounts += ((x < floorCounts) ? aFloorDown : aFloorUp) * (x - floorCounts);
    }

    float span = ceilCounts - floorCounts;
    if (span < 0) span = 0;
    const float minPulse = MIN_PULSE_V / ADC_VREF * ((1 << ADC_BITS) - 1);
    float startOff = START_FRAC * span;
    float endOff   = END_FRAC   * span;
    if (startOff < minPulse) { startOff = minPulse; endOff = minPulse * (END_FRAC / START_FRAC); }

    if (ACTIVE_HIGH) {
      hiCounts = (int)(floorCounts + startOff);
      loCounts = (int)(floorCounts + endOff);
    } else {
      hiCounts = (int)(ceilCounts - endOff);
      loCounts = (int)(ceilCounts - startOff);
    }
  }

  // ── Schmitt trigger (unchanged) ─────────────────────────────────────────────
  const bool prevHyst = hyst;
  if (ACTIVE_HIGH) {
    if (counts >= hiCounts)      hyst = true;
    else if (counts <= loCounts) hyst = false;
  } else {
    if (counts <= loCounts)      hyst = true;
    else if (counts >= hiCounts) hyst = false;
  }
  if (hyst != prevHyst) hystChangeUs = now;

  // v2_2: while an accepted pulse is active, collect its samples for the report.
  if (pulseTracking) {
    if (counts > pulsePeak) pulsePeak = counts;
    if (pulseBufN < PULSE_BUF_MAX) pulseBuf[pulseBufN++] = (int16_t)counts;
  }

  // ── Filter 1: steady ────────────────────────────────────────────────────────
  if (hyst != steadyActive && (now - hystChangeUs) >= STEADY_US) {
    const bool rising = (hyst && !steadyActive);
    steadyActive = hyst;

    // ── Filter 2: hold-off ────────────────────────────────────────────────────
    if (rising && (now - lastAcceptUs) >= HOLDOFF_US) {
      lastAcceptUs = now;
      pulseCount++;
      outActive = true; outStartUs = now;
      digitalWriteFast(OUT_PIN, HIGH);
      // v2_2: start collecting this pulse's statistics
      pulseTracking = true;
      pulseStartUs  = now;
      pulseBaseline = (ACTIVE_HIGH ? floorCounts : ceilCounts);
      pulsePeak     = counts;
      pulseBufN     = 0;
      pulseBuf[pulseBufN++] = (int16_t)counts;
#if DEBUG
      digitalWriteFast(LED_PIN, HIGH);
#endif
    }

    // v2_2: pulse ended (steady level fell) — emit the per-pulse report.
    if (!rising && pulseTracking) {
      pulseTracking = false;
#if !DEBUG
      const uint32_t width = now - pulseStartUs;
      Serial.print("P ");
      Serial.print(countsToVolts(pulseMedianCounts()) * DIVIDER, 3);  Serial.print(' ');
      Serial.print(countsToVolts(pulsePeak)           * DIVIDER, 3);  Serial.print(' ');
      Serial.print(countsToVolts(pulseBaseline)       * DIVIDER, 3);  Serial.print(' ');
      Serial.println(width);
#endif
    }
  }

  // Finish the output square pulse after OUT_PULSE_US.
  if (outActive && (now - outStartUs) >= OUT_PULSE_US) {
    outActive = false;
    digitalWriteFast(OUT_PIN, LOW);
#if DEBUG
    digitalWriteFast(LED_PIN, LOW);
#endif
  }

#if !DEBUG
  // ── v2_2: 1 Hz idle/baseline line — covers the out-of-pulse fault class
  //    (amp off, lifted ground, ambient light) without the old aliased 100 Hz stream.
  if (now - lastBaselineUs >= BASELINE_PERIOD_US) {
    lastBaselineUs = now;
    Serial.print("B ");
    Serial.print(countsToVolts(floorCounts) * DIVIDER, 3);  Serial.print(' ');
    Serial.println(countsToVolts(ceilCounts) * DIVIDER, 3);
  }
#endif

#if DEBUG
  // 4-trace Serial Plotter stream (unchanged from v2_1).
  if (now - lastDebugUs >= DEBUG_PERIOD_US) {
    lastDebugUs = now;
    Serial.print(countsToVolts(counts)   * DIVIDER, 3);  Serial.print(' ');
    Serial.print(countsToVolts(hiCounts) * DIVIDER, 3);  Serial.print(' ');
    Serial.print(countsToVolts(loCounts) * DIVIDER, 3);  Serial.print(' ');
    Serial.println(steadyActive ? (ADC_VREF * DIVIDER) : 0.0f, 3);
  }
#endif
}
