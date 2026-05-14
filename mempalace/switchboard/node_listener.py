"""
Switchboard node listener — skeleton.

Polls the on-chain `ResolverAssignment` PDAs for assignments
addressed to this palace's resolver pubkey. Emits internal
`switchboard_assignment_received` events that downstream code
turns into `ResolutionJob`s.

# Status

Skeleton — the polling loop and event emission shape are
defined but the on-chain instruction (`assign_resolver`) doesn't
exist yet. When it does, this module's `_poll_assignments` body
becomes a concrete RPC against the Solana program.

# Configuration

A palace opts into switchboard-node mode via
`MEMPALACE_SWITCHBOARD_ENABLED=1` (env var) AND a stake +
registration on-chain. Without both, `is_switchboard_node_enabled`
returns False and no listener spins up.
"""

from __future__ import annotations

import logging
import os
import threading
import time

from ..schema.events import SwitchboardAssignmentReceived

logger = logging.getLogger(__name__)


def is_switchboard_node_enabled() -> bool:
    """True if this palace should run as a switchboard node.

    Currently checks an environment variable. When the on-chain
    registration lands, also checks that the palace's pubkey is
    in the active resolver set.
    """
    return os.environ.get("MEMPALACE_SWITCHBOARD_ENABLED", "0") == "1"


class SwitchboardNodeListener:
    """Polls for assignments and dispatches them as ResolutionJobs.

    Lifecycle:

        listener = SwitchboardNodeListener(...)
        listener.start()
        # ... runs in background thread, polling
        listener.stop()

    The listener does NOT run jobs itself; it emits an internal
    `switchboard_assignment_received` log event with the assignment
    payload, and a separate worker (out of scope for this skeleton)
    consumes those events and constructs `ResolutionJob`s.

    Separation reasons:
      - The listener is one I/O loop; the job runtime is
        compute-heavy. Decouple their cadences.
      - The log-event indirection means the assignment is durably
        recorded before any work begins. Restart safety.
    """

    DEFAULT_POLL_INTERVAL_SEC = 30.0

    def __init__(
        self,
        resolver_pubkey: str,
        poll_interval_sec: float = DEFAULT_POLL_INTERVAL_SEC,
    ) -> None:
        self.resolver_pubkey = resolver_pubkey
        self.poll_interval_sec = poll_interval_sec
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        """Spawn the polling thread."""
        if self._thread is not None and self._thread.is_alive():
            logger.warning("SwitchboardNodeListener already running")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="switchboard-node-listener",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "SwitchboardNodeListener started for resolver %s",
            self.resolver_pubkey,
        )

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the polling thread to exit."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        logger.info("SwitchboardNodeListener stopped")

    def _run_loop(self) -> None:
        """Polling loop body."""
        while not self._stop_event.is_set():
            try:
                self._poll_assignments()
            except Exception as e:
                logger.warning("Assignment poll failed: %s", e)
            self._stop_event.wait(self.poll_interval_sec)

    def _poll_assignments(self) -> None:
        """Fetch any new assignments addressed to this resolver.

        Production wiring (when the chain client is connected):

            client = solana_client.SolanaClient(...)
            program = MEMPALACE_EXTENSIONS_PROGRAM
            # Filter by resolver pubkey appearing in the assignment's
            # `resolvers` array. SolanaClient doesn't have a generic
            # array-contains filter; the concrete implementation
            # subscribes to the program's `ResolverAssigned` events
            # and inspects each one for self-membership.
            for assignment in client.iter_resolver_assignments():
                if self._resolver_pubkey not in assignment.resolvers:
                    continue
                if assignment.address in self._seen:
                    continue
                self._seen.add(assignment.address)
                evt = SwitchboardAssignmentReceived(
                    assignment_id=assignment.address,
                    market_id=assignment.market_id,
                    privacy_mode=assignment.privacy_mode_label(),
                    slice_request_spec=assignment.slice_request_spec,
                    k_threshold=assignment.k_threshold,
                    n_resolvers=assignment.n,
                    consensus_rule=assignment.consensus_rule_label(),
                )
                self.log_client.append(evt)

        For now the skeleton is a no-op; emitting the event happens
        once the chain client is wired in (a follow-on session that
        depends on a working `cargo build` for the extensions
        program — the IDL drives the client codegen).
        """
        # Skeleton: no-op. Logged at debug to avoid noise.
        logger.debug(
            "SwitchboardNodeListener._poll_assignments: "
            "no-op skeleton; awaiting chain-client wiring"
        )
