/*
 * teensy_photodiode_sync.ino  —  Teensy 4.x photodiode sync-pulse detector
 *
 * Reads the photodiode voltage on an analog pin, thresholds it, applies the SAME two
 * filters as the VRFarm setup UI, and emits a clean 3.3V square pulse to the RPi GPIO
 * on each detected sync pulse. Teensy's 3.3V logic drives the RPi input directly (no
 * level issues), fixing the "photodiode ~2V < Pi logic-high" problem.
 *
 *   Filter 1  STEADY / glitch  (= setup UI "Steady filter", glitch_ms 0.5 ms):
 *             the thresholded level must HOLD for STEADY_US before an edge counts.
 *             Drops spikes narrower than this; also delays every edge by this amount.
 *   Filter 2  HOLD-OFF          (= setup UI "Hold-off", debounce_ms 5 ms):
 *             after an accepted pulse, ignore further pulses for HOLDOFF_US.
 *             Kills the spike->plateau double-trigger and ringing.
 *
 * ── ADAPTIVE THRESHOLDS ──────────────────────────────────────────────────────
 * The photodiode baseline drifts (idles anywhere from ~1V to ~2V, always < 3V). With
 * ADAPTIVE=1 the two Schmitt thresholds are no longer fixed voltages — they RIDE on a
 * live estimate of the signal, so they self-adjust to whatever level the diode idles at
 * (and to whatever height the pulse reaches):
 *
 *   baseline follower : an asymmetric 1-pole filter that chases the signal's FLOOR fast
 *                       but rises SLOWLY, so brief upward pulses barely move it while real
 *                       baseline drift is still tracked.  (mirror for active-low)
 *   peak follower     : the opposite — jumps to a new pulse TIP fast, relaxes back slowly.
 *   thresholds        : placed a fixed FRACTION of the way from baseline to peak
 *                       (START_FRAC to begin a pulse, END_FRAC < START_FRAC to end it —
 *                       the gap is the hysteresis dead-band).
 *   MIN_PULSE guard   : a real pulse must rise at least MIN_PULSE_V above the baseline;
 *                       smaller excursions are ignored and the START threshold is pinned
 *                       here, so the thresholds can't sink into the noise when the flashes
 *                       pause (the peak decays but START stays put).  <-- the safety valve.
 *   baseline freeze   : while a pulse is active the elevated samples are NOT fed to the
 *                       baseline follower, so the pulse itself can't drag the baseline up.
 *
 * Set ADAPTIVE=0 to fall back to the original fixed THRESHOLD_HI_V / THRESHOLD_LO_V.
 *
 * ⚠️  HARDWARE: Teensy 4 analog pins are 3.3V MAX and NOT 5V-tolerant. Your photodiode
 *     swings >5V — you MUST divide/clamp the input below 3.3V before PD_PIN (e.g. a
 *     resistor divider, or a Schottky clamp to 3V3). Set DIVIDER below to your divider
 *     ratio so DEBUG prints the true photodiode voltage.
 *
 * TUNING (DEBUG=1 opens the Serial Plotter with:  1 = raw V,  2 = live START threshold,
 *     3 = live END threshold,  4 = detection marker.  The onboard LED also mirrors the
 *     output in DEBUG builds. Run the setup-UI photodiode "Test"):
 *       • Watch traces 2 & 3 sit a little above the idle level and track it as the baseline
 *         drifts. Each real flash (trace 1 spike) should punch above trace 2, then fall
 *         back below trace 3 → trace 4 pops to full-scale.
 *       • If the thresholds sit too high (flash never crosses trace 2): lower START_FRAC.
 *       • If they hug the noise and false-trigger while idle: raise MIN_PULSE_V (and/or
 *         START_FRAC). If they sag when flashes pause then snap back on the next flash,
 *         that's the MIN_PULSE floor working — raise MIN_PULSE_V if the sag dips too low.
 *       • Peak tracker slow to catch the tip: lower PEAK_RISE_TC_S. Noise spikes inflating
 *         it: raise PEAK_RISE_TC_S. Baseline chasing drift too slowly: lower BASELINE_RISE_TC_S.
 *     Then set DEBUG=0 and wire OUT_PIN to the RPi before porting.
 */

// ── Pins ─────────────────────────────────────────────────────────────────────
const int PD_PIN  = A1;   // photodiode analog input  (MUST be <= 3.3 V at the pin)
const int OUT_PIN = 1;    // digital square-pulse output -> RPi GPIO (idles LOW, pulses HIGH)
const int LED_PIN = 13;   // onboard LED mirrors the output — DEBUG builds only (quick visual check)

// ── ADC / scaling ────────────────────────────────────────────────────────────
const int   ADC_BITS = 12;     // 12-bit -> 0..4095
const float ADC_VREF = 3.3f;   // Teensy 4 analog reference (volts, fixed)
const float DIVIDER  = 1.0f;   // input scale-down ratio (e.g. 2.0 for a 2:1 divider).
                               // Detection uses the PIN voltage; DEBUG also prints pin_V * DIVIDER.

// ── Detection: adaptive vs fixed ─────────────────────────────────────────────
const bool  ACTIVE_HIGH = true;    // true: pulse = signal ABOVE baseline; false: BELOW
const bool  ADAPTIVE    = true;    // true: thresholds track the live baseline/peak (below);
                                   // false: use the fixed THRESHOLD_*_V pair (original behaviour).

// Fixed thresholds — used ONLY when ADAPTIVE == false.
const float THRESHOLD_HI_V = 1.2f;
const float THRESHOLD_LO_V = 0.6f;

// ── Adaptive tracker tuning (used when ADAPTIVE == true) ─────────────────────
// Time constants are in seconds. "fast" = small TC, "slow" = large TC. Values below assume
// a ~1.3 ms pulse at 60 Hz; adjust with the Serial Plotter (see TUNING above).
const float BASELINE_FALL_TC_S = 0.05f;  // baseline chases the FLOOR this fast (snappy).
const float BASELINE_RISE_TC_S = 4.0f;   // baseline creeps UP this slow. (With the pulse freeze
                                         // below this now only governs idle drift — shorten for
                                         // quicker recovery after a real baseline shift.)
const float PEAK_RISE_TC_S     = 0.01f;  // peak jumps to a new TIP this fast.
const float PEAK_FALL_TC_S     = 2.0f;   // peak relaxes back this slow -> survives missed pulses.
const float START_FRAC = 0.60f;          // begin a pulse at 60% of the baseline->peak span.
const float END_FRAC   = 0.30f;          // end it back at 30%   (END < START = hysteresis band).
const float MIN_PULSE_V = 0.20f;         // guard: a real pulse must rise at least this far above the
                                         // baseline. Smaller excursions are ignored, and this is the
                                         // floor (on the START offset) that stops the thresholds
                                         // collapsing into the noise when the flashes pause.

// ── Filters ──────────────────────────────────────────────────────────────────
const uint32_t STEADY_US  = 150;    // Filter 1: steady/glitch — MUST be << the pulse's time-above-
                                    // threshold (~1.2 ms). 150 µs drops fast spikes, passes the pulse.
const uint32_t HOLDOFF_US = 5000;   // Filter 2: hold-off — > the 1.5 ms pulse (kills falling-edge
                                    // ringing), << the inter-pulse interval (>=16.7 ms at 60 Hz).

// ── Output pulse to the RPi ──────────────────────────────────────────────────
const uint32_t OUT_PULSE_US = 5000; // width of the square pulse (µs). Must be >> the RPi's
                                    // glitch filter (0.5 ms) and << the inter-pulse interval.

// ── Debug ────────────────────────────────────────────────────────────────────
#define DEBUG 0                       // 1 = stream "V start end detect" (4 traces) + pulse the LED; 0 = silent (production)
const uint32_t DEBUG_PERIOD_US = 200;   // V print interval (µs); 200 = 5 kHz so a ~1.3 ms pulse shows ~6-7
                                        // points (Teensy USB ignores baud). Detection uses EVERY sample regardless.

// ── State ────────────────────────────────────────────────────────────────────
int      hiCounts, loCounts;     // hysteresis thresholds in ADC counts (LIVE when adaptive, else set in setup)
float    floorCounts = 0;        // asymmetric follower: signal FLOOR  (ADC counts, kept as float)
float    ceilCounts  = 0;        // asymmetric follower: signal CEILING (ADC counts, kept as float)
uint32_t lastSampleUs = 0;       // dt source for the followers
bool     hyst         = false;   // Schmitt-trigger output — instantaneous level
bool     steadyActive = false;   // level after the steady filter (== "pulse detected now")
uint32_t hystChangeUs = 0;       // when hyst last changed
uint32_t lastAcceptUs = 0;       // when we last accepted a pulse (for hold-off)
bool     outActive    = false;   // output square pulse currently HIGH
uint32_t outStartUs   = 0;
uint32_t lastDebugUs  = 0;
uint32_t pulseCount   = 0;

static inline float countsToVolts(int c)   { return c * ADC_VREF / ((1 << ADC_BITS) - 1); }
static inline float countsToVolts(float c) { return c * ADC_VREF / ((1 << ADC_BITS) - 1); }

void setup() {
  pinMode(OUT_PIN, OUTPUT);  digitalWriteFast(OUT_PIN, LOW);
  pinMode(LED_PIN, OUTPUT);  digitalWriteFast(LED_PIN, LOW);   // configured + parked low; only pulsed in DEBUG builds
  analogReadResolution(ADC_BITS);
  analogReadAveraging(8);                    // light averaging for a steadier threshold
  Serial.begin(115200);

  // Seed the followers near the real signal (~50 ms average) so they don't start at 0 and
  // false-trigger while converging. If flashes are already running the seed sits a hair high;
  // the floor follower settles it within BASELINE_FALL_TC_S. Best to seed before the flash test.
  double acc = 0; const int N = 500;
  for (int i = 0; i < N; i++) { acc += analogRead(PD_PIN); delayMicroseconds(100); }
  floorCounts = ceilCounts = (float)(acc / N);

  // Fixed-threshold fallback (used only when ADAPTIVE == false).
  hiCounts = (int)(THRESHOLD_HI_V / ADC_VREF * ((1 << ADC_BITS) - 1));
  loCounts = (int)(THRESHOLD_LO_V / ADC_VREF * ((1 << ADC_BITS) - 1));

  lastSampleUs = micros();
}

void loop() {
  const uint32_t now = micros();
  const int counts   = analogRead(PD_PIN);

  // ── Adaptive tracking: update the followers and recompute the LIVE thresholds ───────────────
  // Two asymmetric one-pole followers bracket the signal. alpha = dt/(TC+dt) turns each time
  // constant into a per-sample blend factor, self-correcting for the loop's actual sample rate.
  //   floorCounts : fast DOWN, slow UP   -> tracks the idle floor, ignores upward pulses.
  //   ceilCounts  : fast UP,   slow DOWN -> tracks the pulse tip, survives a few missed pulses.
  // Thresholds are then a fraction of the baseline->peak span, with the START offset floored at
  // MIN_PULSE_V. Skipped entirely when ADAPTIVE == false (fixed hiCounts/loCounts from setup()).
  if (ADAPTIVE) {
    const float dt = (now - lastSampleUs) * 1e-6f;   // seconds since last sample
    lastSampleUs = now;
    const float x = (float)counts;

    const float aFloorDown = dt / (BASELINE_FALL_TC_S + dt);
    const float aFloorUp   = dt / (BASELINE_RISE_TC_S + dt);
    const float aCeilUp    = dt / (PEAK_RISE_TC_S     + dt);
    const float aCeilDown  = dt / (PEAK_FALL_TC_S     + dt);

    // Freeze the BASELINE follower while a pulse is active (hyst high) so the elevated samples can't
    // drag it up. A sample that's beyond the baseline in the *idle* direction is still let through
    // (it can't be part of the pulse, and it keeps the estimate from sticking). The PEAK follower
    // always updates: mid-pulse is exactly when it should catch the tip. (hyst here is the previous
    // sample's state; the 1-sample lag at ~20 kHz is negligible.)
    const bool inPulse = hyst;
    if (ACTIVE_HIGH) {                                        // baseline = floor
      if (!inPulse || x < floorCounts)
        floorCounts += ((x < floorCounts) ? aFloorDown : aFloorUp) * (x - floorCounts);
      ceilCounts += ((x > ceilCounts) ? aCeilUp : aCeilDown) * (x - ceilCounts);
    } else {                                                  // baseline = ceiling
      if (!inPulse || x > ceilCounts)
        ceilCounts += ((x > ceilCounts) ? aCeilUp : aCeilDown) * (x - ceilCounts);
      floorCounts += ((x < floorCounts) ? aFloorDown : aFloorUp) * (x - floorCounts);
    }

    // Thresholds sit a fraction of the way baseline->peak, but the START offset is floored at
    // MIN_PULSE_V: a sub-0.2 V excursion (or a peak that has decayed because the flashes paused)
    // can never reach START, which is what keeps the thresholds from collapsing into the noise.
    // The END offset is scaled down with it so the hysteresis band keeps its shape.
    float span = ceilCounts - floorCounts;
    if (span < 0) span = 0;
    const float minPulse = MIN_PULSE_V / ADC_VREF * ((1 << ADC_BITS) - 1);
    float startOff = START_FRAC * span;
    float endOff   = END_FRAC   * span;
    if (startOff < minPulse) { startOff = minPulse; endOff = minPulse * (END_FRAC / START_FRAC); }

    if (ACTIVE_HIGH) {                                 // baseline = floor; pulse rides UP
      hiCounts = (int)(floorCounts + startOff);        // START (higher magnitude)
      loCounts = (int)(floorCounts + endOff);          // END
    } else {                                           // baseline = ceiling; pulse dips DOWN
      hiCounts = (int)(ceilCounts - endOff);           // END (shallow -> higher magnitude)
      loCounts = (int)(ceilCounts - startOff);         // START (deep -> lower magnitude)
    }
  }

  // ── Schmitt trigger (hysteresis comparator) -> instantaneous level ──────────────────────────
  // A plain single-threshold comparator chatters when a noisy/inflected signal sits near the
  // threshold: every notch that dips back across it looks like a fresh edge. A Schmitt trigger uses
  // TWO thresholds plus one bit of memory (`hyst`):
  //     - while INACTIVE it ignores everything until the signal crosses UP past hiCounts   -> active
  //     - while ACTIVE   it stays active until the signal falls DOWN past loCounts          -> inactive
  //     - in the dead band (loCounts < x < hiCounts) it HOLDS the previous state.
  // So the fast downward notches in the pulse plateau — which stay above loCounts — never flip the
  // state, and the whole pulse reads as one clean level. (hiCounts/loCounts are now the LIVE,
  // baseline-relative thresholds when ADAPTIVE, or the fixed ones otherwise — the logic is identical.)
  const bool prevHyst = hyst;
  if (ACTIVE_HIGH) {
    if (counts >= hiCounts)      hyst = true;    // rise past HI -> start
    else if (counts <= loCounts) hyst = false;   // fall past LO -> end   (else: hold, in dead band)
  } else {                                       // active-low: pulse is a downward excursion
    if (counts <= loCounts)      hyst = true;
    else if (counts >= hiCounts) hyst = false;
  }
  if (hyst != prevHyst) hystChangeUs = now;

  // ── Filter 1: steady — commit the Schmitt level only after it holds STEADY_US ──
  if (hyst != steadyActive && (now - hystChangeUs) >= STEADY_US) {
    const bool rising = (hyst && !steadyActive);   // edge INTO the active state
    steadyActive = hyst;

    // ── Filter 2: hold-off — accept a rising edge only outside the refractory window ──
    if (rising && (now - lastAcceptUs) >= HOLDOFF_US) {
      lastAcceptUs = now;
      pulseCount++;
      outActive = true; outStartUs = now;
      digitalWriteFast(OUT_PIN, HIGH);
#if DEBUG
      digitalWriteFast(LED_PIN, HIGH);   // onboard LED mirrors the output — DEBUG builds only
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

#if DEBUG
  // Four space-separated values per line -> four traces in the Arduino Serial Plotter:
  //   trace 1 = raw photodiode volts.
  //   trace 2 = live START threshold (crossed UP to begin a pulse).
  //   trace 3 = live END threshold   (crossed back DOWN to end it). Traces 2/3 straddle the pulse
  //             and, in ADAPTIVE mode, visibly track the baseline as it drifts.
  //   trace 4 = detection marker, pinned to full-scale while a pulse is accepted (after both filters).
  // If trace 1's flash never reaches trace 2 -> lower START_FRAC (adaptive) or THRESHOLD_HI_V (fixed).
  // If traces 2/3 hug the noise and trace 4 flickers while idle -> raise MIN_PULSE_V (adaptive).
  if (now - lastDebugUs >= DEBUG_PERIOD_US) {
    lastDebugUs = now;
    Serial.print(countsToVolts(counts)   * DIVIDER, 3);  Serial.print(' ');
    Serial.print(countsToVolts(hiCounts) * DIVIDER, 3);  Serial.print(' ');
    Serial.print(countsToVolts(loCounts) * DIVIDER, 3);  Serial.print(' ');
    Serial.println(steadyActive ? (ADC_VREF * DIVIDER) : 0.0f, 3);
  }
#endif
}
