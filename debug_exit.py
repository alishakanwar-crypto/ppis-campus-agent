"""Debug script to catch silent sys.exit() calls in main.py"""
import sys
import traceback

_real_exit = sys.exit

def _catch_exit(code=0):
    print(f"\n=== EXIT CALLED with code: {code} ===")
    print("Stack trace:")
    traceback.print_stack()
    _real_exit(code)

sys.exit = _catch_exit

print("Loading main.py...")
exec(compile(open('main.py', encoding='utf-8').read(), 'main.py', 'exec'))
print("main.py loaded without exit")
