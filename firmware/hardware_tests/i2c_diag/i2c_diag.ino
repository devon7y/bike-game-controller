// I2C wiring diagnostic. The GY-521 board has its own pull-up resistors on SDA
// and SCL, so with the ESP32's weak pull-down enabled, a line reads HIGH only if
// its wire reaches a powered GY-521. Then scans every I2C address.
#include <Wire.h>

const int SDA_PIN = 21;
const int SCL_PIN = 22;

bool lineReachesBoard(int pin) {
  pinMode(pin, INPUT_PULLDOWN);
  delay(5);
  return digitalRead(pin) == HIGH;
}

void setup() {
  Serial.begin(115200);
}

void loop() {
  bool sda = lineReachesBoard(SDA_PIN);
  bool scl = lineReachesBoard(SCL_PIN);
  Serial.printf("SDA (D21): %s   SCL (D22): %s\n",
                sda ? "connected" : "NOT connected",
                scl ? "connected" : "NOT connected");

  Wire.begin(SDA_PIN, SCL_PIN, 100000);
  int found = 0;
  for (uint8_t addr = 1; addr < 127; addr++) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) {
      Serial.printf("  device answered at 0x%02X\n", addr);
      found++;
    }
  }
  if (!found) Serial.println("  no device answered");
  Wire.end();
  delay(1000);
}
