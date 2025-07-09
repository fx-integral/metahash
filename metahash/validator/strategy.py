from pathlib import Path
from collections import defaultdict
import yaml
import bittensor as bt

def load_weights(path: Path):
    if not path.exists():
        bt.logging.warning(f"strategy file {path} missing – default 100 bps")
        return defaultdict(lambda: 100)
    try:
        data = yaml.safe_load(path.read_text()) or {}
        return defaultdict(lambda: 0, {int(k): int(v) for k, v in data.items()})
    except Exception as e:
        bt.logging.error(f"strategy load failed: {e}")
        return defaultdict(lambda: 0)
