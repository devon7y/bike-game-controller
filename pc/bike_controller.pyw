"""Starts the Bike Controller window without a console (run with pythonw)."""
import sys

from bikepad.app import main

main(tab=sys.argv[1] if len(sys.argv) > 1 else None)
