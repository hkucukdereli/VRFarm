// gyroscope_nano.ino — VRFarm Gyroscope (Nano) firmware
//
// Reads a TDK ICM-42670-P 6-axis IMU over the Nano's I2C and streams raw int16
// samples as CSV over USB serial at ~100 Hz:
//
//     ax,ay,az,gx,gy,gz\n
//
// Nothing else is ever printed after setup (the Pi-side parser skips any
// non-CSV line, so the WHO_AM_I banner is harmless).
//
// WIRING (Nano <-> ICM-42670-P breakout):
//   A4 (SDA) -> SDA      A5 (SCL) -> SCL
//   3V3      -> VCC      GND      -> GND
//   !! The ICM-42670 is NOT 5 V tolerant — power it from 3V3. If your breakout
//   has no level shifter, use 3V3 pull-ups on SDA/SCL as well.
//
// FLASH:  Arduino IDE (board: Arduino Nano — FT232 clones usually need
//         "ATmega328P (Old Bootloader)"), or:
//   arduino-cli compile --fqbn arduino:avr:nano:cpu=atmega328old arduino/gyroscope_nano
//   arduino-cli upload  --fqbn arduino:avr:nano:cpu=atmega328old -p /dev/ttyUSB0 arduino/gyroscope_nano
//
// Registers/scales mirror devices/icm42670.py: ±16 g (2048 LSB/g),
// ±2000 dps (16.4 LSB/dps), 100 Hz ODR.

#include <Wire.h>

const uint8_t ADDR = 0x68;
const uint8_t REG_WHO_AM_I = 0x75;      // reads 0x67
const uint8_t REG_PWR_MGMT0 = 0x1F;     // 0x0F = gyro+accel low-noise
const uint8_t REG_GYRO_CONFIG0 = 0x20;  // 0x09 = ±2000 dps @ 100 Hz
const uint8_t REG_ACCEL_CONFIG0 = 0x21; // 0x09 = ±16 g    @ 100 Hz
const uint8_t REG_ACCEL_DATA_X1 = 0x0B; // 12-byte burst AX..GZ (big-endian)

const uint32_t PERIOD_MS = 10;          // 100 Hz output
uint32_t lastTick = 0;

void writeReg(uint8_t reg, uint8_t val) {
  Wire.beginTransmission(ADDR);
  Wire.write(reg);
  Wire.write(val);
  Wire.endTransmission();
}

uint8_t readReg(uint8_t reg) {
  Wire.beginTransmission(ADDR);
  Wire.write(reg);
  Wire.endTransmission(false);
  Wire.requestFrom(ADDR, (uint8_t)1);
  return Wire.read();
}

void setup() {
  Serial.begin(115200);
  Wire.begin();
  Wire.setClock(400000);                // fast mode: 12-byte burst @ 100 Hz is easy

  uint8_t who = readReg(REG_WHO_AM_I);
  Serial.print("WHO_AM_I: 0x");
  Serial.println(who, HEX);             // banner — the Pi parser skips it
  if (who != 0x67) {
    // Wrong/absent sensor: blink forever instead of streaming garbage.
    pinMode(LED_BUILTIN, OUTPUT);
    while (true) {
      digitalWrite(LED_BUILTIN, HIGH); delay(150);
      digitalWrite(LED_BUILTIN, LOW);  delay(150);
    }
  }

  writeReg(REG_PWR_MGMT0, 0x0F);        // gyro + accel low-noise
  writeReg(REG_GYRO_CONFIG0, 0x09);     // ±2000 dps, 100 Hz
  writeReg(REG_ACCEL_CONFIG0, 0x09);    // ±16 g, 100 Hz
  delay(50);                            // sensor spin-up
}

void loop() {
  uint32_t now = millis();
  if (now - lastTick < PERIOD_MS) return;
  lastTick = now;

  // 12-byte burst: AX1 AX0 AY1 AY0 AZ1 AZ0 GX1 GX0 GY1 GY0 GZ1 GZ0
  Wire.beginTransmission(ADDR);
  Wire.write(REG_ACCEL_DATA_X1);
  Wire.endTransmission(false);
  Wire.requestFrom(ADDR, (uint8_t)12);
  if (Wire.available() < 12) return;    // dropped transfer — skip this tick

  int16_t v[6];
  for (uint8_t i = 0; i < 6; i++) {
    uint8_t hi = Wire.read();
    uint8_t lo = Wire.read();
    v[i] = (int16_t)(((uint16_t)hi << 8) | lo);
  }

  // ax,ay,az,gx,gy,gz  (~40 bytes/line * 100 Hz = 4 KB/s << 115200 baud)
  for (uint8_t i = 0; i < 6; i++) {
    Serial.print(v[i]);
    Serial.print(i < 5 ? ',' : '\n');
  }
}
