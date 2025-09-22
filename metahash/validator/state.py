# neurons/state_store.py
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Set, Any

import bittensor
from metahash.utils.pretty_logs import pretty


@dataclass
class StateStore:
    """
    JSON-backed persistent store with atomic writes under a single root directory.

    Files (all relative to `root`):
      - validated_epochs.json : list[int]
      - jailed_coldkeys.json  : {str: int}
      - reputation.json       : {str: float}
      - pending_commits.json  : {epoch_str: payload_dict}
    """
    root: Path = field(default_factory=lambda: Path.cwd())
    _file_lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    validated_epochs: Set[int] = field(default_factory=set)
    ck_jail_until_epoch: Dict[str, int] = field(default_factory=dict)
    reputation: Dict[str, float] = field(default_factory=dict)
    pending_commits: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    # ---- Filenames (constants) ----
    _FN_VALIDATED = "validated_epochs.json"
    _FN_JAILED = "jailed_coldkeys.json"
    _FN_REPUTATION = "reputation.json"
    _FN_PENDING = "pending_commits.json"

    def __post_init__(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.validated_epochs = self._load_set(self._FN_VALIDATED)
        self.ck_jail_until_epoch = self._load_dict_int(self._FN_JAILED)
        self.reputation = self._load_dict_float(self._FN_REPUTATION)
        self.pending_commits = self._load_dict_obj(self._FN_PENDING)

    # ---------- file ops ----------
    def _atomic_write_text(self, path: Path, text: str) -> None:
        """
        Write atomically on the same filesystem using a temp file + os.replace.
        Ensures directory exists; guarded with a process-level lock.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._file_lock:
            # Use NamedTemporaryFile in the same directory for atomic replace on all OSes
            tmp_suffix = f".tmp.{os.getpid()}.{int(time.time() * 1000)}.{uuid.uuid4().hex[:6]}"
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(path.parent),
                prefix=path.name + ".",
                suffix=tmp_suffix,
                delete=False,
            ) as tmp:
                tmp.write(text)
                tmp.flush()
                os.fsync(tmp.fileno())
                tmp_path = Path(tmp.name)
            os.replace(tmp_path, path)

    def _load_text_json(self, path: Path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _load_set(self, filename: str) -> Set[int]:
        data = self._load_text_json(self.root / filename)
        if isinstance(data, list):
            try:
                return set(int(x) for x in data)
            except Exception:
                return set()
        return set()

    def _save_set(self, s: Set[int], filename: str) -> None:
        path = self.root / filename
        try:
            self._atomic_write_text(path, json.dumps(sorted(s)))
        except Exception as e:
            pretty.log(f"[yellow]Could not save {filename}: {e}[/yellow]")

    def _load_dict_int(self, filename: str) -> Dict[str, int]:
        data = self._load_text_json(self.root / filename)
        if isinstance(data, dict):
            out: Dict[str, int] = {}
            for k, v in data.items():
                try:
                    out[str(k)] = int(v)
                except Exception:
                    continue
            return out
        return {}

    def _save_dict_int(self, d: Dict[str, int], filename: str) -> None:
        path = self.root / filename
        try:
            self._atomic_write_text(path, json.dumps(d, sort_keys=True))
        except Exception as e:
            pretty.log(f"[yellow]Could not save {filename}: {e}[/yellow]")

    def _load_dict_float(self, filename: str) -> Dict[str, float]:
        data = self._load_text_json(self.root / filename)
        if isinstance(data, dict):
            out: Dict[str, float] = {}
            for k, v in data.items():
                try:
                    out[str(k)] = float(v)
                except Exception:
                    continue
            return out
        return {}

    def _save_dict_float(self, d: Dict[str, float], filename: str) -> None:
        path = self.root / filename
        try:
            self._atomic_write_text(path, json.dumps(d, sort_keys=True))
        except Exception as e:
            pretty.log(f"[yellow]Could not save {filename}: {e}[/yellow]")

    def _load_dict_obj(self, filename: str) -> Dict[str, Dict[str, Any]]:
        data = self._load_text_json(self.root / filename)
        return data if isinstance(data, dict) else {}

    def _save_dict_obj(self, d: Dict[str, Dict[str, Any]], filename: str) -> None:
        path = self.root / filename
        try:
            self._atomic_write_text(path, json.dumps(d, sort_keys=True))
        except Exception as e:
            pretty.log(f"[yellow]Could not save {filename}: {e}[/yellow]")

    # ---------- public API ----------
    def wipe(self) -> None:
        """Delete known state files under `root` only."""
        bittensor.logging.warning("Wiping cache...")
        filenames = (self._FN_VALIDATED, self._FN_JAILED, self._FN_REPUTATION, self._FN_PENDING)
        for fn in filenames:
            path = self.root / fn
            try:
                if path.exists():
                    path.unlink()
            except Exception as e:
                pretty.log(f"[yellow]Could not remove {fn}: {e}[/yellow]")
        pretty.log("[magenta]Fresh start requested: cleared validator local state files.[/magenta]")

    def save_validated_epochs(self) -> None:
        self._save_set(self.validated_epochs, self._FN_VALIDATED)

    def save_ck_jail(self) -> None:
        self._save_dict_int(self.ck_jail_until_epoch, self._FN_JAILED)

    def save_reputation(self) -> None:
        self._save_dict_float(self.reputation, self._FN_REPUTATION)

    def save_pending_commits(self) -> None:
        self._save_dict_obj(self.pending_commits, self._FN_PENDING)
