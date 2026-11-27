"""Test-wide settings."""
import os

# Never touch the real macOS Keychain from tests: the store keeps its key in a file next to
# the (temporary) test database instead.
os.environ["FOOD_CONSOLE_KEY_BACKEND"] = "file"
