# Bike Game Controller

Use a real bike on a smart trainer as a game controller. Turn the handlebars to steer, squeeze the brake levers to brake, and pedal to accelerate or walk. Games see an Xbox 360 controller and, if you like, keyboard keys.

It's been used with American Truck Simulator, Minecraft and Zwift, and the controls can be set up for any game that supports a controller or keyboard.

**What's in this repository**

- `firmware/`: ESP32 firmware that reads a steering sensor on the handlebars and a sensor on each brake lever.
- `pc/`: a Windows app that turns those readings, plus your smart trainer's power, into a virtual Xbox controller and key presses, with game profiles, tuning and guided calibration.

The companion project [ATS Trainer Telemetry](https://github.com/devon7y/ats-trainer-telemetry) is a plugin for American Truck Simulator and Euro Truck Simulator 2 that lets the app make your trainer harder to pedal on climbs.

## Features

- **Steering** from an MPU6050 on the stem. The gyro gives fast response, and gravity corrects drift, using the tilt of the bike's steering axis (the head-tube angle), so the angle never wanders. Calibration finds the steering axis itself, so the sensor can be mounted at any angle.
- **Brakes** from a linear Hall sensor and a small magnet on each lever, read as a smooth 0–100%. The brakes themselves aren't modified.
- **Pedalling** from any Bluetooth FTMS smart trainer (tested with a Tacx Flux 2). Choose power, speed or cadence, and what counts as 100%.
- **Game profiles.** Any bike input can drive any stick, trigger, controller button, keyboard key or mouse button. Buttons and keys can be held or toggled. There are starting profiles for driving games, Minecraft, Zwift and testing.
- **Zwift steering.** The ESP32 shows up in Zwift as a steering device called STERZO.
- **Trainer resistance from the game.** With the [ATS Trainer Telemetry](https://github.com/devon7y/ats-trainer-telemetry) plugin, the trainer's slope follows the truck's road in American Truck Simulator and Euro Truck Simulator 2.

## Hardware

| Part | Notes |
|---|---|
| ESP32 dev board | ESP32-WROOM-32, 30-pin, with a USB-serial chip (CP2102 or similar) |
| MPU6050 board (GY-521) | Steering sensor. MPU6500-based clones work too. Its header pins need soldering. |
| 2 × SS49E linear Hall sensors | One per brake lever |
| 2 × 6 × 3 mm neodymium disc magnets | One per brake lever |
| Female-to-female and male-to-female jumper wires | 20 cm wires can be joined to reach the levers |
| USB cable to the PC | A 6 m (20 ft) USB 2.0 cable has worked fine |
| Smart trainer with Bluetooth FTMS | For pedalling. The PC needs Bluetooth LE. |
| Windows 10 or 11 PC | Runs the app and the games |

No breadboard is needed. The ESP32 has only one 3.3 V pin, so each brake sensor is powered from two spare GPIO pins (one driven high, one low). They draw under 10 mA.

### Wiring

The steering sensor (GY-521):

| GY-521 pin | ESP32 pin |
|---|---|
| VCC | 3V3 |
| GND | GND |
| SCL | GPIO22 (often labelled D22) |
| SDA | GPIO21 |

Each Hall sensor: hold it with the printed side (49E) towards you and the legs pointing down. The legs are then power, ground and signal, from left to right.

| | Power (left leg) | Ground (middle leg) | Signal (right leg) |
|---|---|---|---|
| Brake sensor A | GPIO32 | GPIO33 | GPIO34 |
| Brake sensor B | GPIO25 | GPIO26 | GPIO35 |

It doesn't matter which sensor goes on which lever: calibration works that out.

### Mounting

- **Steering sensor:** flat on top of the stem, with a layer of electrical tape under it so its pins can't touch the metal. It must be held firmly, since any wobble reads as steering.
- **ESP32:** on the handlebar next to the stem, in a small bag or case so its pins are covered.
- **Brakes:** tape the Hall sensor to the front of the handlebar just below the lever hood, printed side facing forward. Tape a magnet to the back of the lever, where it swings closest to the bar, so that squeezing brings it within a few millimetres of the sensor. Use the app's live readings to find the spot that changes the most.

## Software setup

### Firmware

1. Install the [Arduino IDE 2](https://www.arduino.cc/en/software) or [arduino-cli](https://arduino.github.io/arduino-cli/), and the **esp32** board package by Espressif (3.x).
2. Open `firmware/bike_controller/bike_controller.ino`, pick the board **ESP32 Dev Module** and your COM port, and upload. With arduino-cli:
   ```
   arduino-cli compile --fqbn esp32:esp32:esp32 --upload -p COM3 firmware/bike_controller
   ```

If Windows doesn't create a COM port for the board, install the driver for its USB chip (for a CP2102, the Silicon Labs CP210x driver from Windows Update).

`firmware/hardware_tests/` has small sketches for checking each part as you wire it: `blink`, `i2c_diag` (is the steering sensor wired right?) and `sensor_check` (prints all three sensors).

### PC app

1. Install 64-bit [Python 3.11](https://www.python.org/downloads/windows/).
2. Install the [ViGEmBus 1.22.0 driver](https://github.com/nefarius/ViGEmBus/releases/tag/v1.22.0). It creates the virtual Xbox controller. Install it **before** the next step, otherwise `vgamepad` launches the installer for an older version.
3. From the repository folder:
   ```
   py -3.11 -m venv venv
   venv\Scripts\pip install -r pc\requirements.txt
   ```
4. Start the app with `venv\Scripts\pythonw.exe pc\bike_controller.pyw`. You can make a desktop shortcut to that, using `pc\icon.ico` as the icon.

The app finds the ESP32 and the trainer by itself. The first time, open the **Calibrate** tab and run a **full calibration** (about a minute): hold still, turn the bars right then left, then squeeze each brake three times.

There's also a command-line mode for testing: `python -m bikepad monitor`, `run` or `calibrate` (run from the `pc` folder).

## Using it

The header shows the bike, steering sensor and trainer connections, the current game profile and the **Game controls** switch, which turns the virtual controller and key presses on and off.

- **Dashboard:** live handlebars, brakes and pedalling, beside a drawing of the controller showing what the game receives.
- **Controls:** what each part of the bike does in the current profile. Each row links a bike input (handlebars, pedalling, left/right/either/both brakes) to a stick, trigger, controller button, or keyboard key or mouse button. Buttons and keys can be **Hold** or **Toggle**. There's also **Swap left and right triggers**, and a checkbox to send steering to Zwift.
- **Tuning:**
  - **Handlebars:** full-turn angle, dead zone and response curve.
  - **Pedalling:** power, speed or cadence, what counts as 100%, a starting threshold and smoothing.
  - **Brakes:** dead zone and curve.
- **Trainer:** installs the truck-game plugin and sets how the trainer's resistance follows the game's hills.
- **Calibrate:** a full calibration, or the brakes only.

### Games

- **American Truck Simulator / Euro Truck Simulator 2:** use the *Driving games* profile and choose the gamepad option in the game's controls. Pedalling power is the throttle, either brake is the brake. For hills you can feel, install the plugin from the Trainer tab.
- **Minecraft (Bedrock):** controllers work directly. The *Minecraft* profile is a starting point: handlebars look around, pedalling walks, hard pedalling sprints, the right brake sneaks. Minecraft Java needs a controller mod such as Controlify.
- **Zwift:** choose the *Zwift* profile, then in Zwift's pairing screen pick **STERZO** under Steer. Zwift reads the trainer itself, so the app releases it while a profile doesn't use pedalling. The app has to stay open, because it works out the steering angle.

## How it works

The ESP32 sends raw sensor readings to the PC 200 times a second over USB serial, with a checksum on each line. The app does everything else:

- **Steering:** fuses the gyro with gravity's direction around the tilted steering axis.
- **Brakes:** maps each Hall sensor reading between its calibrated released and squeezed values.
- **Trainer:** reads Indoor Bike Data over Bluetooth, and sets its simulated slope through the FTMS control point.
- **Output:** drives the virtual controller through ViGEmBus, and keys through Windows `SendInput`.

For Zwift, the app sends the steering angle back to the ESP32, which acts as an Elite Sterzo Smart over Bluetooth, including Zwift's handshake.

## Credits

- The Sterzo Smart protocol and Zwift handshake follow [stefaandesmet2003/ESP32Sterzo](https://github.com/stefaandesmet2003/ESP32Sterzo) and [Felixrising/STERZStick](https://github.com/Felixrising/STERZStick).
- Virtual controller: [ViGEmBus](https://github.com/nefarius/ViGEmBus) and [vgamepad](https://github.com/yannbouteiller/vgamepad).
- Bluetooth: [Bleak](https://github.com/hbldh/bleak). Theme: [Sun Valley ttk](https://github.com/rdbende/Sun-Valley-ttk-theme).

This project isn't affiliated with Zwift, Elite, Tacx, Garmin or SCS Software.

## License

MIT (see [LICENSE](LICENSE)). `pc/game_plugin/bike_controller_telemetry.dll` is the prebuilt plugin from [ATS Trainer Telemetry](https://github.com/devon7y/ats-trainer-telemetry), which includes code under SCS Software's SDK license.
