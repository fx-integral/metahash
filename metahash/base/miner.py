# ============================================================================ #
# metahash/base/miner.py                                                       #
# Clean base class for Bittensor miners (event-driven, v2-ready).              #
# - Serves an Axon and attaches handlers for:                                  #
#     • AuctionStartSynapse (masters broadcast at epoch head)                  #
#     • WinSynapse          (invoices to pay)                                  #
# - Provides shared blacklist/priority logic and a simple run loop.            #
# - NO custom project state here (kept in neurons/miner.py).                   #
# ============================================================================ #

import typing
import time
import asyncio
import threading
import argparse
import traceback
import bittensor as bt

from metahash.base.neuron import BaseNeuron
from metahash.base.utils.config import add_miner_args  # compatibility with your CLI pack
from typing import Union
from metahash.protocol import AuctionStartSynapse, WinSynapse


class BaseMinerNeuron(BaseNeuron):
    """
    Base class for Bittensor miners (event-driven).

    Subclasses override:
      - auctionstart_forward(self, synapse: AuctionStartSynapse) -> AuctionStartSynapse
      - win_forward(self, synapse: WinSynapse) -> WinSynapse
      - forward(self, synapse: bt.Synapse) -> bt.Synapse   # optional catch-all
    """

    neuron_type: str = "MinerNeuron"

    # ------------------------------- CLI hooks ------------------------------- #
    @classmethod
    def add_args(cls, parser: argparse.ArgumentParser):
        # Keep legacy compatibility; safe no-op if your add_miner_args ignores cls
        super().add_args(parser)
        add_miner_args(cls, parser)

    # ------------------------------ construction ---------------------------- #
    def __init__(self, config=None):
        super().__init__(config=config)

        # Warn if allowing incoming requests from anyone.
        if not self.config.blacklist.force_validator_permit:
            bt.logging.warning(
                "You are allowing non-validators to send requests to your miner. This is a security risk."
            )
        if self.config.blacklist.allow_non_registered:
            bt.logging.warning(
                "You are allowing non-registered entities to send requests to your miner. This is a security risk."
            )

        # The axon handles request processing, allowing validators to send requests to this miner.
        self.axon = bt.axon(
            wallet=self.wallet,
            config=self.config() if callable(self.config) else self.config,
        )

        # Catch-all forward for any other synapse
        bt.logging.info("Attaching generic forward function to miner axon.")

        self.axon.attach(
            forward_fn=self.forward,
            blacklist_fn=self.blacklist,
            priority_fn=self.priority,
        )

        # # Attach v2 handlers (typed by synapse)
        bt.logging.info("Attaching v3 auction handlers to miner axon")
        self.axon.attach(
            forward_fn=self.auctionstart_forward,
            blacklist_fn=self.auctionstart_blacklist,
            priority_fn=self.auctionstart_priority,
        )
        self.axon.attach(
            forward_fn=self.win_forward,
            blacklist_fn=self.win_blacklist,
            priority_fn=self.win_priority,
        )

        bt.logging.info(f"Axon created: {self.axon}")

        # Background runner fields
        self.should_exit: bool = False
        self.is_running: bool = False
        self.thread: Union[threading.Thread, None] = None
        self.lock = asyncio.Lock()

    # ----------------------------- run lifecycle ----------------------------- #
    def run(self):
        """
        Serve axon and keep neuron synced with block cadence (weights disabled).
        Event-driven auction logic lives entirely in *_forward handlers.
        """
        # Ensure registered, initialize chain objects.
        self.sync()

        # Serve the miner's axon on chain.
        bt.logging.info(
            f"Serving miner axon {self.axon} on network: {self.config.subtensor.chain_endpoint} "
            f"with netuid: {self.config.netuid}"
        )
        self.axon.serve(netuid=self.config.netuid, subtensor=self.subtensor)
        self.axon.start()

        bt.logging.info(f"Miner starting at block: {self.block}")

        try:
            while not self.should_exit:
                # light epoch cadence for metagraph maintenance
                while (
                    True
                ):
                    if self.should_exit:
                        break

                    self.sync()   
                    self.step += 1
                    time.sleep(12 * 4)

        except KeyboardInterrupt:
            self.axon.stop()
            bt.logging.success("Miner killed by keyboard interrupt.")
            exit()
        except Exception:
            bt.logging.error(traceback.format_exc())

    # ------------------------ background-thread helpers ---------------------- #
    def run_in_background_thread(self):
        if not self.is_running:
            bt.logging.debug("Starting miner in background thread.")
            self.should_exit = False
            self.thread = threading.Thread(target=self.run, daemon=True)
            self.thread.start()
            self.is_running = True
            bt.logging.debug("Started")

    def stop_run_thread(self):
        if self.is_running:
            bt.logging.debug("Stopping miner in background thread.")
            self.should_exit = True
            if self.thread is not None:
                self.thread.join(5)
            self.is_running = False
            bt.logging.debug("Stopped")

    def __enter__(self):
        self.run_in_background_thread()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.stop_run_thread()

    # ------------------------- metagraph / weights sync ---------------------- #
    def resync_metagraph(self):
        """Resyncs the metagraph; miners do not set weights."""
        bt.logging.info("resync_metagraph()")
        self.metagraph.sync(subtensor=self.subtensor)

    def set_weights(self):
        """Miners do not emit weights; keep no‑op."""
        pass

    # -------------------------- blacklist & priority ------------------------- #
    async def blacklist(self, synapse: bt.Synapse) -> typing.Tuple[bool, str]:
        return await self._common_blacklist(synapse)

    async def auctionstart_blacklist(self, synapse: AuctionStartSynapse) -> typing.Tuple[bool, str]:
        return await self._common_blacklist(synapse)

    async def win_blacklist(self, synapse: WinSynapse) -> typing.Tuple[bool, str]:
        return await self._common_blacklist(synapse)

    async def priority(self, synapse: bt.Synapse) -> float:
        return await self._common_priority(synapse)

    async def auctionstart_priority(self, synapse: AuctionStartSynapse) -> float:
        return await self._common_priority(synapse)

    async def win_priority(self, synapse: WinSynapse) -> float:
        return await self._common_priority(synapse)

    async def _common_blacklist(
        self, synapse: typing.Union[bt.Synapse, AuctionStartSynapse, WinSynapse]
    ) -> typing.Tuple[bool, str]:
        if synapse.dendrite is None or synapse.dendrite.hotkey is None:
            bt.logging.warning("Received a request without a dendrite or hotkey.")
            return True, "Missing dendrite or hotkey"

        validator_hotkey = synapse.dendrite.hotkey

        # Ensure hotkey is recognized unless allow_non_registered
        if (
            not self.config.blacklist.allow_non_registered
            and validator_hotkey not in self.metagraph.hotkeys
        ):
            bt.logging.warning(f"Unrecognized hotkey: {validator_hotkey}")
            return True, f"Unrecognized hotkey: {validator_hotkey}"

        uid = self.metagraph.hotkeys.index(validator_hotkey)

        # Optionally force only validators
        if self.config.blacklist.force_validator_permit:
            if not self.metagraph.validator_permit[uid]:
                bt.logging.warning(f"Blacklisted Non-Validator {validator_hotkey}")
                return True, f"Non-validator hotkey: {validator_hotkey}"

        # Minimum stake check
        stake = self.metagraph.S[uid]
        min_stake = self.config.blacklist.minimum_stake_requirement
        if stake < min_stake:
            bt.logging.warning(f"Blacklisted insufficient stake: {validator_hotkey}")
            return True, f"Insufficient stake ({stake} < {min_stake})"

        return False, f"Hotkey recognized: {validator_hotkey}"

    async def _common_priority(
        self, synapse: typing.Union[bt.Synapse, AuctionStartSynapse, WinSynapse]
    ) -> float:
        if synapse.dendrite is None or synapse.dendrite.hotkey is None:
            return 0.0
        validator_hotkey = synapse.dendrite.hotkey
        if validator_hotkey not in self.metagraph.hotkeys:
            return 0.0
        caller_uid = self.metagraph.hotkeys.index(validator_hotkey)
        return float(self.metagraph.S[caller_uid])
