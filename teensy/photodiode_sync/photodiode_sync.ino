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
 * ⚠️  HARDWARE: Teensy 4 analog pins are 3.3V MAX and NOT 5V-tolerant. Your photodiode
 *     swings >5V — you MUST divide/clamp the input below 3.3V before PD_PIN (e.g. a
 *     resistor divider, or a Schottky clamp to 3V3). Set DIVIDER below to your divider
 *     ratio so DEBUG prints the true photodiode voltage.
 *
 * TUNING (DEBUG=1 opens a 3-trace Serial Plotter: 1 = raw V, 2 = comparator `hyst`, 3 = detection.
 *     Run the setup-UI photodiode "Test" so the red square flashes):
 *       1. Read the baseline V, the pulse peak V, and how deep the downward notches dip (trace 1).
 *       2. Set ACTIVE_HIGH: true if the pulse is the HIGHER voltage, false if lower.
 *       3. Set THRESHOLD_HI_V between baseline and peak; THRESHOLD_LO_V just above baseline but
 *          BELOW the notch troughs (the HI..LO dead band is what swallows the notches).
 *       4. If trace 2 never rises -> HI is above your peak, lower it. If trace 2 rises but trace 3
 *          doesn't -> lower STEADY_US (pulse too brief) and/or LO (notches cutting it short).
 *     Then set DEBUG=0 and wire OUT_PIN to the RPi before porting.
 */

// ── Pins ─────────────────────────────────────────────────────────────────────
const int PD_PIN  = A1;   // photodiode analog input  (MUST be <= 3.3 V at the pin)
const int OUT_PIN = 1;    // digital square-pulse output -> RPi GPIO (idles LOW, pulses HIGH)
const int LED_PIN = 13;   // onboard LED mirrors the output (quick visual check)

// ── ADC / scaling ────────────────────────────────────────────────────────────
const int   ADC_BITS = 12;     // 12-bit -> 0..4095
const float ADC_VREF = 3.3f;   // Teensy 4 analog reference (volts, fixed)
const float DIVIDER  = 1.0f;   // input scale-down ratio (e.g. 2.0 for a 2:1 divider).
                               // Detection uses the PIN voltage; DEBUG also prints pin_V * DIVIDER.

// ── Detection ────────────────────────────────────────────────────────────────
const bool  ACTIVE_HIGH = true;    // true: pulse = signal ABOVE threshold; false: BELOW
// Hysteresis (Schmitt) thresholds — cross UP through HI to START a pulse, must fall below LO to END it.
// The HI..LO band swallows the fast downward notches in the pulse plateau (as long as they don't dip
// below LO). Tune both from the two-trace plot: HI between baseline and peak; LO just above baseline
// but below the notch troughs. (baseline≈0, peak≈1.85 V -> HI 0.9, LO 0.5. If your baseline sits high —
// the scope Mean read 1.18 V — raise both: HI just under the peak, LO just over the baseline.)
const float THRESHOLD_HI_V = 0.8f;
const float THRESHOLD_LO_V = 0.2f;

// ── Filters ──────────────────────────────────────────────────────────────────
const uint32_t STEADY_US  = 150;    // Filter 1: steady/glitch — MUST be << the pulse's time-above-
                                    // threshold (~1.2 ms). 150 µs drops fast spikes, passes the pulse.
const uint32_t HOLDOFF_US = 5000;   // Filter 2: hold-off — > the 1.5 ms pulse (kills falling-edge
                                    // ringing), << the inter-pulse interval (>=16.7 ms at 60 Hz).

// ── Output pulse to the RPi ──────────────────────────────────────────────────
const uint32_t OUT_PULSE_US = 5000; // width of the square pulse (µs). Must be >> the RPi's
                                    // glitch filter (0.5 ms) and << the inter-pulse interval.

// ── Debug ────────────────────────────────────────────────────────────────────
#define DEBUG 0                       // 1 = stream "V hyst detect" (3 traces) for the Serial Plotter (tuning); 0 = silent (production)
const uint32_t DEBUG_PERIOD_US = 200;   // V print interval (µs); 200 = 5 kHz so a ~1.3 ms pulse shows ~6-7
                                        // points (Teensy USB ignores baud). Detection uses EVERY sample regardless.

// ── State ────────────────────────────────────────────────────────────────────
int      hiCounts, loCounts;     // hysteresis thresholds in ADC counts (computed in setup)
bool     hyst         = false;   // Schmitt-trigger output — instantaneous level
bool     steadyActive = false;   // level after the steady filter (== "pulse detected now")
uint32_t hystChangeUs = 0;       // when hyst last changed
uint32_t lastAcceptUs = 0;       // when we last accepted a pulse (for hold-off)
bool     outActive    = false;   // output square pulse currently HIGH
uint32_t outStartUs   = 0;
uint32_t lastDebugUs  = 0;
uint32_t pulseCount   = 0;

static inline float countsToVolts(int c) { return c * ADC_VREF / ((1 << ADC_BITS) - 1); }

void setup() {
  pinMode(OUT_PIN, OUTPUT);  digitalWriteFast(OUT_PIN, LOW);
  pinMode(LED_PIN, OUTPUT);  digitalWriteFast(LED_PIN, LOW);
  analogReadResolution(ADC_BITS);
  analogReadAveraging(8);                    // light averaging for a steadier threshold
  hiCounts = (int)(THRESHOLD_HI_V / ADC_VREF * ((1 << ADC_BITS) - 1));
  loCounts = (int)(THRESHOLD_LO_V / ADC_VREF * ((1 << ADC_BITS) - 1));
  Serial.begin(115200);
}

void loop() {
  const uint32_t now = micros();
  const int counts   = analogRead(PD_PIN);

  // ── Schmitt trigger (hysteresis comparator) -> instantaneous level ──────────────────────────
  // A plain single-threshold comparator chatters when a noisy/inflected signal sits near the
  // threshold: every notch that dips back across it looks like a fresh edge. A Schmitt trigger uses
  // TWO thresholds plus one bit of memory (`hyst`):
  //     - while INACTIVE it ignores everything until the signal crosses UP past hiCounts   -> active
  //     - while ACTIVE   it stays active until the signal falls DOWN past loCounts          -> inactive
  //     - in the dead band (loCounts < x < hiCounts) it HOLDS the previous state.
  // So the fast downward notches in the pulse plateau — which stay above loCounts — never flip the
  // state, and the whole pulse reads as one clean level. Implementation: compare against hiCounts or
  // loCounts depending on the current state; assign nothing in the dead band so `hyst` is retained.
  // (ACTIVE_HIGH just mirrors the two comparisons for a downward-going pulse.)
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
      digitalWriteFast(LED_PIN, HIGH);
    }
  }

  // Finish the output square pulse after OUT_PULSE_US.
  if (outActive && (now - outStartUs) >= OUT_PULSE_US) {
    outActive = false;
    digitalWriteFast(OUT_PIN, LOW);
    digitalWriteFast(LED_PIN, LOW);
  }

#if DEBUG
  // Three space-separated values per line -> three traces in the Arduino Serial Plotter:
  //   trace 1 = raw photodiode volts.
  //   trace 2 = Schmitt-trigger output `hyst` (1.0 while the signal is above THRESHOLD_HI_V). This
  //             answers "did the comparator fire at all?" If it stays flat, THRESHOLD_HI_V is ABOVE
  //             your pulse peak -> lower it (read the peak off trace 1).
  //   trace 3 = final detection `steadyActive` (2.0) after the steady + hold-off filters. If trace 2
  //             pulses but trace 3 does NOT, the pulse isn't staying above threshold for STEADY_US
  //             -> lower STEADY_US (and/or lower THRESHOLD_LO_V so the notches don't cut it short).
  if (now - lastDebugUs >= DEBUG_PERIOD_US) {
    lastDebugUs = now;
    Serial.print(countsToVolts(counts) * DIVIDER, 3);
    Serial.print(' ');
    Serial.print(hyst ? 1.0f : 0.0f, 3);
    Serial.print(' ');
    Serial.println(steadyActive ? 2.0f : 0.0f, 3);
  }
#endif
}
