import os
import tempfile
import unittest

from sizing import SizingError, SymbolSpec, compute_lot
from state import StateStore


class SafetyTests(unittest.TestCase):
    def test_risk_sizing_fails_closed_below_minimum(self):
        spec = SymbolSpec(
            name="EURUSD", tick_value=10.0, tick_size=0.0001,
            volume_min=0.1, volume_max=100.0, volume_step=0.1,
        )
        with self.assertRaises(SizingError):
            compute_lot(
                equity=100.0, risk_percent=1.0,
                loss_per_lot=1000.0, spec=spec, max_lot=1.0,
            )

    def test_state_leg_key_is_stable_and_unique_by_leg(self):
        self.assertEqual(StateStore.key(1, 2, 0), StateStore.key(1, 2, 0))
        self.assertNotEqual(StateStore.key(1, 2, 0), StateStore.key(1, 2, 1))

    def test_signal_and_leg_survive_reopen(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "state.sqlite3")
            s = StateStore(path)
            s.begin_signal(10, 20, 30, 40, "BUY EURUSD SL 1.0")
            leg = s.upsert_leg(10, 20, 0, "EURUSD", "buy", "market", 0.1, None, 1.0, 1.2)
            s.set_leg_status(10, 20, 0, "SUBMITTING", detail="before send")
            s.set_offset(31)
            s.db.close()

            s2 = StateStore(path)
            restored = s2.get_leg(10, 20, 0)
            self.assertEqual(restored.idempotency_key, leg.idempotency_key)
            self.assertEqual(restored.status, "SUBMITTING")
            self.assertEqual(s2.get_offset(), 31)
            self.assertEqual(len(list(s2.unsettled_legs())), 1)

    def test_message_content_change_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            s = StateStore(os.path.join(td, "state.sqlite3"))
            s.begin_signal(1, 2, 3, 4, "original")
            with self.assertRaises(RuntimeError):
                s.begin_signal(1, 2, 5, 6, "edited")


if __name__ == "__main__":
    unittest.main()
