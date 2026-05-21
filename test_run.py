import traceback
try:
    from lunchbox_detector import run_gui
    run_gui()
except Exception as e:
    traceback.print_exc()
    input("Press Enter to exit...")
