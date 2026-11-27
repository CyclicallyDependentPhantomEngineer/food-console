"""Test-wide settings."""
import os

# Never touch the real macOS Keychain from tests: the store keeps its key in a file next to
# the (temporary) test database instead.
os.environ["FOOD_CONSOLE_KEY_BACKEND"] = "file"


import pytest

FAKE_READ_ONLY = {label: {"get_order_history", "search_menu", "payment_status", "search"}
                  for label in ("food", "zomato", "instamart", "dineout", "pay")}
FAKE_READ_ONLY["blinkit"] = {"status", "list_addresses", "search", "view_cart", "order_history"}
PAYMENT_POLICY = {"pay": {"place_order": {
    "uriField": ["payment", "uri"], "orderField": ["order_id"],
    "orderArgument": "order_id", "amountArgument": "amount", "payeeArgument": "payee"}}}


@pytest.fixture(autouse=True)
def isolated_credentials(monkeypatch, tmp_path):
    # No test may read real OAuth tokens or modify the user's cache.
    import food_console.store as store
    monkeypatch.setattr(store, "credential_fingerprint", lambda: "test-account")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "default-cache" / "food.db")
