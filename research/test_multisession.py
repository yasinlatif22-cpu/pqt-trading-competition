"""One strategy instance across two sessions, as on a live server (on_start runs only once)."""
import importlib.util, sys
from pqt_client.backtest_v2 import load_session
from pqt_client.v2 import Strategy, backtest_strategy
spec = importlib.util.spec_from_file_location("strat", sys.argv[1]); mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
Base = next(v for v in vars(mod).values() if isinstance(v, type) and issubclass(v, Strategy) and v is not Strategy and v.__module__ == "strat")
class Once(Base):
    def on_start(self):
        if getattr(self, "_started", False):
            return
        self._started = True
        super().on_start()
inst = Once()
backtest_strategy(load_session(sys.argv[2], "s01"), inst)
clean1 = sum(st["clean"] for s in inst.done_blocks for _, st in inst.done_blocks[s])
backtest_strategy(load_session(sys.argv[2], "s02"), inst)
clean2 = sum(st["clean"] for s in inst.done_blocks for _, st in inst.done_blocks[s])
paused = {s: v for s, v in inst.paused_until.items()}
print("session 1 clean shares scored:", clean1, "| session 2:", clean2, "| sessions seen:", getattr(inst, "sessions", "?"), "| pending left:", len(inst.pending))
ok = clean1 > 0 and clean2 > 0 and clean2 != clean1 and len(inst.pending) < 1000
print("MULTISESSION TEST", "PASSED" if ok else "FAILED")
