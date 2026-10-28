// Bike controller firmware: streams raw MPU6050 and brake Hall sensor readings
// to the PC 200 times a second. All processing happens in the PC program.
//
// Each line is
//   D,<seq>,<t_us>,<mpu_ok>,<ax>,<ay>,<az>,<gx>,<gy>,<gz>,<brakeA_mV>,<brakeB_mV>,<zwift>*<xor>
// where accel/gyro are raw counts (+-2 g, +-500 deg/s), <zwift> is the Zwift
// steering link state (see ZwiftState) and <xor> is the XOR of every character
// before '*', as two hex digits.
//
// Zwift steering: the PC sends "Z,<degrees>\n" lines (positive = right). The
// first one starts Bluetooth, and the board then appears to Zwift as an Elite
// Sterzo Smart called STERZO. Protocol as in github.com/stefaandesmet2003/ESP32Sterzo
// (12/2024) and github.com/Felixrising/STERZStick.
//
// The onboard LED is solid while the MPU6050 responds and blinks when it doesn't.
#include <BLE2902.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <Wire.h>
#include "driver/gpio.h"

const uint32_t BAUD = 921600;
const uint32_t PERIOD_US = 5000;  // 200 Hz

const int LED_PIN = 2;
const int SDA_PIN = 21;
const int SCL_PIN = 22;
const uint8_t MPU_ADDR = 0x68;  // AD0 pin left unconnected

// The board has only one 3V3 pin, so each Hall sensor is powered from two
// GPIOs: one driven LOW as its ground and one driven HIGH as its supply.
struct HallSensor {
  int vccPin, gndPin, outPin;
};
const HallSensor HALLS[] = {
  {32, 33, 34},  // brake A
  {25, 26, 35},  // brake B
};

#define STERZO_SERVICE_UUID "347b0001-7635-408b-8918-8ff3949ce592"
#define CHAR14_UUID "347b0014-7635-408b-8918-8ff3949ce592"  // unused by Zwift, but present on a real Sterzo
#define CHAR30_UUID "347b0030-7635-408b-8918-8ff3949ce592"  // steering angle, float32 degrees
#define CHAR31_UUID "347b0031-7635-408b-8918-8ff3949ce592"  // Zwift -> Sterzo handshake
#define CHAR32_UUID "347b0032-7635-408b-8918-8ff3949ce592"  // Sterzo -> Zwift handshake
const float ZWIFT_MAX_DEG = 40.0;
const uint32_t ZWIFT_MIN_GAP_MS = 50;        // at most 20 angle updates a second
const uint32_t ZWIFT_KEEPALIVE_MS = 1000;
const uint32_t ZWIFT_CHALLENGE_RETRY_MS = 5000;

enum ZwiftState { ZWIFT_OFF, ZWIFT_ADVERTISING, ZWIFT_CONNECTED, ZWIFT_READY };

bool mpuReady = false;
uint16_t seq = 0;
uint32_t nextUs = 0;
char cmd[32];
int cmdLen = 0;

bool bleStarted = false;
volatile bool zwiftConnected = false;
volatile bool zwiftSubscribed = false;  // Zwift turned on handshake indications
volatile bool zwiftSpoke = false;       // Zwift has written to the handshake characteristic
volatile bool zwiftReady = false;       // handshake finished
float zwiftAngle = 0.0;
float zwiftSent = NAN;
uint32_t zwiftSentMs = 0, zwiftChallengeMs = 0;
BLECharacteristic *char30, *char32;

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

// Fills raw accel and gyro counts; returns false if the sensor is missing.
bool readMpu(int16_t accel[3], int16_t gyro[3]) {
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
    accel[k] = be16(2 * k);
    gyro[k] = be16(8 + 2 * k);
  }
  return true;
}

int readHallMv(int pin) {
  uint32_t sum = 0;
  for (int i = 0; i < 8; i++) sum += analogReadMilliVolts(pin);
  return sum / 8;
}

// --- Zwift steering (Elite Sterzo Smart emulation) ---

static uint32_t rotateLeft32(uint32_t value, uint32_t count) {
  const uint32_t mask = 31;
  count &= mask;
  return (value << count) | (value >> (-count & mask));
}

// Answer to the seed Zwift sends in the 0x0312 handshake step.
static uint32_t sterzoHash(uint64_t seed) {
  uint32_t ret = (uint32_t)(seed + 0x16fa5717);
  uint64_t rax = seed * 0xba2e8ba3ULL;
  uint64_t eax = (rax >> 35) * 0xb;
  uint64_t ecx = seed - eax;
  return ret ^ rotateLeft32((uint32_t)seed, (uint32_t)(ecx & 0x0F));
}

static void indicate(const uint8_t *data, size_t len) {
  char32->setValue((uint8_t *)data, len);
  char32->indicate();
}

class ServerCallbacks : public BLEServerCallbacks {
  void onConnect(BLEServer *) override {
    zwiftConnected = true;
    zwiftSubscribed = zwiftSpoke = zwiftReady = false;
    zwiftSent = NAN;
  }
  void onDisconnect(BLEServer *) override {
    zwiftConnected = zwiftSubscribed = zwiftSpoke = zwiftReady = false;
    BLEDevice::startAdvertising();  // be ready for the next connection
  }
};

class HandshakeCallbacks : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic *c) override {
    String v = c->getValue();
    const uint8_t *b = (const uint8_t *)v.c_str();
    zwiftSpoke = true;
    if (v.length() < 2 || b[0] != 0x03) return;
    if (b[1] == 0x10) {
      const uint8_t challenge[] = {0x03, 0x10, 0x12, 0x34};
      indicate(challenge, sizeof(challenge));
    } else if (b[1] == 0x11) {
      const uint8_t ok[] = {0x03, 0x11, 0xff, 0xff};
      indicate(ok, sizeof(ok));
      zwiftReady = true;
    } else if (b[1] == 0x12 && v.length() >= 6) {
      uint32_t seed = (uint32_t)b[2] | (uint32_t)b[3] << 8 | (uint32_t)b[4] << 16 | (uint32_t)b[5] << 24;
      uint32_t answer = sterzoHash(seed);
      const uint8_t reply[] = {0x03, 0x12, (uint8_t)answer, (uint8_t)(answer >> 8),
                               (uint8_t)(answer >> 16), (uint8_t)(answer >> 24)};
      indicate(reply, sizeof(reply));
    } else if (b[1] == 0x13) {
      const uint8_t done[] = {0x03, 0x13, 0xff};
      indicate(done, sizeof(done));
      zwiftReady = true;
    }
  }
};

class SubscribeCallbacks : public BLEDescriptorCallbacks {
  void onWrite(BLEDescriptor *) override { zwiftSubscribed = true; }
};

void startZwiftBle() {
  BLEDevice::init("STERZO");
  BLEServer *server = BLEDevice::createServer();
  server->setCallbacks(new ServerCallbacks());
  BLEService *svc = server->createService(STERZO_SERVICE_UUID);
  BLECharacteristic *char14 = svc->createCharacteristic(CHAR14_UUID, BLECharacteristic::PROPERTY_NOTIFY);
  char30 = svc->createCharacteristic(CHAR30_UUID, BLECharacteristic::PROPERTY_NOTIFY);
  BLECharacteristic *char31 = svc->createCharacteristic(CHAR31_UUID, BLECharacteristic::PROPERTY_WRITE);
  char32 = svc->createCharacteristic(CHAR32_UUID, BLECharacteristic::PROPERTY_INDICATE);
  char14->addDescriptor(new BLE2902());
  BLE2902 *notify30 = new BLE2902();
  notify30->setNotifications(true);
  char30->addDescriptor(notify30);
  BLE2902 *indicate32 = new BLE2902();
  indicate32->setCallbacks(new SubscribeCallbacks());
  char32->addDescriptor(indicate32);
  char31->setCallbacks(new HandshakeCallbacks());

  uint8_t ff = 0xff;
  char14->setValue(&ff, 1);
  float zero = 0.0;
  char30->setValue((uint8_t *)&zero, 4);
  const uint8_t challenge[] = {0x03, 0x10, 0x12, 0x34};
  char32->setValue((uint8_t *)challenge, sizeof(challenge));
  svc->start();

  BLEAdvertising *adv = BLEDevice::getAdvertising();
  adv->addServiceUUID(STERZO_SERVICE_UUID);
  adv->setScanResponse(true);
  adv->setMinPreferred(0x06);
  adv->setMinPreferred(0x12);
  BLEDevice::startAdvertising();
  bleStarted = true;
}

ZwiftState zwiftState() {
  if (!bleStarted) return ZWIFT_OFF;
  if (!zwiftConnected) return ZWIFT_ADVERTISING;
  return zwiftReady ? ZWIFT_READY : ZWIFT_CONNECTED;
}

// Sends the steering angle to Zwift (in whole degrees, as the Sterzo does)
// when it changes, plus a keep-alive. Until Zwift starts the handshake itself,
// offers the challenge every few seconds (some Zwift versions wait for it).
void serviceZwift() {
  if (!bleStarted || !zwiftConnected) return;
  uint32_t now = millis();
  if (zwiftSubscribed && !zwiftSpoke && now - zwiftChallengeMs > ZWIFT_CHALLENGE_RETRY_MS) {
    zwiftChallengeMs = now;
    const uint8_t challenge[] = {0x03, 0x10, 0x12, 0x34};
    indicate(challenge, sizeof(challenge));
  }
  float angle = roundf(constrain(zwiftAngle, -ZWIFT_MAX_DEG, ZWIFT_MAX_DEG));
  if (angle == 0.0f) angle = 0.0f;  // no negative zero
  bool changed = isnan(zwiftSent) || angle != zwiftSent;
  if ((changed && now - zwiftSentMs >= ZWIFT_MIN_GAP_MS) || now - zwiftSentMs >= ZWIFT_KEEPALIVE_MS) {
    char30->setValue((uint8_t *)&angle, 4);
    char30->notify();
    zwiftSent = angle;
    zwiftSentMs = now;
  }
}

// Reads "Z,<degrees>" commands from the PC.
void readCommands() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c != '\n') {
      if (cmdLen < (int)sizeof(cmd) - 1) cmd[cmdLen++] = c;
      continue;
    }
    cmd[cmdLen] = 0;
    if (cmd[0] == 'Z' && cmd[1] == ',') {
      zwiftAngle = atof(cmd + 2);
      if (!bleStarted) startZwiftBle();
    }
    cmdLen = 0;
  }
}

void setup() {
  Serial.begin(BAUD);
  pinMode(LED_PIN, OUTPUT);
  for (const HallSensor &h : HALLS) {
    pinMode(h.gndPin, OUTPUT);
    gpio_set_drive_capability((gpio_num_t)h.gndPin, GPIO_DRIVE_CAP_3);
    digitalWrite(h.gndPin, LOW);
    pinMode(h.vccPin, OUTPUT);
    gpio_set_drive_capability((gpio_num_t)h.vccPin, GPIO_DRIVE_CAP_3);
    digitalWrite(h.vccPin, HIGH);
  }
  Wire.begin(SDA_PIN, SCL_PIN, 400000);
  Wire.setTimeOut(5);  // ms; keeps a missing sensor from stalling the loop
  nextUs = micros();
}

void loop() {
  while ((int32_t)(micros() - nextUs) < 0) {}
  uint32_t t = nextUs;
  nextUs += PERIOD_US;

  int16_t a[3] = {0, 0, 0}, g[3] = {0, 0, 0};
  bool ok = readMpu(a, g);
  digitalWrite(LED_PIN, ok || (millis() / 250) % 2);

  char line[100];
  int n = snprintf(line, sizeof(line), "D,%u,%lu,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d",
                   seq++, (unsigned long)t, ok, a[0], a[1], a[2], g[0], g[1], g[2],
                   readHallMv(HALLS[0].outPin), readHallMv(HALLS[1].outPin), zwiftState());
  uint8_t x = 0;
  for (int i = 0; i < n; i++) x ^= line[i];
  Serial.printf("%s*%02X\n", line, x);

  readCommands();
  serviceZwift();

  // If a slow I2C error made us fall behind, skip ahead instead of bursting.
  if ((int32_t)(micros() - nextUs) > (int32_t)PERIOD_US) nextUs = micros();
}
