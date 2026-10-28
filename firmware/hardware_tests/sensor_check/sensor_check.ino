// Sensor check: prints both brake Hall sensor voltages and the MPU6050's
// acceleration and rotation ten times a second.
//
// The board has only one 3V3 pin, so each Hall sensor is powered from two
// GPIOs: one driven LOW as its ground and one driven HIGH as its supply.
#include <Wire.h>
#include "driver/gpio.h"

struct HallSensor {
  const char *name;
  int vccPin, gndPin, outPin;
};

const HallSensor HALLS[] = {
  {"brakeA", 32, 33, 34},
  {"brakeB", 25, 26, 35},
};

const int SDA_PIN = 21;
const int SCL_PIN = 22;
const uint8_t MPU_ADDR = 0x68;  // AD0 pin left unconnected

bool mpuReady = false;

bool writeReg(uint8_t reg, uint8_t val) {
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(reg);
  Wire.write(val);
  return Wire.endTransmission() == 0;
}

bool readRegs(uint8_t reg, uint8_t *buf, int n) {
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom((int)MPU_ADDR, n) != n) return false;
  for (int i = 0; i < n; i++) buf[i] = Wire.read();
  return true;
}

bool initMpu() {
  uint8_t who;
  if (!readRegs(0x75, &who, 1)) return false;
  return writeReg(0x6B, 0x01)   // wake up, clock from gyro PLL
      && writeReg(0x1A, 0x03)   // digital low-pass filter ~44 Hz
      && writeReg(0x1B, 0x08)   // gyro range +-500 deg/s
      && writeReg(0x1C, 0x00);  // accel range +-2 g
}

// Fills accel (g) and gyro (deg/s); returns false if the sensor is missing.
bool readMpu(float accel[3], float gyro[3]) {
  if (!mpuReady && !(mpuReady = initMpu())) return false;
  // A power glitch resets the MPU6050 into sleep mode; wake it again.
  uint8_t pwr;
  if (!readRegs(0x6B, &pwr, 1) || pwr != 0x01) {
    mpuReady = false;
    return false;
  }
  uint8_t b[14];
  if (!readRegs(0x3B, b, 14)) {
    mpuReady = false;
    return false;
  }
  // Not named "word": Arduino.h defines a word() macro that would replace it.
  auto be16 = [&](int i) { return (int16_t)(b[i] << 8 | b[i + 1]); };
  for (int k = 0; k < 3; k++) {
    accel[k] = be16(2 * k) / 16384.0;
    gyro[k] = be16(8 + 2 * k) / 65.5;
  }
  return true;
}

int readHallMv(int pin) {
  uint32_t sum = 0;
  for (int i = 0; i < 16; i++) sum += analogReadMilliVolts(pin);
  return sum / 16;
}

void setup() {
  Serial.begin(115200);
  for (const HallSensor &h : HALLS) {
    pinMode(h.gndPin, OUTPUT);
    gpio_set_drive_capability((gpio_num_t)h.gndPin, GPIO_DRIVE_CAP_3);
    digitalWrite(h.gndPin, LOW);
    pinMode(h.vccPin, OUTPUT);
    gpio_set_drive_capability((gpio_num_t)h.vccPin, GPIO_DRIVE_CAP_3);
    digitalWrite(h.vccPin, HIGH);
  }
  Wire.begin(SDA_PIN, SCL_PIN, 400000);
}

void loop() {
  for (const HallSensor &h : HALLS) Serial.printf("%s %4d mV  ", h.name, readHallMv(h.outPin));
  float a[3], g[3];
  if (readMpu(a, g)) {
    Serial.printf("| accel g: %+.2f %+.2f %+.2f  gyro deg/s: %+6.1f %+6.1f %+6.1f\n",
                  a[0], a[1], a[2], g[0], g[1], g[2]);
  } else {
    Serial.println("| MPU6050 not responding");
  }
  delay(100);
}
