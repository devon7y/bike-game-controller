"""Bike Controller.

    python -m bikepad              open the app window
    python -m bikepad monitor      print live values (no virtual controller)
    python -m bikepad run          act as the virtual controller without a window
    python -m bikepad calibrate    guided calibration in the terminal
    python -m bikepad calibrate --brakes   redo only the brakes

Add --seconds N to stop after N seconds and --profile NAME to pick a profile.
"""
import argparse
import time

from .profiles import Settings

STATUS_INTERVAL = 0.2  # seconds between printed lines


def print_status(engine):
    s = engine.snapshot
    deg = "-" if s.steering_deg is None else f"{s.steering_deg:+6.1f}"
    out = s.outputs
    controls = "  ".join([f"{k} {v:+.2f}" for k, v in {**out.axes, **out.triggers}.items()] +
                         sorted(out.buttons))
    print(f"steer {deg} deg | brakes {s.brakes_mv[0]} {s.brakes_mv[1]} mV "
          f"({s.levers[0]:.2f} {s.levers[1]:.2f}) | {engine.trainer.describe()} | "
          f"sensor {'ok' if s.mpu_ok else 'NO'} | {controls or 'no controls'}", flush=True)


def headless(args, settings):
    from .engine import Engine

    engine = Engine(settings, port=args.port, controller=args.command == "run")
    end = time.monotonic() + args.seconds if args.seconds else None
    if args.command == "calibrate":
        while not engine.snapshot.bike_connected:
            time.sleep(0.1)
        engine.start_calibration("brakes" if args.brakes else "full")
    shown = (None, 0)
    try:
        while not end or time.monotonic() < end:
            time.sleep(STATUS_INTERVAL)
            run = engine.calibration
            if args.command != "calibrate":
                print_status(engine)
                continue
            if (run.instruction, len(run.results)) != shown:
                if run.instruction != shown[0]:
                    print(f"\n{run.step}: {run.instruction}", flush=True)
                for line in run.results[shown[1]:]:
                    print(f"    {line}", flush=True)
                shown = (run.instruction, len(run.results))
            if run.done:
                print(f"\nCalibration stopped: {run.error}" if run.error else "\nCalibration saved.")
                break
    except KeyboardInterrupt:
        pass
    engine.shutdown()


def main():
    parser = argparse.ArgumentParser(prog="bikepad", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", default="app",
                        choices=["app", "monitor", "run", "calibrate"])
    parser.add_argument("--profile", help="profile name to use")
    parser.add_argument("--port", help="serial port, e.g. COM3 (found automatically if omitted)")
    parser.add_argument("--seconds", type=float, help="stop after this many seconds")
    parser.add_argument("--brakes", action="store_true", help="calibrate: redo only the brakes")
    args = parser.parse_args()

    if args.command == "app":
        from .app import main as app_main
        app_main(args.port)
        return
    settings = Settings()
    if args.profile:
        if args.profile not in settings.names():
            raise SystemExit(f"No profile called {args.profile!r}. Profiles: {settings.names()}")
        settings.current = args.profile
    headless(args, settings)


if __name__ == "__main__":
    main()
