// Blink test: toggles the onboard LED once per second and reports each toggle
// over USB serial, so the board can be checked both by eye and from the PC.

const int LED_PIN = 2;  // blue onboard LED on most 30-pin ESP32 dev boards

unsigned long blinks = 0;

void setup() {
  Serial.begin(115200);
  pinMode(LED_PIN, OUTPUT);
}

void loop() {
  digitalWrite(LED_PIN, HIGH);
  Serial.printf("blink %lu: LED on  (%s rev %d, %lu MHz)\n", ++blinks,
                ESP.getChipModel(), ESP.getChipRevision(), ESP.getCpuFreqMHz());
  delay(500);
  digitalWrite(LED_PIN, LOW);
  Serial.printf("blink %lu: LED off\n", blinks);
  delay(500);
}
