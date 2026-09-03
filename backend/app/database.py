"""SQLite database — metrics + users."""

import asyncio
import json
import logging
import os
import sqlite3
import time
import re
import weakref
from contextlib import asynccontextmanager
from contextvars import ContextVar
from enum import Enum
from typing import NamedTuple

import aiosqlite
import bcrypt

from app.config import settings

logger = logging.getLogger("glassops.db")

_db_path = settings.db_path
_conn: aiosqlite.Connection | None = None

# A bare ":memory:" gives EACH aiosqlite connection its OWN private
# database — with the dedicated metric connection added by this milestone,
# init_db() (on the shared connection) would create the schema in one
# database while store_metric (on the metric connection) writes to a
# different, empty one ("OperationalError: no such table: metrics"; server
# still starts, but every metric silently becomes ephemeral and History
# stays empty). Route ":memory:" to a shared-cache URI instead, so every
# connection attaches to the SAME in-memory database.
_MEMORY_URI = "file::memory:?cache=shared"


def _connect_args() -> tuple[str, bool]:
    """(path, uri) for aiosqlite.connect(), read from _db_path at CALL time
    (tests monkeypatch it). Ordinary file paths are unaffected; a `file:`
    URI the operator configured directly is passed through as-is."""
    if _db_path == ":memory:":
        return _MEMORY_URI, True
    return _db_path, _db_path.startswith("file:")


def _initial_admin_pw_file() -> str:
    """Path of the one-time initial-admin password file (data dir, alongside the DB)."""
    return os.path.join(os.path.dirname(_db_path) or ".", "initial_admin_password")


async def clear_initial_admin_password_file() -> None:
    """Best-effort removal of the one-time initial-admin password file once the admin
    has changed their password, so the plaintext credential doesn't linger on disk."""
    try:
        os.remove(_initial_admin_pw_file())
        logger.info("Removed initial admin password file after password change")
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("Could not remove initial admin password file", exc_info=True)


# ── Operation boundary ───────────────────────────────
#
# One lock per DB FILE, covering the shared AND the metric connection. Not one
# lock per connection: both write the same file, and the measured failure
# (deploy/contracts/slices/pl1/, case CP-1) is exactly a metric-connection
# commit landing between a shared read and a shared write — the shared write
# then fails with SQLITE_BUSY_SNAPSHOT, its transaction stays open, and every
# later write on that connection fails too while WAL checkpointing stalls.
#
# CP-1-RECOVER-* measured the recovery condition: closing the abandoned cursor
# alone does NOT recover, and rolling back alone does NOT recover — the
# statement's read snapshot and the failed transaction block independently.
# Both must be resolved before the lock is released.
#
# aiosqlite runs every statement on ONE worker thread per connection and awaits
# a future. Cancelling the caller does NOT stop the worker: it finishes the
# statement and drops the result because the future is already done. So every
# submission here goes through _submit(), which recovers the worker's TERMINAL
# outcome even when our own task is being cancelled. Anything less loses a
# cursor (whose snapshot then wedges the file) or mistakes an unknown COMMIT
# for a successful one.

_op_lock = asyncio.Lock()

# Set for the duration of one operation so a nested acquire raises instead of
# deadlocking on the non-reentrant lock. A ContextVar (not a plain flag) so a
# task waiting on the lock isn't mistaken for a nested caller.
_in_operation: ContextVar[bool] = ContextVar("glassops_db_in_operation", default=False)


class _Disposal(str, Enum):
    """What a submission's result OWNS, and therefore how a result that
    arrives after its caller gave up has to be disposed of.

    Bound into the unresolved entry at submission time rather than guessed
    later: by the time the worker answers, the only thing that still knows a
    connect returns a live sqlite handle (and a non-daemon worker thread) is
    the call site that submitted it."""

    NONE = "value"            # a plain value; nothing to own
    CONNECTION = "connection"  # an aiosqlite.Connection: sqlite handle + worker thread
    CURSOR = "cursor"          # a cursor: pins a read snapshot until it is closed


class _Unresolved(NamedTuple):
    """One worker submission whose outcome could not be established.

    `owner` is the aiosqlite Connection whose worker thread is running it —
    the only thing that can ever answer whether the submission is really over.
    Without it the registry has to fall back on the state of OUR future, and a
    cancelled future is not evidence about a thread."""

    what: str
    task: "asyncio.Task"
    disposal: _Disposal = _Disposal.NONE
    owner: object = None


class _LateResult(NamedTuple):
    """A resource a worker handed back AFTER its caller gave up on it.

    It has no caller left to close it, so it is owned here until a close is
    confirmed. Reading the task result and dropping the object is what leaves
    an open sqlite handle and a live aiosqlite thread behind while close_db
    still answers CLOSED."""

    what: str
    disposal: _Disposal
    obj: object
    owner: object = None


# ── admission state (all process-global, all deliberately sticky) ──
_fail_stop: str | None = None       # a terminal outcome could not be established
_closing = False                    # close_db() is quiescing
_closed = False                     # connections are gone; publish no more
_unresolved: list[_Unresolved] = []   # worker submissions still in flight
_unclosed: list = []                # connections whose close was never confirmed
_late_results: list[_LateResult] = []  # resources returned late, not yet closed

# Every connection this process ever built. A WeakSet so a collected connection
# drops out on its own, and the shutdown verdict can still make ONE statement
# about all of them: CLOSED means not one aiosqlite worker thread is left.
_connections: "weakref.WeakSet" = weakref.WeakSet()

# Bounds on how long one worker submission may stay unresolved, and how many
# times our own cancellation may interrupt waiting for it, before we stop
# guessing. _CLOSE_TIMEOUT bounds shutdown quiescence.
_CLEANUP_TIMEOUT = 10.0
_CLEANUP_CANCEL_BUDGET = 4
_CLOSE_TIMEOUT = 20.0


class DatabaseFailStop(RuntimeError):
    """The database refuses this operation: restart-required, or closing/closed.

    Raised once a terminal outcome could not be established. Deliberately NOT
    recoverable in-process: see _enter_fail_stop."""


class TransactionHandleMisuse(RuntimeError):
    """A write_transaction handle was used after its transaction ended, or by a
    task other than the one that opened it."""


class ReadOnlyViolation(RuntimeError):
    """The read helper was handed a statement that can mutate the database."""


class InvalidAggregationResolution(ValueError):
    """downsample_metrics() was called with a (seconds, label) pair the history
    tables do not serve.

    Raised BEFORE any transaction is opened: a mismatched pair would write
    buckets of one width under another width's label, and every later read,
    coverage check and retention decision would then be reasoning about a
    resolution that does not exist."""


class CloseVerdict(str, Enum):
    CLOSED = "closed"                        # every connection confirmed closed
    RESTART_REQUIRED = "restart-required"    # quiescence or close unconfirmed


class SchemaIntegrityViolation(RuntimeError):
    """Rows already in the database violate a foreign key the schema declares.

    Deliberately NOT repaired here. Deleting the rows would destroy an
    attributable record, and re-attaching them to whoever next owns that
    primary key is precisely the failure: `user_host_accounts` rows left behind
    by a deleted user become the terminal permissions of the next person to
    register that email. Startup fails and the database refuses service until
    an approved migration resolves them."""


class _OutcomeUnknown(Exception):
    """Internal — a worker submission neither completed nor failed observably.

    Carries the cancellation the caller is still owed, if one arrived while we
    were waiting. Without it, converting this into a fail-stop silently drops
    that cancellation — and the caller is told "restart required" instead of
    "you were cancelled"."""

    def __init__(self, what: str,
                 caller_cancelled: asyncio.CancelledError | None = None) -> None:
        super().__init__(what)
        self.what = what
        self.caller_cancelled = caller_cancelled


class _CancelledAfterWorkerError(asyncio.CancelledError):
    """Our caller was cancelled AND the worker later returned an error.

    Subclasses CancelledError on purpose: the caller is owed its cancellation,
    and raising the worker's error instead loses it. That matters concretely —
    app.main's periodic_maintenance re-raises CancelledError but swallows
    Exception, so a lost cancellation keeps the maintenance loop alive through
    shutdown and the shutdown await never finishes without a second cancel.
    The worker's error rides along so cleanup can still classify it."""

    def __init__(self, worker_error: BaseException) -> None:
        super().__init__()
        self.worker_error = worker_error

    def __str__(self) -> str:
        return f"cancelled; the worker then failed with {self.worker_error!r}"

    def __repr__(self) -> str:
        # super().__init__() takes no args, so the DEFAULT repr is an empty
        # class name — and every `{exc!r}` in a cleanup message would then
        # record a restart-required database whose reason names nothing.
        return f"{type(self).__name__}({self.worker_error!r})"


class _ChildCancelled(Exception):
    """Internal — a worker submission came back CANCELLED without us cancelling
    it. That proves nothing about what the worker did, so it is not a terminal
    outcome and must never be read as "the step completed".

    Carries any cancellation the caller is still owed, for the same reason
    _OutcomeUnknown does."""

    def __init__(self, what: str,
                 caller_cancelled: asyncio.CancelledError | None = None) -> None:
        super().__init__(what)
        self.what = what
        self.caller_cancelled = caller_cancelled


def _caller_owed(cancelled: asyncio.CancelledError | None,
                 error: BaseException) -> BaseException:
    """What the caller must actually be handed.

    A cancellation still owed WINS: app.main's periodic_maintenance re-raises
    CancelledError but swallows Exception, so a cancellation replaced by an
    error keeps that loop running through shutdown. The error rides along
    inside the cancellation so the reason is not lost either."""
    if cancelled is None or isinstance(error, asyncio.CancelledError):
        return error
    return _CancelledAfterWorkerError(error)


class _Incident:
    """The identity of ONE safety failure.

    An object, deliberately not a message and not a bool. A failure is
    routinely observed more than once — a rollback whose outcome nobody could
    establish, and then the transaction still being open BECAUSE of it — and
    those two observations are one incident, while the SAME two sentences
    arising from two different connections are two. Neither the text nor a
    "was this already recorded?" flag can tell those apart, so the identity is
    carried explicitly by whoever reports the follow-on.

    `recorded` says whether the fail-stop for this incident is already on the
    record: _submit latches inside _register_unresolved before it raises,
    so describing such an incident again must not count as a second failure."""

    __slots__ = ("recorded",)

    def __init__(self, *, recorded: bool) -> None:
        self.recorded = recorded


class _Problem(NamedTuple):
    """One observation of a safety problem, and the incident it belongs to.

    The two facts travel as one record because they only mean anything
    together. Held as two parallel string lists, "is this one already
    recorded?" had to be answered by asking whether the same TEXT appeared in
    the other list — so two genuinely separate observations that happen to read
    the same (the same cursor close failing twice, the same rollback message
    from two connections) collapsed into one answer, and whichever of them was
    still unrecorded was silently reported as recorded."""

    message: str
    incident: _Incident

    @property
    def recorded(self) -> bool:
        return self.incident.recorded


class _Outcome:
    """One operation's accumulated ending.

    Cancellation, failure and connection safety are three INDEPENDENT facts,
    and a stage-local variable loses whichever one it is not holding the moment
    a later stage — or a `finally` — raises. That is exactly how a cancellation
    ends up replaced by the cleanup error that followed it, and how a cursor
    close that FAILED slips out as "just a cancellation" with nothing latched.

    Every stage reports into one of these instead, and only settle() decides
    what the caller is owed."""

    __slots__ = ("cancelled", "error", "problems")

    def __init__(self) -> None:
        self.cancelled: asyncio.CancelledError | None = None
        self.error: BaseException | None = None
        # Each entry carries its own recorded flag; see _Problem.
        self.problems: list[_Problem] = []

    def owe(self, cancelled: asyncio.CancelledError | None) -> None:
        """Remember a cancellation the caller is owed. The FIRST one wins: it
        is the one that actually interrupted the caller."""
        if cancelled is not None and self.cancelled is None:
            self.cancelled = cancelled

    def absorb(self, exc: BaseException) -> None:
        """Record an exception WITHOUT losing a cancellation buried inside it."""
        if isinstance(exc, _CancelledAfterWorkerError):
            self.owe(exc)
            exc = exc.worker_error
        elif isinstance(exc, (_OutcomeUnknown, _ChildCancelled)):
            self.owe(exc.caller_cancelled)
        elif isinstance(exc, asyncio.CancelledError):
            self.owe(exc)
            return
        if self.error is None:
            self.error = exc

    def unsafe(self, problem: str, *, already_recorded: bool = False,
               incident: _Incident | None = None) -> _Incident:
        """This step left the connection in a state we cannot vouch for.

        Pass `incident` to report a FOLLOW-ON observation of a failure already
        reported here: the transaction still being open because the rollback
        could not be resolved is one incident described twice, not two
        failures. Everything else is an incident of its own — including a
        problem that reads exactly like another one, which is why the identity
        is an object rather than the message.

        `already_recorded` says the fail-stop for a NEW incident is already on
        the record — _submit latches when it registers an unresolved
        submission — so settle() describes it without recording it a second
        time. It is passed explicitly, from the exception that carried the
        incident, rather than inferred by comparing message text.

        Returns the incident, so the caller can attach its follow-on."""
        if incident is None:
            incident = _Incident(recorded=already_recorded)
        self.problems.append(_Problem(problem, incident))
        return incident

    def take_problems(self, other: "_Outcome") -> None:
        """Adopt another outcome's safety problems EXACTLY as they stand.

        A copy, not a re-derivation: each problem already knows whether its
        fail-stop is on the record, so nothing has to be inferred from the
        message text — and two problems that read the same but differ in that
        one respect stay two distinct records."""
        self.problems.extend(other.problems)

    @property
    def reasons(self) -> list[str]:
        return [p.message for p in self.problems]

    @property
    def unrecorded(self) -> list[str]:
        """The problems whose fail-stop is NOT yet on the record."""
        return [p.message for p in self.problems if not p.recorded]

    def _by_incident(self) -> list[tuple[_Incident, list[str]]]:
        """Every problem grouped under the incident it belongs to, in the order
        the incidents were first observed.

        Grouped by IDENTITY. Grouping by message would merge two separate
        failures that happen to read the same, and splitting per problem would
        report a single failure once for the rollback that could not be
        resolved and again for the transaction that is open because of it."""
        order: list[_Incident] = []
        grouped: dict[int, list[str]] = {}
        for problem in self.problems:
            key = id(problem.incident)
            if key not in grouped:
                order.append(problem.incident)
                grouped[key] = []
            grouped[key].append(problem.message)
        return [(incident, grouped[id(incident)]) for incident in order]

    @property
    def resolved(self) -> bool:
        return self.error is None and not self.problems

    def settle(self) -> BaseException | None:
        """The single exception this operation owes its caller, or None.

        Safety outranks the error (an unclosed cursor is restart-required
        whatever else went wrong), and the cancellation outranks both. Neither
        of the two it outranks is DISCARDED, though: the error that broke the
        statement rides along as the cause, because an operator reading a
        restart-required database otherwise sees the cleanup failure with
        nothing to say what caused it."""
        if self.problems:
            reason = "; ".join(self.reasons)
            grouped = self._by_incident()
            fresh = [messages for incident, messages in grouped
                     if not incident.recorded]
            known = [messages for incident, messages in grouped
                     if incident.recorded]
            if not fresh and _fail_stop is None:
                # Nothing here is new, yet the database is not on the record at
                # all. The latch has to happen or the next operation is
                # admitted over a connection nobody can vouch for.
                _enter_fail_stop(reason)
            for messages in fresh:
                # ONE event per distinct incident. Joining them reported
                # several separate failures as a single one, and re-latching
                # the whole set would report an incident that has already been
                # recorded as if it were a second, later one.
                _enter_fail_stop("; ".join(messages))
            for messages in known:
                # An incident _submit already latched — a rollback whose
                # outcome nobody could establish, and the transaction still
                # open BECAUSE of it. The follow-on observation is what an
                # operator needs to read, so it is reported as detail of that
                # incident rather than dropped; what it must not do is count as
                # a second failure. Reported whether or not something else here
                # is new: the two halves are independent facts, and handling
                # only one of them is how a mixed ending lost its other half.
                logger.error("DB fail-stop detail (same incident, not a "
                             "further failure): %s", "; ".join(messages))
            failure: BaseException = DatabaseFailStop(
                f"database is restart-required: {reason}")
            if self.error is not None:
                # Both carriers are PUBLIC. A private classification here would
                # put this module's internal vocabulary in front of a caller as
                # the direct cause, and its repr into the message an operator
                # reads — the exception it is attached to already says the
                # outcome was unresolved.
                failure.__cause__ = _public_cause(self.error)
                failure.args = (
                    f"{failure.args[0]}; after {_public_detail(self.error)}",)
            return _caller_owed(self.cancelled, failure)
        if self.error is not None:
            return _caller_owed(self.cancelled, self.error)
        return self.cancelled


# The three classifications this module reasons with internally. NONE of them
# may reach a caller — not as the exception raised, and not as its direct
# cause, which is the first thing an `except ... as exc: exc.__cause__` reads.
_PRIVATE_CLASSIFICATIONS = (_OutcomeUnknown, _ChildCancelled,
                            _CancelledAfterWorkerError)


def _is_private(exc: BaseException | None) -> bool:
    return isinstance(exc, _PRIVATE_CLASSIFICATIONS)


def _public_reason(exc: BaseException) -> BaseException:
    """The public exception that stands IN PLACE of a private classification.

    Used where the exception being handed back says nothing on its own — a bare
    CancelledError, or a private classification being replaced outright. What a
    cancellation carries is not always a worker error: when the worker never
    answered at all it carries an unresolved-outcome classification, and that
    says something different — the worker did not fail, its outcome is unknown —
    so the stand-in names that instead."""
    if isinstance(exc, _CancelledAfterWorkerError):
        return _public_reason(exc.worker_error)
    if isinstance(exc, (_OutcomeUnknown, _ChildCancelled)):
        return DatabaseFailStop(
            _fail_stop or f"worker outcome unresolved: {exc}")
    return exc


def _public_cause(exc: BaseException | None) -> BaseException | None:
    """What may be attached as a DIRECT cause, or None when nothing may be.

    A worker error rides along — that is the fact a caller needs, and it is the
    reason `cancelled; the worker then failed` is not just a cancellation. An
    unresolved outcome does NOT get a stand-in cause: the exception it would be
    attached to is already a DatabaseFailStop saying the outcome was
    unresolved, and hanging a second one off it only makes the private object
    look as though it had been replaced by something informative. The private
    object stays reachable as implicit context; it is never the cause."""
    if exc is None:
        return None
    if isinstance(exc, _CancelledAfterWorkerError):
        return _public_cause(exc.worker_error)
    if isinstance(exc, (_OutcomeUnknown, _ChildCancelled)):
        return None
    return exc


def _public_detail(exc: BaseException) -> str:
    """How an exception may be DESCRIBED in text a caller or an operator reads.

    `repr()` of a private classification names the class, so a fail-stop reason
    built from it publishes the vocabulary the translation exists to hide."""
    if isinstance(exc, _CancelledAfterWorkerError):
        return ("cancelled; the worker then failed with "
                f"{_public_detail(exc.worker_error)}")
    if isinstance(exc, (_OutcomeUnknown, _ChildCancelled)):
        return f"worker outcome unresolved: {exc}"
    return repr(exc)


def _relink(exc: BaseException, attribute: str,
            value: BaseException | None) -> None:
    """Point one link of `exc` at `value` WITHOUT changing what it displays.

    Assigning __cause__ sets __suppress_context__ in CPython — including
    `exc.__cause__ = None`. A scrub that re-assigned the cause of every public
    link it walked therefore suppressed the context of exceptions it had
    nothing to change: the object stayed reachable, so the graph still looked
    right, while traceback.format_exception stopped printing the "During
    handling of the above exception" section that named the real inner failure.

    So: assign only when the link actually changes, and put the display flag
    back either way. Clearing a PRIVATE cause is a change we do mean to make;
    hiding whatever the exception was already showing is not."""
    if getattr(exc, attribute) is value:
        return
    suppress = exc.__suppress_context__
    setattr(exc, attribute, value)
    exc.__suppress_context__ = suppress


def _public_graph(exc: BaseException | None,
                  seen: dict | None = None) -> BaseException | None:
    """`exc` with every private classification ANYWHERE in its cause/context
    graph replaced by what may stand in its place.

    One link is never enough. A DatabaseFailStop raised `from` a private
    classification carries it as the direct cause; `raise` inside an
    `except <private>` block stamps it onto __context__ of the very exception
    being handed out; and either of those can sit two links down behind a
    perfectly public exception. traceback.format_exception() walks the whole
    chain and prints every link by class name, so ONE private object anywhere
    in the graph publishes the vocabulary this translation exists to hide —
    whether or not any caller ever isinstance()-checks it.

    Only PRIVATE links are replaced. A public cause or context is diagnostic an
    operator reads, and blanking it to be safe would hide why the failure
    happened at all. What a private link was carrying is promoted into the
    place it vacates rather than dropped with it."""
    if exc is None:
        return None
    if seen is None:
        seen = {}
    if id(exc) in seen:
        # Already rewritten, or a cycle: __context__ chains can loop, and
        # walking one twice would not terminate.
        return seen[id(exc)]
    if not _is_private(exc):
        seen[id(exc)] = exc
        _relink(exc, "__cause__", _public_graph(exc.__cause__, seen))
        _relink(exc, "__context__", _public_graph(exc.__context__, seen))
        return exc
    # _public_cause, not _public_reason: an unresolved outcome deliberately
    # gets NO stand-in, because the exception it hangs off already says the
    # outcome was unresolved and a second one only makes the private object
    # look as though it had been replaced by something informative.
    replacement = _public_cause(exc)
    seen[id(exc)] = replacement
    if replacement is not None and id(replacement) not in seen:
        # The stand-in is new to the graph, so it takes the private object's
        # place outright and the links that object was carrying fill the slots
        # the stand-in does not already use.
        seen[id(replacement)] = replacement
        for attribute in ("__cause__", "__context__"):
            link = getattr(replacement, attribute)
            if link is None:
                link = getattr(exc, attribute)
            carried = _public_graph(link, seen)
            # A cancellation carrying a worker error stands in AS that error,
            # and `raise ... from error` made the error its own cause too.
            _relink(replacement, attribute,
                    None if carried is replacement else carried)
        return replacement
    # Either there is no stand-in at all, or the stand-in is ALREADY in the
    # scrubbed graph: `_raise_from_private` names the worker error as the
    # fail-stop's cause, and the wrapper that carries the same worker error is
    # that fail-stop's context, so the scrub meets the stand-in one slot before
    # it meets the wrapper. Handing the stand-in back a second time tells an
    # operator nothing they cannot already read at the cause — and it costs the
    # OTHER branch: whatever was being handled when the wrapper was raised is
    # reachable through the wrapper and nowhere else, so it used to leave the
    # graph with it. The slot the wrapper vacates keeps that branch instead.
    #
    # BOTH links are read, never `exc.__cause__ or exc.__context__`. A private
    # object has two of them and stands in for at most one, so reading only the
    # link that happens to be filled first is the same defect in the branch
    # that has no stand-in to fall back on.
    kept: BaseException | None = None
    for attribute in ("__cause__", "__context__"):
        carried = _public_graph(getattr(exc, attribute), seen)
        if carried is None or carried is replacement or carried is kept:
            continue
        if kept is None:
            kept = carried
        elif kept.__context__ is None:
            # Two distinct public branches and one slot. This module raises no
            # private object shaped like that — `raise <private>(e) from e` and
            # the bare `raise <private>(...)` each leave exactly one branch
            # beside the stand-in — so this is a floor, not a path: chain the
            # second under the first rather than drop it, and only into a slot
            # that is still free so nothing already scrubbed is overwritten.
            _relink(kept, "__context__", carried)
    if kept is not None:
        replacement = kept
        seen[id(exc)] = replacement
    return replacement


def _as_public(exc: BaseException) -> BaseException:
    """Translate this module's private classifications into something a caller
    outside it can act on.

    `_CancelledAfterWorkerError` is already a CancelledError subclass, so
    `except asyncio.CancelledError` matches it — but the TYPE is private
    vocabulary. Code that inspects the exception, re-raises it into another
    framework, or simply compares types must not have to know it exists. The
    reason survives as __cause__ and in the log; it is never dropped."""
    if isinstance(exc, _CancelledAfterWorkerError):
        cause = _public_reason(exc.worker_error)
        logger.warning("database operation cancelled; reason: %r", cause)
        public: BaseException = asyncio.CancelledError()
        public.__cause__ = cause
        return public
    if isinstance(exc, (_OutcomeUnknown, _ChildCancelled)):
        if exc.caller_cancelled is not None:
            # A bare CancelledError says nothing about why, so the reason is
            # attached rather than dropped.
            public = asyncio.CancelledError()
            public.__cause__ = _public_reason(exc)
            return public
        # The stand-in already IS the reason; giving it a cause that repeats
        # itself adds nothing, and giving it the private object adds a type the
        # caller is not supposed to know.
        return _public_reason(exc)
    return exc


def _raise_public(exc: BaseException) -> None:
    """Re-raise `exc` in its public form without disturbing what is already
    attached to it.

    Two ways to get this wrong, both of which cost the caller the real reason:
    `raise exc from None` when nothing needed translating clears __cause__ and
    suppresses __context__ on the very object being raised; and `raise public
    from exc` when something DID overwrites the cause the translation just
    chose with this module's private wrapper. Translating twice to decide
    which is worse still — translating logs, so one cancellation is reported
    as two.

    Every path out of this module that has to hand back a translated exception
    goes through here, so there is one place that knows all of it."""
    public = _public_form(exc)
    if public is exc:
        raise exc
    # NOT `raise public from exc`. _public_form has already chosen this
    # exception's cause, and for a cancellation carrying a worker error that
    # cause IS the worker error; `from` would overwrite it with the private
    # wrapper the translation exists to hide, leaving a caller with nothing to
    # follow but a class it is not supposed to know about. The wrapper stays
    # reachable as implicit context.
    raise public


def _already_fail_stopped(exc: BaseException) -> bool:
    """True when RAISING this exception already latched a fail-stop.

    _submit latches inside _register_unresolved before it raises either of
    these, so a caller that turns one into a safety problem is describing an
    incident that is already on the record — recording it again would report
    one failure as two."""
    return isinstance(exc, (_OutcomeUnknown, _ChildCancelled))


def _reaches(start: BaseException | None, target: BaseException) -> bool:
    """Is `target` anywhere in `start`'s cause/context graph?"""
    seen, stack = set(), [start]
    while stack:
        node = stack.pop()
        if node is None or id(node) in seen:
            continue
        if node is target:
            return True
        seen.add(id(node))
        stack.append(node.__cause__)
        stack.append(node.__context__)
    return False


def _carry_raised_from(public: BaseException, exc: BaseException) -> None:
    """Keep the OBJECT the private classification was raised `from`.

    `_as_public` builds the stand-in out of what the private object was
    CARRYING — for a cancellation that is `worker_error`, and the durable-COMMIT
    path makes that a fail-stop naming the reason. The exception the private
    object was raised FROM is a different fact and a different object: the
    driver's own error, holding the frame it was raised in, its notes, and its
    own cause and context. `_public_detail` quotes its repr() into the
    fail-stop's message, and a quote is NOT that object — nobody can read a
    traceback, a note or a __cause__ back out of a string. Dropping it means the
    one exception that knows WHERE the failure happened never reaches the
    caller.

    It goes one link below the stand-in, which is where it belongs: the
    fail-stop exists BECAUSE of that error. Only into a slot that is still free,
    never naming the stand-in itself, and never closing a loop — this transplant
    fills an empty place, it does not rearrange a graph."""
    carried = exc.__cause__
    if carried is None:
        return
    # The stand-in's own reason when it has one (a bare CancelledError says
    # nothing, so the fail-stop under it is what the error explains), else the
    # stand-in itself.
    reason = public.__cause__ if public.__cause__ is not None else public
    if reason is carried or reason.__cause__ is not None:
        # Already carrying something of its own, or already IS the object —
        # `raise _CancelledAfterWorkerError(error) from error` makes the
        # stand-in and the raised-from object the same exception.
        return
    if _reaches(carried, reason):
        return
    _relink(reason, "__cause__", carried)


def _public_form(exc: BaseException) -> BaseException:
    """_as_public, but safe to `raise` directly.

    Translating in place would otherwise cost the diagnostic chain: a bare
    `raise translated from exc.__cause__` sets __cause__ to None whenever the
    original had none, and that also SUPPRESSES the implicit context, so the
    traceback loses where the failure came from.

    The original is NEVER attached back as the cause. _as_public hands back a
    different object only for a private classification — for anything already
    public it returns the identical one — so `exc` here is always private, and
    naming it as the cause would undo the whole translation and hand the caller
    the very type it exists to hide, one link down. That invariant is pinned by
    test_as_public_hands_back_the_same_object_for_a_public_exception; without
    it the transplant below would overwrite a public exception's OWN context."""
    public = _as_public(exc)
    if public is not exc:
        if public.__context__ is None:
            # The stand-in replaces the private object outright, so anything
            # that object was carrying would vanish with it — _public_graph
            # never sees it, because it is already gone from the graph by the
            # time we get here. A public context attached before we ever saw it
            # is diagnostic an operator reads: it is transplanted onto the
            # stand-in, and _public_graph below scrubs it in turn.
            public.__context__ = exc.__context__
        _carry_raised_from(public, exc)
    return _public_graph(public)


def _raise_from_private(failure: BaseException, exc: BaseException) -> None:
    """Raise `failure` for an incident classified by `exc`.

    `raise failure from exc` is right when `exc` is public and wrong when it is
    not: it would publish a private classification as the direct cause, which
    is where a caller looks first. The private object stays reachable as
    implicit context either way."""
    if _is_private(exc):
        cause = _public_cause(exc)
        if cause is None:
            raise failure
        raise failure from cause
    raise failure from exc


def _enter_fail_stop(reason: str) -> None:
    """Latch the database into restart-required.

    The alternative — discard the connection and open a fresh one — is
    deliberately rejected: when a terminal outcome is unknown, the fate of the
    pending statement is unknown too, so a replacement connection would keep
    writing the same file next to a transaction that may still be open (and
    whose orphaned INSERT could ride a later commit). Refusing every subsequent
    operation is the only honest answer a process can give."""
    global _fail_stop
    if _fail_stop is None:
        _fail_stop = f"database is restart-required: {reason}"
        logger.error("DB fail-stop engaged — refusing further operations: %s", reason)
    else:
        # The latch keeps the FIRST reason (it is the one that made the state
        # unknown), but every later one is still a fact about this database and
        # is the only record an operator will have of it.
        logger.error("DB already fail-stopped; additional reason: %s", reason)


def _fail_stop_now(reason: str) -> DatabaseFailStop:
    """Latch, and describe THIS failure.

    The latch deliberately keeps the first reason ever recorded — that is the
    admission state. The exception a caller receives has to be about the
    operation it just ran, or the second failure in a process is reported with
    the first one's text."""
    _enter_fail_stop(reason)
    return DatabaseFailStop(f"database is restart-required: {reason}")


def _register_unclosed(conn) -> None:
    """Record a connection whose close could not be confirmed, ONCE.

    Several failure paths can reach the same connection — an unreadable
    transaction state during cleanup, then a close that cannot be confirmed
    during shutdown — and readiness() serves len(_unclosed) verbatim as
    "unclosed_connections". Appending twice reports two connections in trouble
    when there is one."""
    if not any(c is conn for c in _unclosed):
        _unclosed.append(conn)


def _worker_alive(conn) -> bool:
    """True while this connection's aiosqlite worker THREAD is still running.

    NOT `_running`, and NOT `task.cancelled()`. aiosqlite clears `_running`
    before the thread is gone in both directions that matter here: a cancelled
    connect clears it while the worker is still blocked inside sqlite3.connect,
    and close() clears it having only QUEUED a stop sentinel the worker has not
    reached yet. The non-daemon thread is what keeps the process alive and what
    can still be touching the database file, so the thread is the question."""
    if conn is None:
        return False
    if getattr(conn, "ident", None) is None:
        return False              # never started: there is no worker
    try:
        return bool(conn.is_alive())
    except BaseException:  # noqa: BLE001 — an unreadable thread is not proof it left
        return True


# Joining a worker blocks, so it has to leave the event loop — but NOT through
# asyncio.to_thread, which submits to the loop's SHARED default executor.
# Ordinary application code parks that pool: alert_service wraps a blocking
# getaddrinfo in to_thread under a wait_for, and wait_for cancels the AWAIT
# while the OS resolver keeps the pool thread for tens of seconds. A join
# queued behind that is unbounded — with the operation lock held — so the close
# deadline would stop being a wall-clock bound at exactly the wrong moment.
# Waiting for a worker thread to exit is a WATCH, not a job. Both ways of
# handing it to an executor were tried here and both failed, differently:
#
#   * the loop's SHARED default executor (asyncio.to_thread) is contended by
#     ordinary application code — alert_service parks a pool thread inside
#     getaddrinfo for as long as the resolver takes — so a join can wait a long
#     time for a slot that has nothing to do with the database;
#   * a DEDICATED single-thread pool removes that contention but adds its own:
#     cancelling the await does not stop the submitted job, so the next join
#     queues behind work whose caller has gone away and cannot even look at its
#     own target until that clears, then answers at its deadline rather than at
#     the moment the thread actually exits.
#
# Thread.is_alive() does not block, so the event loop can watch the thread
# directly: no slot to wait for, nothing left running behind us, and the answer
# arrives as soon as it is true.
#
# How often to look: small enough that a shutdown is not padded by it, large
# enough that a long join is not a busy loop.
_JOIN_POLL_INTERVAL = 0.005


async def _join_worker(conn, timeout: float) -> bool:
    """Wait for one aiosqlite worker thread to exit, inside an absolute
    wall-clock bound — and return the moment it does.

    Nothing here can be delayed by another join, by application code sharing a
    thread pool, or by work whose caller was cancelled: the only state consulted
    is the thread's own liveness, and the only bound is this call's deadline."""
    if not _worker_alive(conn):
        return True
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        await asyncio.sleep(min(left, _JOIN_POLL_INTERVAL))
        if not _worker_alive(conn):
            return True
    return not _worker_alive(conn)


def _register_unresolved(what: str, task: "asyncio.Task",
                         disposal: _Disposal = _Disposal.NONE,
                         owner: object = None) -> None:
    """Remember a submission whose worker outcome we could not recover.

    We do NOT cancel it: cancelling the future does not stop the worker thread,
    it only hides that the statement is still running. Keeping the reference —
    with what it will hand back if it ever finishes — and fail-stopping is what
    makes the unknown visible."""
    _unresolved.append(_Unresolved(what, task, disposal, owner))
    _enter_fail_stop(f"{what}: worker outcome unresolved")


def adjudicate_unresolved() -> list[dict]:
    """Classify submissions that were unresolved and have since finished.

    A submission is removed from the registry ONLY once its real outcome can be
    read — completed, failed, or cancelled. One still running stays, because a
    live worker with an unknown outcome is exactly what restart-required means.

    Removal is a TRANSFER, not a discard: a submission that completed holding a
    resource (a connection, a cursor) moves to _late_results, because there is
    no caller left to close it and reading the result to classify it is not the
    same as owning it. Only a confirmed close retires it from there.

    This never clears fail-stop: the window in which the outcome was unknown
    already happened, and no later observation undoes it."""
    settled: list[dict] = []
    still_running: list[_Unresolved] = []
    for entry in _unresolved:
        what, task, disposal = entry.what, entry.task, entry.disposal
        if not task.done():
            still_running.append(entry)
            continue
        if task.cancelled():
            # OUR future was cancelled. That says nothing about the worker: the
            # thread keeps executing whatever it was handed, and for a connect
            # aiosqlite even clears `_running` while the connector is still
            # blocked inside sqlite3.connect. Retiring the entry here is how a
            # live thread ends up unaccounted for while close_db says CLOSED.
            if _worker_alive(entry.owner):
                still_running.append(entry)
                continue
            settled.append({"what": what, "outcome": "cancelled",
                            "detail": "owner worker thread has exited"})
            continue
        error = task.exception()
        if error is not None:
            settled.append({"what": what, "outcome": "failed", "detail": repr(error)})
            continue
        result = task.result()
        if disposal is _Disposal.NONE or result is None:
            settled.append({"what": what, "outcome": "completed", "detail": None})
            continue
        # The worker handed back a LIVE resource — a connection with its own
        # non-daemon thread, or a cursor pinning a read snapshot — and there is
        # no caller left to close it. Reading the result and letting it fall
        # out of scope is the leak: ownership moves here, and only a confirmed
        # close retires it.
        _late_results.append(_LateResult(what, disposal, result, entry.owner))
        settled.append({"what": what, "outcome": "completed",
                        "detail": f"late {disposal.value} awaiting disposal"})
    _unresolved[:] = still_running
    for entry in settled:
        logger.error("unresolved worker submission settled: %s -> %s %s",
                     entry["what"], entry["outcome"], entry["detail"] or "")
    return settled


async def _dispose_late_results(
    timeout: float, disposals: "tuple[_Disposal, ...] | None" = None
) -> None:
    """Close whatever the workers handed back after their callers gave up.

    Bounded by ONE deadline shared across every late result, for the same
    reason close_db has one: an unresponsive close must not be able to multiply
    the shutdown budget. A close that cannot be confirmed keeps the resource
    registered — a connection in _unclosed, anything else still here — so
    CLOSED stays impossible while it is alive.

    `disposals` restricts the pass to one kind, because the two kinds sit on
    different workers: a late CURSOR belongs to a connection that is still
    published, so it has to be closed BEFORE that connection is (a cursor
    closed after its connection can never be confirmed at all), while a late
    CONNECTION owns its own worker and must not be able to hold up closing the
    healthy ones."""
    if not _late_results:
        return
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, timeout)
    if disposals is None:
        pending, _late_results[:] = list(_late_results), []
    else:
        pending = [e for e in _late_results if e.disposal in disposals]
        _late_results[:] = [e for e in _late_results if e.disposal not in disposals]
    for entry in pending:
        what = f"{entry.what}: late {entry.disposal.value} close"
        try:
            await _submit(entry.obj.close(), what,
                          timeout=max(0.0, deadline - loop.time()),
                          owner=entry.owner)
        except BaseException as exc:  # noqa: BLE001 — every ending is recorded
            if entry.disposal is _Disposal.CURSOR and _cursor_already_gone(exc):
                # SQLite drops every cursor with its connection, and aiosqlite
                # refuses to queue on a stopped wrapper. Either answer is proof
                # the cursor is gone — a confirmed close, not a failure.
                logger.warning("late cursor was already dropped with its "
                               "connection: %s", entry.what)
                continue
            if _already_fail_stopped(exc):
                # _submit latched when it registered the unresolved
                # submission. The close we cannot confirm IS that submission,
                # so recording it here would report one incident as two.
                logger.error("%s: late %s close is the submission already "
                             "recorded as unresolved: %s",
                             entry.what, entry.disposal.value,
                             _public_detail(exc))
            else:
                _enter_fail_stop(
                    f"{entry.what}: late {entry.disposal.value} could not be "
                    f"closed: {_public_detail(exc)}")
            if entry.disposal is _Disposal.CONNECTION:
                _register_unclosed(entry.obj)
            else:
                _late_results.append(entry)
        else:
            gone = True
            if entry.disposal is _Disposal.CONNECTION:
                try:
                    gone = await _join_worker(
                        entry.obj, max(0.0, deadline - loop.time()))
                except BaseException as exc:  # noqa: BLE001
                    gone = False
                    logger.warning("late connection join failed: %r", exc)
            if not gone:
                _enter_fail_stop(
                    f"{entry.what}: late connection closed but its aiosqlite "
                    "worker thread did not exit")
                _register_unclosed(entry.obj)
            else:
                logger.error("closed a late %s that was returned after its caller "
                             "gave up: %s", entry.disposal.value, entry.what)


def readiness() -> dict:
    """Structured storage readiness, from IN-PROCESS state only.

    Deliberately submits nothing to a connection: a readiness probe that runs
    SQL would hang exactly when the database is wedged, which is the one moment
    it has to answer."""
    unresolved = len(_unresolved)
    unclosed = len(_unclosed)
    late = len(_late_results)
    if _fail_stop is not None:
        state, reason = "fail_stop", _fail_stop
    elif _closed:
        state, reason = "closed", "database is closed"
    elif _closing:
        state, reason = "closing", "database is shutting down"
    elif unresolved:
        state, reason = "unresolved_workers", (
            f"{unresolved} worker submission(s) with no established outcome")
    elif unclosed:
        state, reason = "unclosed_connections", (
            f"{unclosed} connection(s) whose close was never confirmed")
    elif late:
        state, reason = "late_results", (
            f"{late} resource(s) returned by a worker after their caller gave up")
    else:
        state, reason = "ok", None
    return {
        "status": "ready" if state == "ok" else "not_ready",
        "database": state,
        "reason": reason,
        "restart_required": bool(
            _fail_stop is not None or unresolved or unclosed or late),
        "unresolved_workers": unresolved,
        "unclosed_connections": unclosed,
        "late_results": late,
    }


def _require_admission() -> None:
    """Gate EVERY database operation, read as well as write.

    Reads go through the same worker and the same connection, so admitting them
    after a terminal outcome went unknown would keep using a connection whose
    state nobody can vouch for."""
    if _fail_stop is not None:
        raise DatabaseFailStop(_fail_stop)
    if _closed:
        raise DatabaseFailStop("database is closed")
    if _closing:
        raise DatabaseFailStop("database is closing")


def db_fail_stop_reason() -> str | None:
    """Why operations are being refused, or None while the DB is healthy."""
    return _fail_stop


class _WorkerOutcome(NamedTuple):
    """A recovered worker result, plus the cancellation we still owe our caller
    if one arrived while the worker was busy."""

    result: object
    caller_cancelled: asyncio.CancelledError | None


async def _submit(coro, what: str, timeout: float | None = None,
                  disposal: _Disposal = _Disposal.NONE,
                  owner: object = None) -> _WorkerOutcome:
    """Hand ONE operation to the aiosqlite worker and recover its terminal
    outcome, whatever happens to us.

    Three distinct endings, deliberately not collapsed into one:
      * the worker finished          -> return its result (plus any cancellation
                                        we owe the caller, to raise AFTER the
                                        boundary has cleaned up)
      * the submission came back
        cancelled without us asking  -> _ChildCancelled: NOT a terminal outcome
      * we ran out of budget         -> _OutcomeUnknown, submission registered

    Both endings that give up carry the cancellation we still owe, so nothing
    downstream can turn "you were cancelled" into "the database is refusing".

    `disposal` says what this submission's result OWNS, so a result that lands
    after we stopped waiting can still be closed instead of dropped, and
    `owner` names the connection whose worker thread is running it — the only
    thing that can later answer whether the submission is really over.

    We never cancel the submission ourselves: that would not stop the worker,
    only make an unknown look resolved."""
    task = asyncio.ensure_future(coro)
    caller_cancelled: asyncio.CancelledError | None = None
    loop = asyncio.get_running_loop()
    # An ABSOLUTE deadline, not a per-turn allowance. The retry allowance exists
    # only so a cancellation cannot end the recovery early; re-spending the
    # timeout on every turn would multiply it by _CLEANUP_CANCEL_BUDGET, and the
    # operation lock is held for all of it — so an unresponsive worker would
    # stall every read and write in the process for that whole time, and
    # close_db's single deadline would be multiplied at the close stage.
    deadline = loop.time() + (_CLEANUP_TIMEOUT if timeout is None else max(0.0, timeout))
    for _ in range(_CLEANUP_CANCEL_BUDGET):
        try:
            await asyncio.wait({task}, timeout=max(0.0, deadline - loop.time()))
        except asyncio.CancelledError as exc:
            # Only a cancellation earns another turn: the worker is still
            # running and its outcome is still worth recovering — within the
            # SAME deadline.
            caller_cancelled = exc
            if not task.done():
                continue
        # Otherwise the worker finished, or the deadline is spent. Waiting again
        # would re-enter with no time left and change nothing.
        break
    if not task.done():
        _register_unresolved(what, task, disposal, owner)
        raise _OutcomeUnknown(what, caller_cancelled)
    if task.cancelled():
        _register_unresolved(what, task, disposal, owner)
        raise _ChildCancelled(what, caller_cancelled)
    error = task.exception()
    if error is not None:
        if caller_cancelled is not None:
            # Never let a worker error overwrite a cancellation we still owe.
            raise _CancelledAfterWorkerError(error) from error
        raise error
    return _WorkerOutcome(task.result(), caller_cancelled)


def _cursor_already_gone(exc: BaseException) -> bool:
    """SQLite drops every cursor when its connection closes, and aiosqlite
    refuses to queue anything on a stopped wrapper. Either message is PROOF the
    cursor is gone — a confirmed close, not an unresolved one."""
    if isinstance(exc, sqlite3.ProgrammingError):
        return True
    return isinstance(exc, ValueError) and "closed" in str(exc).lower()


async def _close_cursor(cursor, owner=None) -> asyncio.CancelledError | None:
    """Close one cursor to a definite outcome.

    Phase 0 CP-1-RECOVER-CLOSE: a half-consumed cursor pins a read snapshot
    that blocks WAL checkpointing and later writes on its own, independently of
    the transaction. An unconfirmed close is therefore as fatal as an
    unconfirmed rollback."""
    try:
        outcome = await _submit(cursor.close(), "cursor close",
                                owner=owner if owner is not None
                                else getattr(cursor, "_conn", None))
    except BaseException as exc:
        if _cursor_already_gone(exc):
            logger.debug("cursor already closed with its connection", exc_info=True)
            return None
        raise
    return outcome.caller_cancelled


async def _rollback(conn) -> asyncio.CancelledError | None:
    outcome = await _submit(conn.rollback(), "rollback", owner=conn)
    return outcome.caller_cancelled


# Applied to EVERY connection, then read back. A PRAGMA can silently not take
# effect (a WAL switch can fail on some filesystems, and foreign_keys is a
# connection-local setting that is OFF by default), so the applied value is
# verified rather than assumed.
def _journal_mode_ok(value) -> bool:
    """WAL on a file database; `memory` is the correct answer for the
    shared-cache in-memory database tests use — SQLite cannot put that in WAL,
    so reporting `memory` is a successful PRAGMA, not a failed one."""
    mode = str(value).lower()
    if _db_path == ":memory:":
        return mode in ("memory", "wal")
    return mode == "wal"


_PRAGMA_SETUP = (
    ("PRAGMA journal_mode=WAL", "PRAGMA journal_mode", _journal_mode_ok),
    ("PRAGMA busy_timeout=5000", "PRAGMA busy_timeout",
     lambda v: int(v) == 5000),
    ("PRAGMA foreign_keys=ON", "PRAGMA foreign_keys",
     lambda v: int(v) == 1),
)


async def _run_statement(conn, sql: str, what: str):
    """Submit one statement, drain it, close its cursor. Returns
    (rows, cancellation_owed).

    Every stage reports into ONE outcome. The previous shape kept the
    cancellation in a local that the `finally` overwrote whenever the cursor
    close raised, so a caller cancelled during the execute was handed the close
    error instead."""
    outcome = _Outcome()
    cursor, rows = None, []
    try:
        submitted = await _submit(conn.execute(sql), what,
                                  disposal=_Disposal.CURSOR, owner=conn)
        outcome.owe(submitted.caller_cancelled)
        cursor = submitted.result
        fetched = await _submit(cursor.fetchall(), f"{what} fetch", owner=conn)
        # Recorded BEFORE the rows are materialised: list() runs the driver's
        # row factory, and a row that cannot be built would otherwise take the
        # cancellation down with it.
        outcome.owe(fetched.caller_cancelled)
        rows = list(fetched.result)
    except BaseException as exc:  # noqa: BLE001 — recorded, never dropped
        outcome.absorb(exc)
    if cursor is not None:
        try:
            outcome.owe(await _close_cursor(cursor, owner=conn))
        except BaseException as exc:  # noqa: BLE001
            outcome.absorb(exc)
            outcome.unsafe(f"{what}: cursor close: {_public_detail(exc)}",
                           already_recorded=_already_fail_stopped(exc))
    if not outcome.resolved:
        raise outcome.settle()
    return rows, outcome.cancelled


async def _configure(conn: aiosqlite.Connection) -> asyncio.CancelledError | None:
    """Apply and VERIFY the connection PRAGMAs.

    Returns any cancellation the caller is still owed rather than dropping it —
    a cancellation swallowed here would leave the caller believing its operation
    completed. Raises if any setting did not actually take, so the caller can
    refuse to publish the connection.

    Accumulated, not carried in a local: a PRAGMA that reads back wrong raises
    over everything collected so far, and the cancellation collected from an
    EARLIER pragma is owed just the same."""
    outcome = _Outcome()
    conn.row_factory = aiosqlite.Row
    try:
        for apply_sql, read_sql, accepts in _PRAGMA_SETUP:
            _, c = await _run_statement(conn, apply_sql, f"connect {apply_sql}")
            outcome.owe(c)
            rows, c = await _run_statement(conn, read_sql, f"connect {read_sql}")
            outcome.owe(c)
            value = rows[0][0] if rows else None
            try:
                accepted = accepts(value)
            except (TypeError, ValueError):
                accepted = False
            if not accepted:
                raise RuntimeError(
                    f"{read_sql} read back {value!r}; refusing to publish this connection"
                )
    except BaseException as exc:
        outcome.absorb(exc)
        failure = outcome.settle()
        if failure is exc or failure is None:
            raise
        _raise_from_private(failure, exc)
    return outcome.cancelled


async def _discard_connection(conn, what: str) -> "_Outcome":
    """Close a connection that will never be published, within a bound.

    A connection created but not published owns an aiosqlite worker — a
    non-daemon thread. If its close cannot be confirmed, that thread is
    unaccounted for, which is a restart-required condition, not a shrug.

    Returns the WHOLE outcome: the cancellation the caller is still owed, and
    whether the discard itself could be resolved. A caller that takes only the
    cancellation reports "this connection failed verification" — an ordinary,
    retryable-looking failure — for a database that is actually
    restart-required because a worker is now unaccounted for."""
    outcome = _Outcome()
    try:
        submitted = await _submit(conn.close(), f"{what} discard close", owner=conn)
        outcome.owe(submitted.caller_cancelled)
    except BaseException as exc:  # noqa: BLE001 — every ending is recorded
        outcome.absorb(exc)
        outcome.unsafe(
            f"{what}: unpublished connection could not be closed: "
            f"{_public_detail(exc)}",
            already_recorded=_already_fail_stopped(exc))
    if outcome.resolved:
        # close() only queues a stop sentinel; the thread is what has to go.
        try:
            if not await _join_worker(conn, _CLEANUP_TIMEOUT):
                outcome.unsafe(
                    f"{what}: unpublished connection closed but its aiosqlite "
                    "worker thread did not exit")
        except BaseException as exc:  # noqa: BLE001
            outcome.absorb(exc)
            outcome.unsafe(
                f"{what}: could not confirm the worker thread exited: "
                f"{_public_detail(exc)}",
                already_recorded=_already_fail_stopped(exc))
    if outcome.problems:
        # The RESOURCE is registered here, because no caller may forget it. The
        # REASON is not: both callers fold these problems into their own
        # outcome and latch through settle(), and latching here as well records
        # one failure as two independent facts about the database.
        _register_unclosed(conn)
    return outcome


async def _abandon_unpublished(conn, what: str, reason: str) -> BaseException:
    """Discard a connection that must not be published, and return the single
    exception its caller is owed.

    Two things can come out of the discard besides `reason`: a cancellation it
    collected — owed to the caller ahead of everything else — and a failure,
    which makes this restart-required rather than the ordinary refusal that
    `reason` describes. Dropping either is how a caller ends up being told the
    database merely declined, when a worker is actually unaccounted for."""
    outcome = _Outcome()
    discarded = await _discard_connection(conn, what)
    outcome.owe(discarded.cancelled)
    outcome.take_problems(discarded)
    outcome.absorb(DatabaseFailStop(reason))
    failure = outcome.settle()
    return failure if failure is not None else DatabaseFailStop(reason)


async def _open_connection(what: str) -> aiosqlite.Connection:
    """Build a connection to the point where it is safe to publish: create ->
    configure -> read back -> verify. NOTHING is assigned to a global here.

    Publishing before configuring is the bug this replaces: a connection whose
    `PRAGMA journal_mode=WAL` failed stayed in the global and was reused for the
    life of the process as a DELETE-journal connection on a WAL database."""
    if _closing or _closed:
        raise DatabaseFailStop("database is closing/closed; refusing to open a connection")
    path, uri = _connect_args()
    # Build the wrapper BEFORE awaiting it. aiosqlite.connect() is synchronous —
    # it returns the Connection (a Thread) that `await` then starts — so taking
    # the reference here is what gives the submission an owner even when the
    # await never completes. Without it, a connect that is cancelled or times
    # out leaves a running thread nothing in this process can name.
    wrapper = aiosqlite.connect(path, uri=uri, isolation_level=None)
    _connections.add(wrapper)
    # Recover the connection even if we are cancelled during connect: aiosqlite
    # finishes sqlite3.connect regardless, and a dropped result is a live sqlite
    # resource with no owner.
    outcome = await _submit(wrapper, f"{what} connect",
                            disposal=_Disposal.CONNECTION, owner=wrapper)
    build = _Outcome()
    build.owe(outcome.caller_cancelled)
    conn = outcome.result
    if conn is not wrapper:
        # aiosqlite returns the same object it was given, but the shutdown
        # verdict speaks for what we actually HAVE, not for what we expected.
        _connections.add(conn)
    try:
        build.owe(await _configure(conn))
        if _closing or _closed:
            raise DatabaseFailStop(
                "database began closing while a connection was being built")
        if build.cancelled is not None:
            raise build.cancelled
    except BaseException as exc:
        # The cancellation _submit already recovered from the connect is owed
        # whatever happens next. Raising the verification error over it is the
        # same swallow every other stage of this module was fixed for.
        build.absorb(exc)
        discarded = await _discard_connection(conn, what)
        build.owe(discarded.cancelled)
        # A connection that could neither be published NOR discarded is a
        # restart-required database, not a failed build.
        build.take_problems(discarded)
        failure = build.settle()
        if failure is exc or failure is None:
            raise
        _raise_from_private(failure, exc)
    return conn


@asynccontextmanager
async def _operation():
    """Hold the single DB-file operation lock for one indivisible unit of work.

    A read holds it from execute through the last row to the cursor close; a
    write holds it from BEGIN IMMEDIATE until the commit or rollback is
    confirmed. Nothing else can touch either connection in between, so a cursor
    can never straddle another writer's transaction."""
    if _in_operation.get():
        raise RuntimeError(
            "nested DB operation: the operation lock is not reentrant — use the "
            "handle yielded by the enclosing write_transaction()"
        )
    token = _in_operation.set(True)
    lock = _op_lock
    try:
        await lock.acquire()
    except BaseException:
        _in_operation.reset(token)
        raise
    try:
        yield
    finally:
        lock.release()
        _in_operation.reset(token)


# ── read-only guard for the read helper ──────────────

# PRAGMA is deliberately NOT here. The read helper cannot tell a query PRAGMA
# from a mutating one by shape alone: `PRAGMA user_version = 731`,
# `PRAGMA journal_mode = DELETE` and `PRAGMA wal_checkpoint(TRUNCATE)` all
# change durable state, and the last one does so with no `=` at all. Anything in
# the product that genuinely needs a PRAGMA reads it through
# _fetch_all_unrestricted, which is module-private and used only for
# diagnostics and connection verification.
_READ_ONLY_HEADS = frozenset({"SELECT", "WITH", "EXPLAIN"})
_MUTATING_TOKEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|REPLACE|CREATE|ALTER|DROP|RETURNING|VACUUM"
    r"|ATTACH|DETACH|REINDEX|BEGIN|COMMIT|ROLLBACK|SAVEPOINT|RELEASE)\b",
    re.IGNORECASE,
)


def _require_read_only(sql: str) -> None:
    """Reject anything that can mutate through the read path.

    Without this, `_fetch_all("INSERT ... RETURNING id")` is a fully functional
    write that never opens a transaction and never passes the write boundary."""
    head = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
    if head == "PRAGMA":
        raise ReadOnlyViolation(
            "read helper refuses PRAGMA: a query PRAGMA cannot be told from a "
            f"state-changing one by shape: {sql.strip()[:80]!r}"
        )
    if head not in _READ_ONLY_HEADS or _MUTATING_TOKEN.search(sql):
        raise ReadOnlyViolation(
            f"read helper refuses a statement that can mutate: {sql.strip()[:80]!r}"
        )


class _Result(NamedTuple):
    """What a statement is allowed to hand back — values only, never a cursor."""

    rowcount: int
    lastrowid: int | None


class _TxHandle:
    """Statement handle for one write_transaction boundary.

    Never exposes a raw cursor or connection: every cursor is closed before the
    calling statement returns, and fetches are drained in full, so a caller
    cannot re-create the Phase 0 trigger from inside the boundary.

    Bound to the task that opened the transaction. A handle that escapes to
    another task — a child spawned inside the body, say — would otherwise be
    able to submit a statement after the owner rolled back, writing a row into
    whatever transaction came next."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn
        self._owner = asyncio.current_task()
        self._cursors: list = []
        self._closed = False

    def _track(self, cursor) -> None:
        self._cursors.append(cursor)

    def _take_cursors(self) -> list:
        cursors, self._cursors = self._cursors, []
        return cursors

    async def _release(self, cursor) -> asyncio.CancelledError | None:
        """Close a cursor now. On failure it stays tracked so the boundary's
        cleanup retries it and records the unresolved outcome."""
        cancelled = await _close_cursor(cursor, owner=self._conn)
        try:
            self._cursors.remove(cursor)
        except ValueError:
            pass
        return cancelled

    def _check(self) -> None:
        """Refuse misuse BEFORE anything reaches the worker."""
        if self._closed:
            raise TransactionHandleMisuse(
                "write_transaction handle used after its transaction ended"
            )
        if asyncio.current_task() is not self._owner:
            raise TransactionHandleMisuse(
                "write_transaction handle used by a task other than the one that "
                "opened the transaction"
            )

    async def _run(self, coro, what: str):
        outcome = await _submit(coro, what, disposal=_Disposal.CURSOR,
                                owner=self._conn)
        cursor = outcome.result
        self._track(cursor)
        return cursor, outcome.caller_cancelled

    async def _finish(self, outcome: _Outcome, cursor, value):
        """Release the statement's cursor, then hand the caller exactly one
        ending. A release that raises must not be able to bury the statement's
        own cancellation — nor be waved through as a mere failure, because an
        unreleased cursor pins a read snapshot for the rest of the boundary."""
        if cursor is not None:
            try:
                outcome.owe(await self._release(cursor))
            except BaseException as exc:  # noqa: BLE001
                outcome.absorb(exc)
                outcome.unsafe(
                    f"statement cursor release: {_public_detail(exc)}",
                    already_recorded=_already_fail_stopped(exc))
        failure = outcome.settle()
        if failure is not None:
            _raise_public(failure)
        return value

    async def execute(self, sql: str, params=()) -> _Result:
        self._check()
        outcome = _Outcome()
        cursor, result = None, None
        try:
            cursor, cancelled = await self._run(
                self._conn.execute(sql, params), "execute")
            outcome.owe(cancelled)
            result = _Result(cursor.rowcount, cursor.lastrowid)
        except BaseException as exc:  # noqa: BLE001
            outcome.absorb(exc)
        return await self._finish(outcome, cursor, result)

    async def executemany(self, sql: str, seq) -> _Result:
        self._check()
        outcome = _Outcome()
        cursor, result = None, None
        try:
            cursor, cancelled = await self._run(
                self._conn.executemany(sql, seq), "executemany")
            outcome.owe(cancelled)
            result = _Result(cursor.rowcount, cursor.lastrowid)
        except BaseException as exc:  # noqa: BLE001
            outcome.absorb(exc)
        return await self._finish(outcome, cursor, result)

    async def fetch_all(self, sql: str, params=()) -> list:
        self._check()
        outcome = _Outcome()
        cursor, rows = None, []
        try:
            cursor, cancelled = await self._run(
                self._conn.execute(sql, params), "fetch")
            outcome.owe(cancelled)
            fetched = await _submit(cursor.fetchall(), "fetchall", owner=self._conn)
            outcome.owe(fetched.caller_cancelled)   # before materialising rows
            rows = list(fetched.result)
        except BaseException as exc:  # noqa: BLE001
            outcome.absorb(exc)
        return await self._finish(outcome, cursor, rows)

    async def fetch_one(self, sql: str, params=()):
        rows = await self.fetch_all(sql, params)
        return rows[0] if rows else None


async def _cleanup_cursors(handle: _TxHandle, outcome: "_Outcome") -> None:
    """Close every cursor the boundary still holds, reporting into `outcome`.

    Everything goes through the outcome — the cancellation the caller is still
    owed, AND whether each problem is already on the fail-stop record. A
    cancellation appended to a bare list is one an `except Exception` loop never
    sees; a problem appended to a bare list is one that is either reported
    twice or not at all, depending on what else happens to be in the list."""
    for cursor in handle._take_cursors():
        try:
            outcome.owe(await _close_cursor(cursor, owner=handle._conn))
        except _CancelledAfterWorkerError as exc:
            # Ordered before CancelledError: the close FAILED, we just also owe
            # a cancellation. Not a confirmed close.
            outcome.owe(exc)
            outcome.unsafe(
                "cursor close failed under cancellation: "
                f"{_public_detail(exc.worker_error)}",
                already_recorded=_already_fail_stopped(exc.worker_error))
        except _ChildCancelled as exc:
            outcome.owe(exc.caller_cancelled)
            outcome.unsafe(f"cursor close submission cancelled: {exc}",
                           already_recorded=True)
        except _OutcomeUnknown as exc:
            outcome.owe(exc.caller_cancelled)
            outcome.unsafe(f"cursor close outcome unknown: {exc}",
                           already_recorded=True)
        except asyncio.CancelledError as exc:
            # OUR cancellation; the close itself reached a known outcome — but
            # the cancellation is still owed to the caller.
            outcome.owe(exc)
        except BaseException as exc:  # noqa: BLE001 — every failure is recorded
            outcome.unsafe(f"cursor close: {exc!r}")


def _transaction_state(conn) -> bool | None:
    """True (open) / False (closed) / None (cannot be read).

    `conn.in_transaction` reads SQLite's own autocommit flag without touching
    the worker, so it is usable even when a submission's outcome is unknown —
    but the aiosqlite wrapper raises once it has dropped its connection, and the
    raw sqlite3 handle can still hold an open transaction at that moment. None
    therefore means UNKNOWN, never "safely closed"."""
    try:
        return bool(conn.in_transaction)
    except BaseException:  # noqa: BLE001 — any failure is an unreadable state
        return None


async def _force_close_transaction(conn, outcome: "_Outcome",
                                   caused_by: "_Incident | None" = None) -> None:
    """Leave no transaction open behind us, and never call an unreadable state
    a clean terminal. Reports into `outcome`.

    Everything it finds goes through outcome.unsafe(), so a rollback that did
    not close the transaction is on the same record as whatever sent us here.
    Appending to a bare list instead let those findings reach the caller's
    message while never reaching the fail-stop reason an operator reads.

    `caused_by` is the incident that made this cleanup necessary, for the one
    caller that has already reported the very condition we are about to observe
    again: a COMMIT that returned over a transaction which is still open. There
    the rollback and the state after it are that same failure still unfolding,
    not a second one."""
    state = _transaction_state(conn)
    if state is None:
        # The SAME condition `caused_by` already reported, observed once more
        # on the way in: a COMMIT that could not read the transaction state
        # sends us here, and we cannot read it either. That is one transaction
        # nobody can vouch for, not two failures. With no `caused_by` there is
        # nothing to attach it to and it is an incident of its own.
        outcome.unsafe(
            "transaction state unreadable; cannot prove the transaction is closed",
            incident=caused_by)
        _register_unclosed(conn)
        return
    if not state:
        return
    # The incident the rollback itself failed with, if it failed. What we
    # observe about the transaction AFTERWARDS is that same incident still
    # unfolding — a rollback whose outcome nobody could establish is exactly
    # why the transaction is still open — so the observation is attached to it
    # rather than recorded as a second, later failure. It stays None when the
    # rollback reached a clean outcome: a transaction open after a rollback
    # that SUCCEEDED is a new failure and has to be recorded as one.
    incident: _Incident | None = caused_by
    try:
        outcome.owe(await _rollback(conn))
    except _CancelledAfterWorkerError as exc:
        outcome.owe(exc)
        incident = outcome.unsafe(
            f"rollback failed under cancellation: {_public_detail(exc.worker_error)}",
            already_recorded=_already_fail_stopped(exc.worker_error))
    except _ChildCancelled as exc:
        outcome.owe(exc.caller_cancelled)
        incident = outcome.unsafe(f"rollback submission cancelled: {exc}",
                                  already_recorded=True)
    except _OutcomeUnknown as exc:
        outcome.owe(exc.caller_cancelled)
        incident = outcome.unsafe(f"rollback outcome unknown: {exc}",
                                  already_recorded=True)
    except asyncio.CancelledError as exc:
        outcome.owe(exc)
    except BaseException as exc:  # noqa: BLE001
        incident = outcome.unsafe(f"rollback: {exc!r}")
    state = _transaction_state(conn)
    if state is None:
        outcome.unsafe("transaction state unreadable after rollback",
                       incident=incident)
        _register_unclosed(conn)
    elif state:
        outcome.unsafe("transaction still open after rollback",
                       incident=incident)


async def _commit_transaction(conn, handle: _TxHandle) -> None:
    """Success path: close every cursor, then COMMIT — and only then return, so
    the operation lock is never released over an unfinished transaction.

    Four endings, and every one of them owes a cancellation first:

      * cancellation confirmed BEFORE the COMMIT was submitted
            -> nothing is durable; ROLL BACK and hand back the cancellation
      * COMMIT submitted and confirmed DURABLE
            -> never roll back over it; hand back the cancellation (commit-wins)
      * DURABLE, then the response was lost
            -> fail-stop; this caller still gets its cancellation
      * outcome unknown
            -> fail-stop, submission kept unresolved, cancellation still owed
    """
    handle._closed = True
    outcome = _Outcome()
    await _cleanup_cursors(handle, outcome)

    if outcome.problems or outcome.cancelled is not None:
        # Nothing has been submitted for COMMIT yet, so nothing is durable.
        # Committing a write whose caller is already provably cancelled — or
        # whose cursors we could not close — would make the boundary decide to
        # persist something nobody is waiting for.
        await _force_close_transaction(conn, outcome)
        failure = outcome.settle()
        if failure is not None:
            raise failure
        return

    try:
        submitted = await _submit(conn.commit(), "commit", owner=conn)
    except _OutcomeUnknown as exc:
        # Whether the COMMIT landed is unknown. Submitting a ROLLBACK now could
        # discard a durable commit, so submit nothing and stop. The fail-stop
        # latch and the unresolved registration both stand — but a caller that
        # was cancelled is still owed its cancellation, and only the NEXT
        # operation is refused.
        outcome.owe(exc.caller_cancelled)
        # already_recorded: _submit latched when it registered the submission.
        outcome.unsafe(f"commit outcome unknown ({exc})", already_recorded=True)
        _raise_from_private(outcome.settle(), exc)
    except _ChildCancelled as exc:
        # The submission came back cancelled: the COMMIT may or may not have
        # run. Close any transaction still open so nothing is left dangling,
        # then stop — this is NOT commit-wins.
        outcome.owe(exc.caller_cancelled)
        outcome.unsafe(f"commit submission cancelled ({exc})", already_recorded=True)
        await _force_close_transaction(conn, outcome)
        _raise_from_private(outcome.settle(), exc)
    except BaseException as exc:  # noqa: BLE001
        # absorb, not assignment: _submit reports "cancelled AND the worker
        # then failed" as one exception, and taking it whole would hide the
        # cancellation inside what looks like an ordinary commit error.
        outcome.absorb(exc)
    else:
        outcome.owe(submitted.caller_cancelled)
        # The driver reporting the COMMIT as done is not the same fact as
        # SQLite having left the transaction. Declaring success on the
        # submission alone releases the operation lock over a transaction that
        # is still open — the one state this whole boundary exists to prevent —
        # so ask the authoritative flag before believing it.
        state = _transaction_state(conn)
        if state is not False:
            stuck = outcome.unsafe(
                "commit reported success but the transaction is still open"
                if state else
                "commit reported success but the transaction state could not be read")
            # The same transaction, observed again after the remedy. Reporting
            # it as a second incident recorded one stuck transaction twice.
            await _force_close_transaction(conn, outcome, caused_by=stuck)
            raise outcome.settle()
        if outcome.cancelled is not None:
            # commit-wins: the COMMIT reached a known, successful outcome
            # before our cancellation could be delivered. No rollback.
            logger.warning("write cancelled after its COMMIT completed (commit-wins)")
            raise outcome.cancelled
        return

    commit_error = outcome.error
    if commit_error is not None and _transaction_state(conn) is False:
        # The COMMIT is DURABLE — SQLite is back in autocommit — yet it reported
        # an error, so its response was lost. We cannot tell the caller "failed"
        # (the row is there) or "succeeded" (we do not know what else the error
        # covered), and we must not roll back over it or keep writing next to it.
        raise _caller_owed(outcome.cancelled, _fail_stop_now(
            f"commit response lost after a durable COMMIT ({commit_error!r})"
        )) from commit_error

    await _force_close_transaction(conn, outcome)
    failure = outcome.settle()
    if failure is not None:
        raise failure


class _AbortOutcome(NamedTuple):
    """What the abort path could establish, what it still owes, and how it
    described itself.

    `failure` is the exception settle() built for THIS cleanup. It is carried
    rather than rebuilt because the global latch deliberately keeps the FIRST
    reason the process ever saw, which is routinely an earlier operation's
    incident — so re-deriving the caller's message from it describes somebody
    else's failure."""

    unresolved: bool
    cancelled: asyncio.CancelledError | None
    failure: BaseException | None = None


async def _abort_transaction(conn, handle: _TxHandle) -> _AbortOutcome:
    """Failure path: an exception is already in flight (Exception,
    BaseException or CancelledError alike). Clean up unconditionally and never
    raise over the caller's error.

    Reports whether the cleanup could NOT be resolved, so the boundary can
    classify what the caller sees — and any cancellation the cleanup collected,
    so the boundary cannot lose it either."""
    handle._closed = True
    outcome = _Outcome()
    await _cleanup_cursors(handle, outcome)
    await _force_close_transaction(conn, outcome)
    if outcome.problems:
        # settle() records only what is not already on the record, so an
        # incident _submit already latched is not reported a second time — and
        # the exception it builds is the ONLY description of this cleanup as a
        # whole. Discarding it left _reclassify with nothing but the global
        # latch to quote.
        failure = outcome.settle()
        if isinstance(failure, _CancelledAfterWorkerError):
            # The cancellation travels in its own field and _reclassify decides
            # what outranks what; keep the safety reason itself.
            failure = failure.worker_error
        return _AbortOutcome(True, outcome.cancelled, failure)
    return _AbortOutcome(False, outcome.cancelled)


def _reclassify(abort: _AbortOutcome, exc: BaseException) -> None:
    """Raise DatabaseFailStop when the abort path could not resolve its
    cleanup, mirroring what _commit_transaction does on the success path.

    Without this the caller is handed the raw cleanup error (or an internal
    _OutcomeUnknown), so a caller that distinguishes "restart-required" from an
    ordinary failure — create_user's duplicate-email case — cannot. Cancellation
    outranks all of it: a CancelledError is still owed to the caller, and the
    latch already refuses the next operation.

    What the cleanup established travels in `abort.failure`, and it is used in
    preference to the global latch wherever there is one: the latch keeps the
    FIRST reason this process ever recorded, so quoting it hands the caller an
    earlier operation's incident and silently drops everything this cleanup
    found — the cursor it could not close, the rollback it could not resolve,
    the transaction still open behind it."""
    unresolved, cancelled, cleanup = abort
    if isinstance(exc, (_OutcomeUnknown, _ChildCancelled)):
        cancelled = cancelled or exc.caller_cancelled
    if isinstance(exc, asyncio.CancelledError):
        return
    if isinstance(exc, (_OutcomeUnknown, _ChildCancelled)):
        # Internal classifications must never surface: a caller cannot act on
        # them, and _submit has already latched fail-stop for both. Both facts
        # are kept when the cleanup found something of its own — what it found,
        # and that the submission which sent us here never reached a terminal
        # outcome — and _public_detail is what makes the second one safe to say.
        if cleanup is not None:
            failure: BaseException = DatabaseFailStop(
                f"{cleanup.args[0]}; after {_public_detail(exc)}")
        else:
            failure = DatabaseFailStop(
                _fail_stop or f"worker outcome unresolved: {exc}")
        _raise_from_private(_caller_owed(cancelled, failure), exc)
    if cancelled is not None:
        raise _CancelledAfterWorkerError(exc) from exc
    if not unresolved:
        return
    _raise_from_private(
        cleanup if cleanup is not None else DatabaseFailStop(_fail_stop), exc)


async def _acquire_connection(*, metric: bool = False) -> aiosqlite.Connection:
    """The connection this operation will use, with no internal classification
    escaping to a public caller.

    Building a connection goes through the same worker as any other statement,
    so it can end in _OutcomeUnknown or _ChildCancelled. Those are private
    classifications a caller cannot act on — and _submit has already latched
    fail-stop for both — so they are reported as what they mean: the database
    is refusing this operation. Callers used to see the raw internal exception
    because the connection was acquired outside the boundary's own try."""
    try:
        return await (_get_metric_db() if metric else _get_conn())
    except BaseException as exc:  # noqa: BLE001
        failure = exc
    # Raised OUTSIDE the handler on purpose. `raise` while an exception is
    # being handled stamps that exception onto __context__ of the new one, and
    # here the exception being handled is this module's private classification
    # — reachable from the caller's object graph and printed by name in any
    # formatted traceback, however carefully the cause was chosen.
    _raise_public(failure)


@asynccontextmanager
async def _write_transaction(*, metric: bool = False):
    """The boundary itself. Everything public goes through write_transaction,
    which is the same thing with this module's private exception vocabulary
    translated for the outside world."""
    _require_admission()
    async with _operation():
        _require_admission()  # re-check: we may have waited on the lock
        conn = await _acquire_connection(metric=metric)
        handle = _TxHandle(conn)
        begin = _Outcome()
        try:
            submitted = await _submit(conn.execute("BEGIN IMMEDIATE"), "begin",
                                      disposal=_Disposal.CURSOR, owner=conn)
            begin.owe(submitted.caller_cancelled)
            handle._track(submitted.result)
            begin.owe(await handle._release(submitted.result))
        except BaseException as exc:  # noqa: BLE001
            begin.absorb(exc)
        failure = begin.settle()
        if failure is not None:
            # BEGIN can still land on the worker after our coroutine was
            # cancelled. _submit recovers its real outcome; _abort_transaction
            # then closes any transaction it actually opened.
            _reclassify(await _abort_transaction(conn, handle), failure)
            raise failure

        try:
            yield handle
        except BaseException as exc:
            _reclassify(await _abort_transaction(conn, handle), exc)
            raise
        await _commit_transaction(conn, handle)


@asynccontextmanager
async def write_transaction(*, metric: bool = False):
    """The single boundary every mutating path goes through.

    Takes the DB-file operation lock, opens with an explicit BEGIN IMMEDIATE
    (so the write lock is acquired up front rather than mid-statement), and
    does not release the lock until the transaction is committed or rolled back
    AND every cursor it opened is closed.

    This wrapper exists for one reason: nothing this module classifies
    privately — _OutcomeUnknown, _ChildCancelled, _CancelledAfterWorkerError —
    may reach a caller. They become a plain CancelledError or a DatabaseFailStop
    with the original kept as __cause__. Exceptions raised by the BODY pass
    through untouched."""
    failure: BaseException | None = None
    try:
        async with _write_transaction(metric=metric) as handle:
            yield handle
    except BaseException as exc:  # noqa: BLE001
        failure = exc
    if failure is not None:
        # Raised OUTSIDE the handler on purpose. `raise` while an exception is
        # being handled stamps that exception onto __context__ of the new one, and
        # here the exception being handled is this module's private classification
        # — reachable from the caller's object graph and printed by name in any
        # formatted traceback, however carefully the cause was chosen.
        _raise_public(failure)


async def _fetch_all(sql: str, params=()) -> list:
    """Run one read to completion inside the boundary: fetch every row and
    close the cursor before the lock is released. A half-consumed cursor
    outliving its operation is the Phase 0 CP-1 trigger.

    Module-private on purpose: a public read helper is a public way to run
    arbitrary SQL on the shared connection."""
    _require_admission()
    _require_read_only(sql)
    async with _operation():
        _require_admission()
        conn = await _acquire_connection()
        outcome = _Outcome()
        cursor, rows = None, []
        try:
            submitted = await _submit(conn.execute(sql, params), "read execute",
                                      disposal=_Disposal.CURSOR, owner=conn)
            outcome.owe(submitted.caller_cancelled)
            cursor = submitted.result
            fetched = await _submit(cursor.fetchall(), "read fetchall", owner=conn)
            outcome.owe(fetched.caller_cancelled)   # before materialising rows
            rows = list(fetched.result)
        except BaseException as exc:  # noqa: BLE001
            outcome.absorb(exc)
        if cursor is not None:
            # ONE close path for success and failure alike. Splitting them is
            # how a close that FAILED under cancellation left as "just a
            # cancellation", with nothing latched over a cursor still pinning
            # a read snapshot.
            try:
                outcome.owe(await _close_cursor(cursor, owner=conn))
            except BaseException as exc:  # noqa: BLE001
                outcome.absorb(exc)
                outcome.unsafe(f"read cursor close: {_public_detail(exc)}",
                               already_recorded=_already_fail_stopped(exc))
        failure = outcome.settle()
        if failure is not None:
            _raise_public(failure)
        return rows


async def _fetch_one(sql: str, params=()):
    rows = await _fetch_all(sql, params)
    return rows[0] if rows else None


async def _fetch_all_unrestricted(sql: str, params=()) -> list:
    """Read inside the boundary WITHOUT the read-only guard.

    Module-private and intended for PRAGMA inspection (connection verification,
    diagnostics, tests). Every ordinary read must go through _fetch_all, whose
    guard is what stops the read path from being used as an unguarded write."""
    _require_admission()
    async with _operation():
        _require_admission()
        conn = await _acquire_connection()
        failure: BaseException | None = None
        try:
            rows, cancelled = await _run_statement(conn, sql, "unrestricted read")
        except BaseException as exc:  # noqa: BLE001
            failure = exc
        if failure is not None:
            # Raised OUTSIDE the handler on purpose. `raise` while an exception is
            # being handled stamps that exception onto __context__ of the new one, and
            # here the exception being handled is this module's private classification
            # — reachable from the caller's object graph and printed by name in any
            # formatted traceback, however carefully the cause was chosen.
            _raise_public(failure)
        if cancelled is not None:
            _raise_public(cancelled)
        return rows


# ── Connections ──────────────────────────────────────


async def _get_conn() -> aiosqlite.Connection:
    """The shared connection. Module-private: a raw connection handed outside
    this module can leave a cursor or a transaction open outside the boundary,
    which is the Phase 0 CP-1 trigger. Callers use the public query functions.

    MUST be called inside an _operation() — the lazy init is check-then-act and
    the DB-file operation lock is what makes it safe."""
    global _conn
    if _conn is None:
        if _closing or _closed:
            raise DatabaseFailStop("database is closing/closed; no connection is published")
        if _db_path != ":memory:":
            os.makedirs(os.path.dirname(_db_path) or ".", exist_ok=True)
        conn = await _open_connection("shared")
        if _closing or _closed:      # re-check: building is an await point
            raise await _abandon_unpublished(
                conn, "shared",
                "database began closing; connection not published")
        _conn = conn                 # publish LAST, fully verified
    return _conn


async def close_db(timeout: float = _CLOSE_TIMEOUT) -> CloseVerdict:
    """Quiesce and close, as a participant in the operation lifecycle.

    ONE absolute deadline covers the whole thing — blocking admission, draining
    the in-flight operation, closing shared, closing metric, and adjudicating
    unresolved submissions. Each stage draws down the same budget; giving every
    stage its own `timeout` would let a bad shutdown take timeout x stages.

    One contract for every exit, including cancellation: recover what can be
    recovered and return CLOSED or RESTART_REQUIRED. It never returns the
    database to normal admission — `_closed` is set on every path out, so a
    cancelled close cannot look like a healthy database.

    CLOSED means every connection's close was confirmed AND nothing is left
    unresolved, unclosed, or waiting to be disposed of. Anything else is
    RESTART_REQUIRED, which is a signal for an external hard stop, not something
    this process can recover from."""
    global _conn, _metric_conn, _closing, _closed
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, timeout)

    def remaining() -> float:
        return max(0.0, deadline - loop.time())

    _closing = True
    lock = _op_lock
    acquired = False
    try:
        try:
            await asyncio.wait_for(lock.acquire(), timeout=remaining())
            acquired = True
        except (asyncio.TimeoutError, TimeoutError):
            _enter_fail_stop(
                f"close_db could not quiesce in-flight operations within {timeout}s")
            return CloseVerdict.RESTART_REQUIRED
        except asyncio.CancelledError:
            # Same contract as every other exit: do not propagate, do not return
            # to normal admission. The caller is shutting down either way.
            _enter_fail_stop("close_db was cancelled while quiescing")
            return CloseVerdict.RESTART_REQUIRED

        conn, _conn = _conn, None           # detach first: a failure below
        metric, _metric_conn = _metric_conn, None  # leaves no stale global

        adjudicate_unresolved()
        # A cursor a worker handed back late still pins a read snapshot on one
        # of these connections, so it is closed first — after the connection it
        # belongs to is gone, its close can never be CONFIRMED, only inferred.
        await _dispose_late_results(remaining(), disposals=(_Disposal.CURSOR,))
        # A submission can settle DURING that pass and hand back one more
        # cursor; it gets the same treatment, while its connection is open.
        adjudicate_unresolved()
        await _dispose_late_results(remaining(), disposals=(_Disposal.CURSOR,))
        adjudicate_unresolved()
        if _unresolved:
            # A submission is still on a worker thread. Submitting a close
            # behind it would queue forever, and forcing it would abandon an
            # unknown statement. Keep references to what we could not close —
            # a silently dropped connection is a worker nobody can account for.
            for detached in (conn, metric):
                if detached is not None:
                    _register_unclosed(detached)
            logger.error("close_db: %d unresolved worker submission(s); "
                         "restart required", len(_unresolved))
            # A late CONNECTION owns a worker of its OWN, so it can still be
            # retired inside what is left of the deadline.
            await _dispose_late_results(remaining())
            return CloseVerdict.RESTART_REQUIRED

        verdict = CloseVerdict.CLOSED
        for name, target in (("shared", conn), ("metric", metric)):
            if target is None:
                continue
            problem: str | None = None
            # Whether the failure behind `problem` is already on the fail-stop
            # record. _submit latches when it registers an unresolved
            # submission, so the close it could not establish is that incident;
            # naming it again below would count one close as two failures.
            recorded = False
            try:
                await _submit(target.close(), f"{name} connection close",
                              timeout=remaining(), owner=target)
            except asyncio.CancelledError as exc:
                # Includes _CancelledAfterWorkerError. Either way the close is
                # unconfirmed; do not propagate, classify it.
                problem = (f"{name} connection close unconfirmed: "
                           f"{_public_detail(exc)}")
            except BaseException as exc:  # noqa: BLE001
                problem = f"{name} connection close: {_public_detail(exc)}"
                recorded = _already_fail_stopped(exc)
            if problem is None:
                # aiosqlite's close() closes the raw SQLite handle and QUEUES a
                # stop sentinel — it never joins. The non-daemon thread is what
                # holds the process open and what may still be touching the
                # file, so the close is only confirmed once the thread is gone.
                try:
                    if not await _join_worker(target, remaining()):
                        problem = (f"{name} connection closed but its aiosqlite "
                                   "worker thread did not exit")
                except BaseException as exc:  # noqa: BLE001
                    problem = (f"{name} worker thread exit could not be "
                               f"confirmed: {exc!r}")
            if problem is not None:
                verdict = CloseVerdict.RESTART_REQUIRED
                _register_unclosed(target)
                if recorded:
                    logger.error("close_db: %s", problem)
                else:
                    _enter_fail_stop(problem)

        adjudicate_unresolved()
        # Anything a worker handed back after its caller gave up is closed here,
        # inside the same deadline. Until that close is CONFIRMED the resource
        # stays registered, so CLOSED cannot be reported over a live sqlite
        # handle and a running aiosqlite thread.
        await _dispose_late_results(remaining())
        adjudicate_unresolved()
        if _unresolved or _unclosed or _late_results:
            # Never report CLOSED while a worker, a connection or a late result
            # is unaccounted for — including from an EARLIER close attempt.
            return CloseVerdict.RESTART_REQUIRED
        # One statement about EVERY connection this process ever built, not
        # only the ones a registry happens to still name: CLOSED means not one
        # aiosqlite worker thread is left running.
        if _fail_stop is not None:
            # The verdict is about RESOURCES; the latch is about trust. A
            # database that refused writes mid-run and then closed tidily still
            # needs an operator to know it happened, and this log line is the
            # only place a shutdown says so.
            logger.error("database closed, but it had already fail-stopped: %s",
                         _fail_stop)
        stragglers = [c for c in list(_connections) if _worker_alive(c)]
        if stragglers:
            for straggler in stragglers:
                _register_unclosed(straggler)
            _enter_fail_stop(
                f"{len(stragglers)} aiosqlite worker thread(s) were still "
                "running when the database was closed")
            return CloseVerdict.RESTART_REQUIRED
        return verdict
    finally:
        if acquired:
            lock.release()
        # Every path out, cancellation included: the database stays shut. A
        # cancelled close must not leave `_closing = False` looking healthy.
        _closed = True
        _closing = False


async def init_db() -> None:
    """Create/upgrade the schema in ONE transaction, through the same boundary
    as every other write — a half-applied schema is exactly the state a later
    write cannot recover from."""
    async with write_transaction() as tx:
        await tx.execute("""
            CREATE TABLE IF NOT EXISTS metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                timestamp REAL NOT NULL,
                data TEXT NOT NULL
            )
        """)
        await tx.execute("""
            CREATE INDEX IF NOT EXISTS idx_metrics_agent_ts
            ON metrics (agent_id, timestamp)
        """)

        # Downsampled metrics for long-term storage
        await tx.execute("""
            CREATE TABLE IF NOT EXISTS metrics_downsampled (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                timestamp REAL NOT NULL,
                resolution TEXT NOT NULL,
                data TEXT NOT NULL
            )
        """)
        await tx.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_metrics_ds_unique
            ON metrics_downsampled (agent_id, resolution, timestamp)
        """)

        await tx.execute("""
            CREATE TABLE IF NOT EXISTS runtime_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        # ── Aggregation coverage (additive; metrics/metrics_downsampled are
        # never rebuilt) ────────────────────────────────────────────────────
        #
        # Three tables with three different lifetimes:
        #
        #   metric_agg_coverage  per RAW ROW, lives exactly as long as its raw
        #                        row (deleted in the same transaction), so it
        #                        is bounded by the 1h raw window, not by the
        #                        7-day summary window. FOLDED = this row went
        #                        into an accumulation for that bucket; PENDING
        #                        = deliberately not summarized, and preserved.
        #   metric_agg_bucket    per (agent, resolution, bucket) — the compact
        #                        provenance that OUTLIVES the raw rows. OPEN
        #                        means every contributing raw is still here and
        #                        the bucket can be recomputed exactly; SEALED
        #                        means at least one has been reclaimed, so a
        #                        later arrival can never reconstruct it.
        #                        published_gen is the raw id high-water the
        #                        STORED summary actually covers: it is what
        #                        turns a folded row into a proven one, in a
        #                        single row write, instead of restamping every
        #                        row of the bucket at publication time.
        #   metric_agg_partial   an in-flight bounded accumulation, frozen at
        #                        through_raw_id. While one exists for a bucket,
        #                        nothing in that bucket is deletable.
        #
        # Coverage is keyed by the durable id, not by an output timestamp: an
        # agent can send a valid older sample with a HIGHER id at any time, so
        # no timestamp watermark — global OR per-agent — can decide what is
        # still outstanding.
        #
        # Each table carries a token unique to its current shape, so an older
        # build's table is rebuilt exactly once, in THIS transaction, alongside
        # the data it constrains. Rebuilding these is not a rebuild of
        # metrics/metrics_downsampled, which are never touched.
        cov_sql = await tx.fetch_one(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'metric_agg_coverage'")
        bucket_sql = await tx.fetch_one(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'metric_agg_bucket'")
        partial_sql = await tx.fetch_one(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'metric_agg_partial'")
        cov_is_old = bool(cov_sql) and "'FOLDED'" not in (cov_sql[0] or "")
        bucket_is_old = bool(bucket_sql) and "published_gen" not in (bucket_sql[0] or "")
        partial_is_old = bool(partial_sql) and "through_raw_id" not in (partial_sql[0] or "")
        bucket_existed = bool(bucket_sql)

        cov_columns = """
                raw_id INTEGER NOT NULL,
                resolution TEXT NOT NULL CHECK (resolution IN ('1m', '5m')),
                agent_id TEXT NOT NULL,
                bucket_ts REAL NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('FOLDED', 'PENDING')),
                PRIMARY KEY (raw_id, resolution)
        """
        bucket_columns = """
                agent_id TEXT NOT NULL,
                resolution TEXT NOT NULL CHECK (resolution IN ('1m', '5m')),
                bucket_ts REAL NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('OPEN', 'SEALED')),
                published_gen INTEGER NOT NULL,
                PRIMARY KEY (agent_id, resolution, bucket_ts)
        """
        partial_columns = """
                agent_id TEXT NOT NULL,
                resolution TEXT NOT NULL CHECK (resolution IN ('1m', '5m')),
                bucket_ts REAL NOT NULL,
                n INTEGER NOT NULL,
                last_ts REAL NOT NULL,
                last_raw_id INTEGER NOT NULL,
                through_raw_id INTEGER NOT NULL,
                acc TEXT NOT NULL,
                PRIMARY KEY (agent_id, resolution, bucket_ts)
        """
        await tx.execute(f"CREATE TABLE IF NOT EXISTS metric_agg_coverage ({cov_columns})")
        await tx.execute(f"CREATE TABLE IF NOT EXISTS metric_agg_bucket ({bucket_columns})")
        await tx.execute(f"CREATE TABLE IF NOT EXISTS metric_agg_partial ({partial_columns})")

        # Scan resumption point, PER (agent, resolution). One agent falling
        # behind cannot move another agent's point, and a restart continues
        # from durable state instead of rescanning the whole 7-day window.
        await tx.execute("""
            CREATE TABLE IF NOT EXISTS metric_agg_progress (
                agent_id TEXT NOT NULL,
                resolution TEXT NOT NULL CHECK (resolution IN ('1m', '5m')),
                last_raw_id INTEGER NOT NULL,
                PRIMARY KEY (agent_id, resolution)
            )
        """)


        # Bucket FIRST: its published_gen is derived from the coverage states
        # the rebuild below is about to collapse.
        if bucket_is_old:
            await tx.execute(f"CREATE TABLE metric_agg_bucket_v2 ({bucket_columns})")
            await tx.execute("""
                INSERT INTO metric_agg_bucket_v2
                    (agent_id, resolution, bucket_ts, state, published_gen)
                SELECT b.agent_id, b.resolution, b.bucket_ts, b.state,
                       COALESCE((SELECT MAX(c.raw_id) FROM metric_agg_coverage c
                                 WHERE c.agent_id = b.agent_id
                                   AND c.resolution = b.resolution
                                   AND c.bucket_ts = b.bucket_ts
                                   AND c.state = 'DONE'), 0)
                FROM metric_agg_bucket b
            """)
            await tx.execute("DROP TABLE metric_agg_bucket")
            await tx.execute("ALTER TABLE metric_agg_bucket_v2 RENAME TO metric_agg_bucket")
        if cov_is_old:
            # A row the old build had marked PARTIAL was mid-accumulation: it
            # was folded into a generation that never published, so it is NOT
            # a finished fold and must not be carried over as one. Its coverage
            # is DROPPED, which is precisely what puts the row back in front of
            # the candidate scan so the bucket is recomputed from scratch.
            # Only a finished fold (DONE) becomes FOLDED; PENDING is preserved.
            #
            # The scan floor has to come back with it: a durable progress value
            # already past those ids would hide exactly the rows just dropped.
            await tx.execute("""
                UPDATE metric_agg_progress SET last_raw_id = MIN(
                    last_raw_id,
                    COALESCE((SELECT MIN(c.raw_id) - 1 FROM metric_agg_coverage c
                              WHERE c.agent_id = metric_agg_progress.agent_id
                                AND c.resolution = metric_agg_progress.resolution
                                AND c.state = 'PARTIAL'), last_raw_id))
            """)
            await tx.execute(f"CREATE TABLE metric_agg_coverage_v2 ({cov_columns})")
            await tx.execute("""
                INSERT INTO metric_agg_coverage_v2
                    (raw_id, resolution, agent_id, bucket_ts, state)
                SELECT raw_id, resolution, agent_id, bucket_ts,
                       CASE WHEN state = 'PENDING' THEN 'PENDING' ELSE 'FOLDED' END
                FROM metric_agg_coverage
                WHERE state <> 'PARTIAL'
            """)
            await tx.execute("DROP TABLE metric_agg_coverage")
            await tx.execute("ALTER TABLE metric_agg_coverage_v2 RENAME TO metric_agg_coverage")
        if partial_is_old:
            # An in-flight accumulation published nothing, so discarding it
            # loses no result: its rows are folded again from scratch.
            await tx.execute("DROP TABLE metric_agg_partial")
            await tx.execute(f"CREATE TABLE metric_agg_partial ({partial_columns})")

        await tx.execute("""
            CREATE INDEX IF NOT EXISTS idx_metric_agg_cov_bucket
            ON metric_agg_coverage (agent_id, resolution, bucket_ts)
        """)
        # Leading column is the GC predicate, so the periodic sweeps seek
        # instead of scanning the provenance and summary tables.
        await tx.execute("""
            CREATE INDEX IF NOT EXISTS idx_metric_agg_bucket_ts
            ON metric_agg_bucket (bucket_ts)
        """)
        await tx.execute("""
            CREATE INDEX IF NOT EXISTS idx_metrics_ds_ts
            ON metrics_downsampled (timestamp)
        """)

        if not bucket_existed:
            # Carrying an older build's per-raw coverage over to the compact
            # form. A bucket whose folded raw is already gone can never be
            # recomputed, so it is SEALED, not OPEN — and the orphaned per-raw
            # rows go, because coverage no longer outlives its raw.
            await tx.execute("""
                INSERT OR IGNORE INTO metric_agg_bucket
                    (agent_id, resolution, bucket_ts, state, published_gen)
                SELECT c.agent_id, c.resolution, c.bucket_ts,
                       CASE WHEN EXISTS (
                           SELECT 1 FROM metric_agg_coverage c2
                           WHERE c2.agent_id = c.agent_id
                             AND c2.resolution = c.resolution
                             AND c2.bucket_ts = c.bucket_ts
                             AND c2.state = 'FOLDED'
                             AND NOT EXISTS (SELECT 1 FROM metrics m WHERE m.id = c2.raw_id)
                       ) THEN 'SEALED' ELSE 'OPEN' END,
                       MAX(c.raw_id)
                FROM metric_agg_coverage c
                WHERE c.state = 'FOLDED'
                GROUP BY c.agent_id, c.resolution, c.bucket_ts
            """)
            await tx.execute(
                "DELETE FROM metric_agg_coverage "
                "WHERE raw_id NOT IN (SELECT id FROM metrics)")

        # The upgrade cutover, recorded ONCE and in the SAME transaction as the
        # tables above — schema without cutover (or the reverse) is exactly the
        # half-applied state that would let un-provable history be deleted.
        # Rows at or below it predate coverage tracking: their aggregation
        # cannot be proven, so they are never summarized into a bucket whose
        # provenance we would then be guessing at, and never auto-deleted.
        seq = await tx.fetch_one(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sqlite_sequence'")
        high = 0
        if seq:
            row = await tx.fetch_one(
                "SELECT seq FROM sqlite_sequence WHERE name = 'metrics'")
            high = int(row[0]) if row and row[0] is not None else 0
        row = await tx.fetch_one("SELECT COALESCE(MAX(id), 0) FROM metrics")
        high = max(high, int(row[0]) if row and row[0] is not None else 0)
        await tx.execute(
            "INSERT OR IGNORE INTO runtime_config (key, value) VALUES (?, ?)",
            (_AGG_CUTOVER_KEY, str(high)),
        )

        await tx.execute("""
            CREATE TABLE IF NOT EXISTS token_blacklist (
                token_hash TEXT PRIMARY KEY,
                expires_at REAL NOT NULL
            )
        """)

        await tx.execute("""
            CREATE TABLE IF NOT EXISTS alert_config (
                id INTEGER PRIMARY KEY DEFAULT 1,
                config TEXT NOT NULL
            )
        """)

        await tx.execute("""
            CREATE TABLE IF NOT EXISTS users (
                email TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                totp_secret TEXT,
                totp_enabled INTEGER DEFAULT 0,
                must_change_password INTEGER DEFAULT 0,
                created_at REAL NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                is_active INTEGER NOT NULL DEFAULT 1,
                tokens_valid_after REAL NOT NULL DEFAULT 0
            )
        """)

        # Migrate older installs that lack role / is_active / tokens_valid_after.
        existing_cols = {row["name"] for row in await tx.fetch_all("PRAGMA table_info(users)")}
        if "role" not in existing_cols:
            await tx.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")
        if "is_active" not in existing_cols:
            await tx.execute("ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
        if "tokens_valid_after" not in existing_cols:
            await tx.execute("ALTER TABLE users ADD COLUMN tokens_valid_after REAL NOT NULL DEFAULT 0")

        # Promote the earliest-created user to admin if no admin exists yet.
        admin_count = (await tx.fetch_one("SELECT COUNT(*) FROM users WHERE role = 'admin'"))[0]
        if admin_count == 0:
            first = await tx.fetch_one("SELECT email FROM users ORDER BY created_at ASC LIMIT 1")
            if first:
                await tx.execute("UPDATE users SET role = 'admin' WHERE email = ?", (first["email"],))
                logger.info("Promoted %s to admin (migration)", first["email"])

        # User ↔ host (agent) account mappings — controls per-user terminal access per host.
        await tx.execute("""
            CREATE TABLE IF NOT EXISTS user_host_accounts (
                user_email TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                host_user TEXT NOT NULL,
                PRIMARY KEY (user_email, agent_id),
                FOREIGN KEY (user_email) REFERENCES users(email) ON DELETE CASCADE
            )
        """)

        # Audit log — attributable record of host-root-equivalent actions and the
        # account lifecycle, persisted across restarts (survives container log rotation).
        await tx.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                user_email TEXT NOT NULL,
                action TEXT NOT NULL,
                agent_id TEXT NOT NULL DEFAULT '',
                detail TEXT NOT NULL DEFAULT '{}'
            )
        """)
        await tx.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log (timestamp)")

        await tx.execute("""
            CREATE TABLE IF NOT EXISTS net_conn_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                ts REAL NOT NULL,
                event TEXT NOT NULL,
                proto TEXT NOT NULL,
                laddr TEXT, lport INTEGER,
                raddr TEXT, rport INTEGER,
                status TEXT,
                pid INTEGER, pname TEXT,
                duration REAL
            )
        """)
        await tx.execute("CREATE INDEX IF NOT EXISTS idx_nce_agent_ts ON net_conn_events (agent_id, ts)")
        await tx.execute("CREATE INDEX IF NOT EXISTS idx_nce_raddr ON net_conn_events (agent_id, raddr)")
        await tx.execute("""
            CREATE TABLE IF NOT EXISTS net_flow_rollup (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                ts REAL NOT NULL,
                data TEXT NOT NULL
            )
        """)
        await tx.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_nfr_unique ON net_flow_rollup (agent_id, ts)")

        # BEFORE anything with a side effect outside the database. The
        # default-admin branch below writes a plaintext credential file into
        # the data directory; doing that for a database startup is about to
        # refuse would leave a password on disk for a server that never comes
        # up. Everything above this line is schema, and schema is what makes
        # the check meaningful.
        _assert_foreign_keys_intact(await tx.fetch_all("PRAGMA foreign_key_check"))

        # Create default admin if no users exist at all
        user_count = (await tx.fetch_one("SELECT COUNT(*) FROM users"))[0]

        if user_count == 0:
            import secrets
            default_email = os.getenv("GLASSOPS_ADMIN_EMAIL", "admin@glassops.local")
            env_pw = os.getenv("GLASSOPS_ADMIN_PASSWORD", "")

            if env_pw:
                # Explicit password set by admin
                password = env_pw
                must_change = False
            else:
                # No password configured — generate a random one-time password and
                # write it to a 0600 file in the data dir instead of the logs (logs are
                # captured by supervisord/docker and retained). Operator reads it once,
                # is forced to change it on first login, then deletes the file.
                password = secrets.token_urlsafe(16)
                must_change = True
                pw_file = _initial_admin_pw_file()
                try:
                    with open(pw_file, "w") as f:
                        f.write(f"email: {default_email}\npassword: {password}\n")
                    os.chmod(pw_file, 0o600)
                    logger.warning("Initial admin password generated → %s "
                                   "(read it, log in, change immediately, then delete the file)", pw_file)
                except OSError as e:
                    # Never log the plaintext credential (logs are captured/retained).
                    # The data dir already holds the DB we just wrote, so this is
                    # near-impossible; fail closed and tell the operator how to recover.
                    # Raising here rolls the whole schema transaction back, so the next
                    # start retries from a clean slate rather than a half-built DB.
                    raise RuntimeError(
                        f"Could not write initial admin password file ({pw_file}): {e}. "
                        "Fix the data directory permissions, or set GLASSOPS_ADMIN_PASSWORD "
                        "and restart."
                    ) from e

            pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
            await tx.execute(
                "INSERT INTO users (email, password_hash, must_change_password, created_at, role, is_active) VALUES (?, ?, ?, ?, 'admin', 1)",
                (default_email, pw_hash, 1 if must_change else 0, time.time()),
            )
            logger.info("Admin user created: %s", default_email)


def _assert_foreign_keys_intact(violations) -> None:
    """Refuse to finish startup while existing rows break a declared foreign key.

    Foreign keys are enforced per CONNECTION and are OFF by default in SQLite,
    so rows written by an older build can already violate what this schema
    declares. `user_host_accounts` is the case that matters: a row left behind
    when a user was deleted is silently re-adopted the moment anyone registers
    that email again, handing the new account the deleted user's host account —
    routinely `root` — as a terminal permission nobody granted.

    Nothing is repaired here. Deleting the rows destroys an attributable record
    and re-attributing them IS the failure, so both are left to an approved
    migration; this only makes sure the process refuses to serve until then."""
    if not violations:
        return
    counts: dict[str, int] = {}
    for row in violations:
        counts[row[0]] = counts.get(row[0], 0) + 1
    detail = ", ".join(f"{table}={n} row(s)" for table, n in sorted(counts.items()))
    _enter_fail_stop(f"existing rows violate declared foreign keys: {detail}")
    logger.error("foreign_key_check found violations in existing data: %s", detail)
    raise SchemaIntegrityViolation(
        f"existing rows violate the schema's foreign keys: {detail}. Refusing to "
        "serve. These rows are NOT deleted or re-attributed automatically — a "
        "user re-registering a deleted user's email would inherit their host "
        "account mappings. Resolve them with an approved migration, then restart."
    )


# ── Metrics ──────────────────────────────────────────

# Keys an agent must never control: assigned by the server at ingest for the
# broadcast copy (identity + the ephemeral arrival anchor after_seq), and
# recomputed from the id column by the REST read paths. Stripped from inbound
# payloads and from stored data JSON on every read.
RESERVED_SAMPLE_KEYS = ("sample_id", "arrival_seq", "persisted", "after_seq")

_metric_conn: aiosqlite.Connection | None = None


async def _get_metric_db() -> aiosqlite.Connection:
    """Dedicated connection for metric INSERTs. aiosqlite serializes queued
    operations, NOT transactions — on the shared connection another writer's
    commit/rollback interleaved between our INSERT and commit would adopt or
    discard the pending row.

    MUST be called inside an _operation(): the lazy init is check-then-act, and
    the DB-file operation lock is what makes it safe. Concurrent first calls
    without it each open — and all but one leak — a connection whose non-daemon
    worker thread then blocks interpreter exit."""
    global _metric_conn
    if _metric_conn is None:
        conn = await _open_connection("metric")
        if _closing or _closed:      # re-check: building is an await point
            raise await _abandon_unpublished(
                conn, "metric",
                "database began closing; connection not published")
        _metric_conn = conn          # publish LAST, fully verified
    return _metric_conn


async def store_metric(agent_id: str, timestamp: float, data: dict) -> int:
    async with write_transaction(metric=True) as tx:
        result = await tx.execute(
            "INSERT INTO metrics (agent_id, timestamp, data) VALUES (?, ?, ?)",
            (agent_id, timestamp, json.dumps(data)),
        )
    return result.lastrowid


async def get_max_metric_id() -> int:
    """Last metrics.id ever assigned — sqlite_sequence, NOT MAX(id).
    AUTOINCREMENT never reissues, and cleanup deletes the highest rows while
    sqlite_sequence keeps the last issued number, so MAX(id) would
    under-count after a prune. Seeds the ephemeral after_seq anchor so a
    store failure right after a restart still orders the sample after
    existing history. 0 before the first insert (sqlite_sequence exists from
    CREATE TABLE ... AUTOINCREMENT but holds no metrics row until then)."""
    row = await _fetch_one("SELECT seq FROM sqlite_sequence WHERE name = 'metrics'")
    return row[0] if row else 0


async def get_recent_metrics(agent_id: str, limit: int = 60) -> list[dict]:
    """Latest N rows by ingest order (id), returned oldest-arrival-first.
    Selection and ordering use id, not timestamp: a clock-skewed row must not
    displace genuinely newer arrivals from the "recent" window."""
    rows = await _fetch_all(
        "SELECT id, timestamp, data FROM metrics WHERE agent_id = ? ORDER BY id DESC LIMIT ?",
        # Floor as well as cap: SQLite treats LIMIT -1 as unbounded.
        (agent_id, max(1, min(limit, 300))),
    )
    result = []
    for row in rows:
        entry = json.loads(row["data"])
        # Rows stored before the ingest strip landed may carry agent-forged
        # reserved keys (incl. "persisted", which no column overwrites).
        for key in RESERVED_SAMPLE_KEYS:
            entry.pop(key, None)
        entry["timestamp"] = row["timestamp"]
        entry["sample_id"] = f"raw:{row['id']}"
        entry["arrival_seq"] = row["id"]
        result.append(entry)
    return list(reversed(result))


async def get_metrics_range(
    agent_id: str, start: float, end: float, max_points: int = 500
) -> list[dict]:
    """Get metrics between start and end timestamps. Auto-selects resolution.
    Every branch keeps the TIME-ordered response contract (category-axis
    consumers draw the array verbatim); raw rows (<=1h) additionally carry
    their durable identity (sample_id/arrival_seq) with ", id" breaking
    equal-timestamp ties deterministically by arrival. Identity-aware
    consumers sort by arrival_seq themselves."""
    duration = end - start

    is_raw = duration <= 3600
    # < 1 hour: raw data
    if is_raw:
        rows = await _fetch_all(
            "SELECT id, timestamp, data FROM metrics WHERE agent_id = ? AND timestamp BETWEEN ? AND ? ORDER BY timestamp, id",
            (agent_id, start, end),
        )
    # 1h - 24h: 1min downsampled
    elif duration <= 86400:
        rows = await _fetch_all(
            "SELECT timestamp, data FROM metrics_downsampled WHERE agent_id = ? AND resolution = '1m' AND timestamp BETWEEN ? AND ? ORDER BY timestamp",
            (agent_id, start, end),
        )
    # > 24h: 5min downsampled
    else:
        rows = await _fetch_all(
            "SELECT timestamp, data FROM metrics_downsampled WHERE agent_id = ? AND resolution = '5m' AND timestamp BETWEEN ? AND ? ORDER BY timestamp",
            (agent_id, start, end),
        )
    result = []
    # Thin out if too many points
    step = max(1, len(rows) // max_points)
    for i, row in enumerate(rows):
        if i % step == 0:
            entry = json.loads(row["data"])
            # Strip reserved keys: rows stored before the ingest strip (or
            # downsampled copies of them) may carry agent-forged identity.
            for key in RESERVED_SAMPLE_KEYS:
                entry.pop(key, None)
            entry["timestamp"] = row["timestamp"]
            if is_raw:
                entry["sample_id"] = f"raw:{row['id']}"
                entry["arrival_seq"] = row["id"]
            result.append(entry)
    return result


async def get_container_history(
    agent_id: str, container_name: str, start: float, end: float, max_points: int = 500
) -> list[dict]:
    """Time-series for a single container by name. Walks the same auto-resolution
    tables as get_metrics_range, but extracts only the named container's cpu/mem
    so the response stays small.
    Raw-window points carry the row's sample_id/arrival_seq (absent for downsampled windows)."""
    snapshots = await get_metrics_range(agent_id, start, end, max_points=max_points * 4)
    result = []
    for snap in snapshots:
        ts = snap.get("timestamp")
        for c in snap.get("containers") or []:
            if c.get("name") == container_name:
                gpu = c.get("gpu") if isinstance(c.get("gpu"), dict) else None
                point = {
                    "t": ts,
                    "cpu": float(c.get("cpu_percent", 0) or 0),
                    "mem": float(c.get("mem_usage", 0) or 0),
                    "mem_limit": float(c.get("mem_limit", 0) or 0),
                    "vram": float(gpu.get("vram_bytes", 0) or 0) if gpu else 0.0,
                    "gpu_util": float(gpu.get("gpu_util", 0) or 0) if gpu else 0.0,
                    "gpu_present": bool(gpu) or bool(c.get("gpu_reserved")),
                }
                if "sample_id" in snap:
                    point["sample_id"] = snap["sample_id"]
                    point["arrival_seq"] = snap["arrival_seq"]
                result.append(point)
                break
    # Thin out if the underlying fetch was generous
    if len(result) > max_points:
        step = max(1, len(result) // max_points)
        result = [p for i, p in enumerate(result) if i % step == 0]
    return result


# Coverage/progress vocabulary, per RAW ROW.
#   FOLDED   this row went into an accumulation for its bucket. On its own it
#            authorizes NOTHING: proof requires the bucket's published_gen to
#            have reached this row's id AND the summary to still exist.
#   PENDING  real, kept, but its bucket cannot be recomputed exactly, so it is
#            neither summarized nor deletable.
# A row with no coverage at all is simply not yet decided, and is exactly what
# the candidate scan looks for.
_AGG_CUTOVER_KEY = "metric_agg_cutover_raw_id"
_AGG_SPECS = {"1m": 60, "5m": 300}
_AGG_RESOLUTIONS = tuple(_AGG_SPECS)
_AGG_BATCH_LIMIT = 2000
_CLEANUP_BATCH_LIMIT = 2000
_COV_FOLDED = "FOLDED"
_COV_PENDING = "PENDING"
# Bucket-level provenance, the part that OUTLIVES the raw rows.
_BUCKET_OPEN = "OPEN"        # every contributing raw is still present
_BUCKET_SEALED = "SEALED"    # a contributing raw was reclaimed: never recomputable


def _require_resolution(resolution_seconds: int, resolution_label: str) -> int:
    """The only two (seconds, label) pairs the history tables serve.

    Checked before any transaction opens, so a mismatched pair changes no
    summary, no coverage and no progress."""
    expected = _AGG_SPECS.get(resolution_label)
    if expected is None or expected != resolution_seconds:
        raise InvalidAggregationResolution(
            f"unsupported downsample resolution ({resolution_seconds!r}, "
            f"{resolution_label!r}); supported: "
            + ", ".join(f"({sec}, {lab!r})" for lab, sec in _AGG_SPECS.items())
        )
    return expected


async def _agg_cutover(tx) -> int | None:
    """The raw id high-water recorded when coverage tracking was installed.

    None when the marker is absent — a database that never went through
    init_db()'s cutover write. Callers treat that as "nothing is provable":
    aggregate nothing, delete nothing. Fail-closed, because the alternative
    (assume 0) would treat un-provable legacy rows as ordinary new rows."""
    row = await tx.fetch_one(
        "SELECT value FROM runtime_config WHERE key = ?", (_AGG_CUTOVER_KEY,))
    if not row or row[0] is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None


async def _mark_pending(tx, agent_id: str, lo: float, hi: float,
                        resolution_label: str, cutover: int, budget: int) -> int:
    """Record rows we deliberately did NOT summarize, up to `budget` of them.

    The row survives cleanup (only a folded row behind a published generation
    with a live summary is deletable) and the marker stops the scan from
    re-examining it every cycle."""
    rows = await tx.fetch_all(
        "SELECT m.id AS id FROM metrics m "
        "LEFT JOIN metric_agg_coverage c ON c.raw_id = m.id AND c.resolution = ? "
        "WHERE m.agent_id = ? AND m.timestamp >= ? AND m.timestamp < ? AND m.id > ? "
        "  AND c.raw_id IS NULL ORDER BY m.id LIMIT ?",
        (resolution_label, agent_id, lo, hi, cutover, budget),
    )
    for row in rows:
        await tx.execute(
            "INSERT INTO metric_agg_coverage (raw_id, resolution, agent_id, bucket_ts, state) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(raw_id, resolution) DO NOTHING",
            (row["id"], resolution_label, agent_id, lo, _COV_PENDING),
        )
    return len(rows)


async def _advance_bucket(tx, agent_id: str, bucket_ts: int, resolution_seconds: int,
                          resolution_label: str, cutover: int, budget: int) -> tuple[int, bool]:
    """Fold at most `budget` raw rows of ONE completed bucket.

    Work is organised in GENERATIONS. Starting an accumulation freezes
    `through_raw_id` at the bucket's current highest raw id; that generation
    then folds exactly the rows at or below it, however many bounded calls
    that takes. Ids only ever increase, so a row arriving mid-accumulation —
    even one whose TIMESTAMP is behind the resume cursor — falls outside the
    frozen generation instead of invalidating it. It is picked up by the next
    generation, which recomputes the whole bucket from scratch. So publication
    always terminates, and no row is ever folded twice into one average.

    Publication itself is a single row write: metric_agg_bucket.published_gen
    moves to the generation's high-water. Coverage rows are never restamped,
    so the publishing call writes no more rows than any other call.

    A generation only starts when the bucket is provable:

      * no pre-cutover row in the bucket (its aggregation state is unknown),
      * the bucket is not SEALED (nothing was reclaimed out from under us),
      * and an existing summary is accompanied by the bucket provenance that
        produced it (a legacy summary's provenance cannot be reconstructed).

    Otherwise the rows are kept as PENDING and the stored summary stands.
    Returns (rows folded, summary published)."""
    lo = float(bucket_ts)
    hi = float(bucket_ts + resolution_seconds)

    bucket = await tx.fetch_one(
        "SELECT state, published_gen FROM metric_agg_bucket "
        "WHERE agent_id = ? AND resolution = ? AND bucket_ts = ?",
        (agent_id, resolution_label, lo),
    )
    published_gen = bucket["published_gen"] if bucket else 0

    partial = await tx.fetch_one(
        "SELECT n, last_ts, last_raw_id, through_raw_id, acc FROM metric_agg_partial "
        "WHERE agent_id = ? AND resolution = ? AND bucket_ts = ?",
        (agent_id, resolution_label, lo),
    )
    if partial is None:
        # Anything to do at all? A bucket whose every row is already behind a
        # published generation is skipped outright, so a progress point pinned
        # elsewhere does not re-fold finished work every cycle.
        outstanding = await tx.fetch_one(
            "SELECT 1 FROM metrics m "
            "LEFT JOIN metric_agg_coverage c ON c.raw_id = m.id AND c.resolution = ? "
            "WHERE m.agent_id = ? AND m.timestamp >= ? AND m.timestamp < ? AND m.id > ? "
            "  AND (c.raw_id IS NULL OR (c.state = ? AND m.id > ?)) LIMIT 1",
            (resolution_label, agent_id, lo, hi, cutover, _COV_FOLDED, published_gen),
        )
        if not outstanding:
            return 0, False

        legacy = await tx.fetch_one(
            "SELECT 1 FROM metrics WHERE agent_id = ? AND timestamp >= ? AND timestamp < ? "
            "AND id <= ? LIMIT 1",
            (agent_id, lo, hi, cutover),
        )
        summary = await tx.fetch_one(
            "SELECT 1 FROM metrics_downsampled WHERE agent_id = ? AND resolution = ? "
            "AND timestamp = ? LIMIT 1",
            (agent_id, resolution_label, lo),
        )
        provable = ((not legacy)
                    and (bucket is None or bucket["state"] == _BUCKET_OPEN)
                    and (bucket is not None or not summary))
        if not provable:
            return await _mark_pending(tx, agent_id, lo, hi, resolution_label,
                                       cutover, budget), False

        frozen = await tx.fetch_one(
            "SELECT MAX(id) FROM metrics WHERE agent_id = ? AND timestamp >= ? "
            "AND timestamp < ? AND id > ?",
            (agent_id, lo, hi, cutover),
        )
        through = int(frozen[0]) if frozen and frozen[0] is not None else 0
        if not through:
            return 0, False
        state = _agg_new()
        cursor_ts, cursor_id = lo - 1.0, 0
    else:
        state = json.loads(partial["acc"])
        through = partial["through_raw_id"]
        cursor_ts, cursor_id = partial["last_ts"], partial["last_raw_id"]

    # Consumption order IS the averaging order: earliest timestamp first, id
    # breaking ties. The template and the "latest metadata wins" container rule
    # both depend on it, so the resume cursor is that same pair.
    rows = await tx.fetch_all(
        "SELECT id, timestamp, data FROM metrics "
        "WHERE agent_id = ? AND timestamp >= ? AND timestamp < ? AND id > ? AND id <= ? "
        "  AND (timestamp > ? OR (timestamp = ? AND id > ?)) "
        "ORDER BY timestamp, id LIMIT ?",
        (agent_id, lo, hi, cutover, through, cursor_ts, cursor_ts, cursor_id, budget),
    )
    for row in rows:
        _agg_add(state, json.loads(row["data"]))
        await tx.execute(
            "INSERT INTO metric_agg_coverage (raw_id, resolution, agent_id, bucket_ts, state) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(raw_id, resolution) DO UPDATE SET "
            "state = excluded.state, agent_id = excluded.agent_id, "
            "bucket_ts = excluded.bucket_ts",
            (row["id"], resolution_label, agent_id, lo, _COV_FOLDED),
        )
        cursor_ts, cursor_id = row["timestamp"], row["id"]

    remaining = await tx.fetch_one(
        "SELECT 1 FROM metrics WHERE agent_id = ? AND timestamp >= ? AND timestamp < ? "
        "AND id > ? AND id <= ? AND (timestamp > ? OR (timestamp = ? AND id > ?)) LIMIT 1",
        (agent_id, lo, hi, cutover, through, cursor_ts, cursor_ts, cursor_id),
    )
    if remaining:
        await tx.execute(
            "INSERT INTO metric_agg_partial "
            "(agent_id, resolution, bucket_ts, n, last_ts, last_raw_id, through_raw_id, acc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(agent_id, resolution, bucket_ts) DO UPDATE SET "
            "n = excluded.n, last_ts = excluded.last_ts, "
            "last_raw_id = excluded.last_raw_id, through_raw_id = excluded.through_raw_id, "
            "acc = excluded.acc",
            (agent_id, resolution_label, lo, state["n"], cursor_ts, cursor_id, through,
             json.dumps(state)),
        )
        return len(rows), False

    await tx.execute(
        "DELETE FROM metric_agg_partial WHERE agent_id = ? AND resolution = ? AND bucket_ts = ?",
        (agent_id, resolution_label, lo),
    )
    avg = _agg_finalize(state)
    if avg is None:
        return len(rows), False

    await tx.execute(
        "INSERT OR REPLACE INTO metrics_downsampled (agent_id, timestamp, resolution, data) "
        "VALUES (?, ?, ?, ?)",
        (agent_id, lo, resolution_label, json.dumps(avg)),
    )
    # The whole publication, in ONE row: every folded row at or below
    # `through` is proven by the average just written, and nothing above it is.
    await tx.execute(
        "INSERT INTO metric_agg_bucket (agent_id, resolution, bucket_ts, state, published_gen) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(agent_id, resolution, bucket_ts) "
        "DO UPDATE SET state = excluded.state, "
        "published_gen = MAX(published_gen, excluded.published_gen)",
        (agent_id, resolution_label, lo, _BUCKET_OPEN, through),
    )
    return len(rows), True


async def downsample_metrics(resolution_seconds: int, resolution_label: str,
                             batch_limit: int = _AGG_BATCH_LIMIT) -> int:
    """Aggregate raw metrics into downsampled buckets.

    Outstanding work is found by raw id per (agent, resolution), so a fast
    agent's progress can never carry another agent's still-unaggregated rows
    past the scan, and a valid older sample that arrives with a higher id is
    still picked up for its own agent (CP-2).

    The candidate scan asks only for rows that are DUE (their bucket has
    closed) and not yet decided, so a single row dated far in the future
    cannot fill the window and starve the closed buckets behind it. That row
    is not forgotten either: progress is held below it, so it is reconsidered
    on every cycle and folded normally once its bucket actually closes.

    Bounded by ROWS FOLDED, not by rows selected: at most `batch_limit` raw
    rows are decoded, averaged and recorded per call — including the call that
    publishes — however wide the buckets they belong to. Work continues on the
    next maintenance cycle or after a restart, from durable state.

    Read-then-write: the progress query, the raw rows it selects, the summaries
    derived from them and the coverage that proves those summaries all sit in
    ONE boundary, so nothing can commit between the read and the write it feeds
    — and a failure rolls summary, coverage and progress back together."""
    resolution_seconds = _require_resolution(resolution_seconds, resolution_label)
    now = time.time()
    budget = max(1, int(batch_limit))
    # A row is due once the bucket holding it has closed.
    due_cutoff = float(int(now // resolution_seconds) * resolution_seconds)

    async with write_transaction() as tx:
        cutover = await _agg_cutover(tx)
        if cutover is None:
            return 0

        count = 0
        visited: set[tuple[str, int]] = set()

        # 1. Finish what is already in flight. A frozen generation holds its
        #    bucket's rows back from cleanup, so completing it comes first —
        #    and it is reached from the partial table, not from progress.
        for row in await tx.fetch_all(
            "SELECT agent_id, bucket_ts FROM metric_agg_partial WHERE resolution = ? "
            "ORDER BY bucket_ts, agent_id LIMIT ?",
            (resolution_label, budget),
        ):
            if budget <= 0:
                break
            key = (row["agent_id"], int(row["bucket_ts"]))
            visited.add(key)
            used, published = await _advance_bucket(
                tx, key[0], key[1], resolution_seconds, resolution_label, cutover, budget)
            budget -= used
            count += 1 if published else 0

        # 2. Then undecided rows whose bucket has closed.
        candidates = await tx.fetch_all(
            "SELECT m.id AS id, m.agent_id AS agent_id, m.timestamp AS timestamp "
            "FROM metrics m "
            "LEFT JOIN metric_agg_progress p "
            "  ON p.agent_id = m.agent_id AND p.resolution = ? "
            "WHERE m.id > ? AND m.id > COALESCE(p.last_raw_id, ?) "
            "  AND m.timestamp < ? "
            "  AND NOT EXISTS (SELECT 1 FROM metric_agg_coverage c "
            "                  WHERE c.raw_id = m.id AND c.resolution = ?) "
            "ORDER BY m.id LIMIT ?",
            (resolution_label, cutover, cutover, due_cutoff, resolution_label, budget),
        )

        buckets: dict[tuple[str, int], list[int]] = {}
        per_agent: dict[str, list[int]] = {}
        for row in candidates:
            bucket = int(row["timestamp"] // resolution_seconds) * resolution_seconds
            buckets.setdefault((row["agent_id"], bucket), []).append(row["id"])
            per_agent.setdefault(row["agent_id"], []).append(row["id"])

        for key, ids in sorted(buckets.items(), key=lambda kv: min(kv[1])):
            if budget <= 0:
                break
            if key in visited:
                continue
            used, published = await _advance_bucket(
                tx, key[0], key[1], resolution_seconds, resolution_label, cutover, budget)
            budget -= used
            count += 1 if published else 0

        # Progress is a scan floor, never a decision: it stops below the lowest
        # candidate still undecided AND below the lowest row that is not due
        # yet, so nothing is skipped and nothing is forgotten.
        settled = await _settled_ids(tx, resolution_label,
                                     [row["id"] for row in candidates])
        for aid in set(per_agent) | {row["agent_id"] for row in candidates}:
            ids = per_agent.get(aid, [])
            floors = [i for i in ids if i not in settled]
            held = await tx.fetch_one(
                "SELECT MIN(id) FROM metrics "
                "WHERE agent_id = ? AND id > ? AND timestamp >= ?",
                (aid, cutover, due_cutoff),
            )
            if held and held[0] is not None:
                floors.append(int(held[0]))
            advanced = (min(floors) - 1) if floors else max(ids)
            if advanced <= cutover:
                continue
            await tx.execute(
                "INSERT INTO metric_agg_progress (agent_id, resolution, last_raw_id) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(agent_id, resolution) DO UPDATE SET "
                "last_raw_id = MAX(last_raw_id, excluded.last_raw_id)",
                (aid, resolution_label, advanced),
            )

    return count


async def _settled_ids(tx, resolution_label: str, raw_ids: list[int]) -> set[int]:
    """Which of these raw rows now carry coverage, i.e. have been decided.

    A folded row counts as decided even before its generation publishes: the
    generation is frozen and resumed from metric_agg_partial, not from the
    progress floor, so moving the floor past it cannot lose it."""
    settled: set[int] = set()
    for start in range(0, len(raw_ids), 400):   # keep well under SQLITE_MAX_VARIABLE_NUMBER
        chunk = raw_ids[start:start + 400]
        placeholders = ", ".join("?" for _ in chunk)
        rows = await tx.fetch_all(
            "SELECT raw_id FROM metric_agg_coverage "
            f"WHERE resolution = ? AND raw_id IN ({placeholders})",
            (resolution_label, *chunk),
        )
        settled.update(row["raw_id"] for row in rows)
    return settled


_GPU_AVG_FIELDS = ("gpu_util", "mem_util", "temperature", "power_watts", "clock_sm_mhz")


def _agg_new() -> dict:
    """An empty, JSON-serializable fold state.

    The averaging is expressed as new/add/finalize rather than one pass over a
    list so a bucket wider than one batch can be folded across several bounded
    transactions and still produce exactly the same summary: the state IS the
    sufficient statistics (sums, counts and the template), so nothing has to be
    re-read or re-decoded to resume."""
    return {"n": 0, "template": None, "cpu": 0.0, "mem": 0.0, "disk": 0.0,
            "cores": [], "gpu": [], "containers": {},
            "scalar_failed": False, "container_failed": False}


def _agg_add(state: dict, entry: dict) -> None:
    """Fold ONE snapshot in. Entries must arrive in (timestamp, id) order: the
    first is the template, and the last wins for container metadata."""
    if state["template"] is None:
        state["template"] = json.loads(json.dumps(entry))  # deep copy
    state["n"] += 1

    if not state["scalar_failed"]:
        try:
            if state["n"] == 1:
                tmpl = state["template"]
                state["cores"] = [0.0] * len(
                    tmpl.get("cpu", {}).get("percent_per_core", []))
                state["gpu"] = [{"n": 0, "sum": dict.fromkeys(_GPU_AVG_FIELDS, 0.0)}
                                for _ in tmpl.get("gpu", [])]
            state["cpu"] += entry.get("cpu", {}).get("percent_total", 0)
            state["mem"] += entry.get("memory", {}).get("percent", 0)
            state["disk"] += entry.get("disk", {}).get("percent", 0)
            cores = len(state["cores"])
            if cores:
                per_core = entry.get("cpu", {}).get("percent_per_core", [0] * cores)
                for ci in range(cores):
                    state["cores"][ci] += per_core[ci]
            entry_gpus = entry.get("gpu", [])
            for gi, dev in enumerate(state["gpu"]):
                # Same divisor rule as before: a device only counts for the
                # snapshots that actually reported it.
                if len(entry_gpus) > gi:
                    dev["n"] += 1
                    for field in _GPU_AVG_FIELDS:
                        dev["sum"][field] += entry_gpus[gi].get(field, 0)
        # AttributeError belongs here too: a sample whose "cpu", a "gpu"
        # entry or "containers" arrives as a scalar instead of a mapping
        # reaches .get() on a str, which is precisely the malformed shape
        # these two latches exist to contain. Nothing between the agent
        # socket and store_metric checks the payload, so it is reachable.
        # Letting it escape aborts the whole fold, and retention only
        # reclaims a row once a summary covers it — so one bad row would
        # stop aggregation AND deletion for every agent, permanently.
        except (AttributeError, KeyError, IndexError, TypeError):
            state["scalar_failed"] = True

    if not state["container_failed"]:
        try:
            _agg_add_containers(state["containers"], entry)
        except (AttributeError, KeyError, IndexError, TypeError):
            state["container_failed"] = True


def _agg_add_containers(agg: dict, entry: dict) -> None:
    """Aggregate per-container CPU/Mem by name. Containers can come and go within
    a bucket; keying by name keeps history continuous across restart/recreate
    while id changes. Latest non-zero mem_limit wins (limits can be updated)."""
    for c in entry.get("containers") or []:
        name = c.get("name")
        if not name:
            continue
        s = agg.get(name)
        if s is None:
            s = {
                "name": name,
                "id": c.get("id", ""),
                "image": c.get("image", ""),
                "status": c.get("status", ""),
                "state": c.get("state", ""),
                "ports": c.get("ports", []),
                "cpu_sum": 0.0,
                "mem_sum": 0.0,
                "samples": 0,
                "mem_limit": c.get("mem_limit", 0),
                "gpu_vram_sum": 0.0,
                "gpu_util_sum": 0.0,
                "gpu_vram_samples": 0,
                "gpu_util_samples": 0,
                "gpu_seen": False,
            }
            agg[name] = s
        s["cpu_sum"] += float(c.get("cpu_percent", 0) or 0)
        s["mem_sum"] += float(c.get("mem_usage", 0) or 0)
        s["samples"] += 1
        # Use the most recent metadata so the UI shows current status/image.
        s["status"] = c.get("status", s["status"])
        s["state"] = c.get("state", s["state"])
        s["image"] = c.get("image", s["image"])
        s["id"] = c.get("id", s["id"])
        s["ports"] = c.get("ports", s["ports"])
        ml = c.get("mem_limit", 0) or 0
        if ml > 0:
            s["mem_limit"] = ml
        # Average VRAM/SM only across samples where the container actually held
        # GPU memory or was running compute — averaging zeros into the divisor
        # would understate usage for workloads that allocate intermittently.
        # gpu_reserved=true with zero util/vram still creates the field so the
        # downsampled history shows the idle period instead of dropping out.
        gpu = c.get("gpu")
        if isinstance(gpu, dict):
            vram = float(gpu.get("vram_bytes", 0) or 0)
            util = float(gpu.get("gpu_util", 0) or 0)
            if vram > 0:
                s["gpu_vram_sum"] += vram
                s["gpu_vram_samples"] = s.get("gpu_vram_samples", 0) + 1
            if util > 0:
                s["gpu_util_sum"] += util
                s["gpu_util_samples"] = s.get("gpu_util_samples", 0) + 1
            s["gpu_seen"] = True


def _agg_finalize(state: dict) -> dict | None:
    """Turn the fold state into one downsampled snapshot."""
    if not state["n"] or state["template"] is None:
        return None

    result = json.loads(json.dumps(state["template"]))  # deep copy
    n = state["n"]

    try:
        if not state["scalar_failed"]:
            result["cpu"]["percent_total"] = state["cpu"] / n
            result["memory"]["percent"] = state["mem"] / n
            result["disk"]["percent"] = state["disk"] / n

            cores = len(result.get("cpu", {}).get("percent_per_core", []))
            if cores:
                for ci in range(cores):
                    result["cpu"]["percent_per_core"][ci] = state["cores"][ci] / n

            for gi, gpu in enumerate(result.get("gpu", [])):
                dev = state["gpu"][gi]
                for field in _GPU_AVG_FIELDS:
                    gpu[field] = dev["sum"][field] / dev["n"] if dev["n"] else 0
                # Remove per-snapshot process data from downsampled
                gpu.pop("processes", None)

            if not state["container_failed"]:
                out_containers = []
                for s in state["containers"].values():
                    entry = {
                        "id": s["id"],
                        "name": s["name"],
                        "image": s["image"],
                        "status": s["status"],
                        "state": s["state"],
                        "ports": s["ports"],
                        "cpu_percent": s["cpu_sum"] / s["samples"] if s["samples"] else 0,
                        "mem_usage": s["mem_sum"] / s["samples"] if s["samples"] else 0,
                        "mem_limit": s["mem_limit"],
                    }
                    if s["gpu_seen"]:
                        entry["gpu"] = {
                            "vram_bytes": (s["gpu_vram_sum"] / s["gpu_vram_samples"]
                                           if s["gpu_vram_samples"] else 0),
                            "gpu_util": (s["gpu_util_sum"] / s["gpu_util_samples"]
                                         if s["gpu_util_samples"] else 0),
                        }
                    out_containers.append(entry)
                result["containers"] = out_containers
    except (AttributeError, KeyError, IndexError, TypeError):
        pass

    # Drop heavy fields for downsampled data
    result.pop("processes", None)
    result.pop("network", None)

    return result


def _average_metrics(entries: list[dict]) -> dict | None:
    """Average numeric fields from a list of metric snapshots."""
    state = _agg_new()
    for entry in entries:
        _agg_add(state, entry)
    return _agg_finalize(state)


def _covered_clause(resolution_label: str) -> str:
    """Proof that ONE raw row is represented in `resolution_label`'s history.

    Every link is checked, not just the label: the coverage must belong to the
    same agent as the raw row, the bucket's PUBLISHED generation must have
    reached this row's id (a folded row inside an unpublished generation
    proves nothing), an actual metrics_downsampled row must occupy exactly
    that (agent, resolution, timestamp), and that bucket must not be
    mid-recomputation."""
    return (
        "EXISTS (SELECT 1 FROM metric_agg_coverage c "
        "        JOIN metric_agg_bucket b "
        "          ON b.agent_id = c.agent_id AND b.resolution = c.resolution "
        "         AND b.bucket_ts = c.bucket_ts "
        "        JOIN metrics_downsampled d "
        "          ON d.agent_id = c.agent_id AND d.resolution = c.resolution "
        "         AND d.timestamp = c.bucket_ts "
        f"        WHERE c.raw_id = m.id AND c.resolution = '{resolution_label}' "
        "          AND c.state = 'FOLDED' AND c.agent_id = m.agent_id "
        "          AND m.id <= b.published_gen "
        "          AND NOT EXISTS (SELECT 1 FROM metric_agg_partial p "
        "                          WHERE p.agent_id = c.agent_id "
        "                            AND p.resolution = c.resolution "
        "                            AND p.bucket_ts = c.bucket_ts))"
    )


_DELETABLE_RAW_SQL = (
    "SELECT m.id AS id FROM metrics m "
    "WHERE m.timestamp < ? AND m.id > ? AND "
    + " AND ".join(_covered_clause(label) for label in _AGG_RESOLUTIONS)
    + " ORDER BY m.id LIMIT ?"
)

# The 7-day sweeps are DEPENDENCY-ORDERED, not purely age-ordered. A summary
# that a surviving raw row still needs as its deletion proof is kept until that
# raw row is gone, otherwise a bounded cleanup that reclaims part of a bucket
# strands the rest of it forever. Both sweeps drive off a leading-column index
# (idx_metrics_ds_ts / idx_metric_agg_bucket_ts) so neither scans its table.
_PRUNE_SUMMARY_SQL = (
    "DELETE FROM metrics_downsampled WHERE rowid IN ("
    "  SELECT d.rowid FROM metrics_downsampled d "
    "  WHERE d.timestamp < ? "
    "    AND NOT EXISTS (SELECT 1 FROM metric_agg_coverage c "
    "                    WHERE c.agent_id = d.agent_id AND c.resolution = d.resolution "
    "                      AND c.bucket_ts = d.timestamp AND c.state = 'FOLDED') "
    "    AND NOT EXISTS (SELECT 1 FROM metric_agg_partial p "
    "                    WHERE p.agent_id = d.agent_id AND p.resolution = d.resolution "
    "                      AND p.bucket_ts = d.timestamp) "
    "  LIMIT ?)"
)

_PRUNE_BUCKET_SQL = (
    "DELETE FROM metric_agg_bucket WHERE rowid IN ("
    "  SELECT b.rowid FROM metric_agg_bucket b "
    "  WHERE b.bucket_ts < ? "
    "    AND NOT EXISTS (SELECT 1 FROM metrics_downsampled d "
    "                    WHERE d.agent_id = b.agent_id AND d.resolution = b.resolution "
    "                      AND d.timestamp = b.bucket_ts) "
    "    AND NOT EXISTS (SELECT 1 FROM metric_agg_coverage c "
    "                    WHERE c.agent_id = b.agent_id AND c.resolution = b.resolution "
    "                      AND c.bucket_ts = b.bucket_ts) "
    "    AND NOT EXISTS (SELECT 1 FROM metric_agg_partial p "
    "                    WHERE p.agent_id = b.agent_id AND p.resolution = b.resolution "
    "                      AND p.bucket_ts = b.bucket_ts) "
    "  LIMIT ?)"
)

# Sealing is driven FROM the doomed rows' coverage, not by hunting the bucket
# table for OPEN rows: `state` is not a leading index column, so a predicate on
# it alone scans all seven days of provenance on every cleanup cycle. Looking
# the triples up by raw_id (the coverage PK's leading column) and then seeking
# each bucket by its own PK keeps both halves index-bound.
_SEAL_LOOKUP_SQL = (
    "SELECT DISTINCT agent_id, resolution, bucket_ts FROM metric_agg_coverage "
    "WHERE raw_id IN ({placeholders})"
)

_SEAL_BUCKET_SQL = (
    "UPDATE metric_agg_bucket SET state = ? "
    "WHERE agent_id = ? AND resolution = ? AND bucket_ts = ? AND state = ?"
)

_PRUNE_COVERAGE_SQL = "DELETE FROM metric_agg_coverage WHERE raw_id IN ({placeholders})"


def _cleanup_query_plans(now: float, max_age_hours: int = 1,
                         batch_limit: int = _CLEANUP_BATCH_LIMIT) -> list[tuple[str, tuple]]:
    """The statements the periodic cleanup runs, with representative
    parameters. Exposed so their query plans can be inspected: this cleanup is
    on the 5-minute maintenance path, and a full scan of the summary or
    coverage state here is the cost that made per-raw retention untenable."""
    return [
        (_DELETABLE_RAW_SQL, (now - max_age_hours * 3600, 0, batch_limit)),
        (_SEAL_LOOKUP_SQL.format(placeholders="?, ?"), (1, 2)),
        (_SEAL_BUCKET_SQL, (_BUCKET_SEALED, "a", "1m", 0.0, _BUCKET_OPEN)),
        (_PRUNE_COVERAGE_SQL.format(placeholders="?, ?"), (1, 2)),
        (_PRUNE_SUMMARY_SQL, (now - 7 * 86400, batch_limit)),
        (_PRUNE_BUCKET_SQL, (now - 7 * 86400, batch_limit)),
    ]


async def cleanup_old_metrics(max_age_hours: int = 1,
                              batch_limit: int = _CLEANUP_BATCH_LIMIT) -> int:
    """Delete raw metrics that are old AND provably represented, right now, by
    a real summary at BOTH resolutions. Downsampled data kept for 7 days.

    Age alone never authorizes a delete (CP-3), and neither does a coverage
    label on its own: the label is re-joined to the live metrics_downsampled
    row it claims, in this transaction, immediately before the delete. A
    summary that was pruned, overwritten or never published leaves its raw
    rows in place and this returns 0.

    Deleting the raw rows, dropping their per-raw coverage and sealing the
    buckets they belonged to happen together in this one transaction. Sealing
    is what keeps the promise afterwards: once a contributing raw is gone, the
    bucket can never be recomputed, so a later arrival is preserved instead of
    silently rewriting an average it cannot reconstruct.

    A future-dated row is NOT deleted on its timestamp alone — neither raw nor
    summary. The ingest path clamps forged timestamps (LOGIC-08); a row that
    predates that clamp or bypassed it is kept until it is genuinely
    summarized, and a wall clock that steps backwards must not be able to
    destroy history that is perfectly valid."""
    now = time.time()

    raw_cutoff = now - (max_age_hours * 3600)
    ds_cutoff = now - (7 * 86400)
    limit = max(1, int(batch_limit))
    async with write_transaction() as tx:
        cutover = await _agg_cutover(tx)
        raw_deleted = 0
        if cutover is not None:
            doomed = [row["id"] for row in await tx.fetch_all(
                _DELETABLE_RAW_SQL, (raw_cutoff, cutover, limit))]
            for start in range(0, len(doomed), 400):
                chunk = doomed[start:start + 400]
                placeholders = ", ".join("?" for _ in chunk)
                # Seal FIRST, from the coverage that is about to go: after this
                # transaction there is no longer any record of which rows the
                # summary was built from, only that it can no longer be rebuilt.
                seals = await tx.fetch_all(
                    _SEAL_LOOKUP_SQL.format(placeholders=placeholders), tuple(chunk))
                if seals:
                    await tx.executemany(_SEAL_BUCKET_SQL, [
                        (_BUCKET_SEALED, row["agent_id"], row["resolution"],
                         row["bucket_ts"], _BUCKET_OPEN)
                        for row in seals
                    ])
                await tx.execute(
                    _PRUNE_COVERAGE_SQL.format(placeholders=placeholders), tuple(chunk))
                result = await tx.execute(
                    f"DELETE FROM metrics WHERE id IN ({placeholders})", tuple(chunk))
                raw_deleted += result.rowcount

        # Downsampled: keep 7 days. Bounded, and NO future-timestamp clause —
        # see the docstring.
        await tx.execute(_PRUNE_SUMMARY_SQL, (ds_cutoff, limit))
        # Bucket provenance is only needed while a summary it describes can
        # still be read back, so it ages out on the same 7-day boundary.
        await tx.execute(_PRUNE_BUCKET_SQL, (ds_cutoff, limit))
    return raw_deleted


async def store_net_audit(agent_id: str, ts: float, events: list, rollups: list) -> None:
    """Persist connection events + minute rollups. Metadata only — no payloads."""
    async with write_transaction() as tx:
        if events:
            await tx.executemany(
                "INSERT INTO net_conn_events "
                "(agent_id, ts, event, proto, laddr, lport, raddr, rport, status, pid, pname, duration) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [(agent_id, float(e.get("ts", ts)), str(e.get("event", "")), str(e.get("proto", "")),
                  e.get("laddr"), e.get("lport"), e.get("raddr"), e.get("rport"),
                  e.get("status"), e.get("pid"), e.get("pname"), e.get("duration"))
                 for e in events],
            )
        for r in rollups:
            await tx.execute(
                "INSERT OR REPLACE INTO net_flow_rollup (agent_id, ts, data) VALUES (?,?,?)",
                (agent_id, float(r.get("ts", ts)), json.dumps(
                    {"interfaces": r.get("interfaces", []), "top_talkers": r.get("top_talkers", [])})),
            )


async def cleanup_net_audit(event_days: int = 7, rollup_days: int = 30) -> int:
    now = time.time()
    future = now + 300
    async with write_transaction() as tx:
        result = await tx.execute(
            "DELETE FROM net_conn_events WHERE ts < ? OR ts > ?",
            (now - event_days * 86400, future),
        )
        deleted = result.rowcount
        await tx.execute(
            "DELETE FROM net_flow_rollup WHERE ts < ? OR ts > ?",
            (now - rollup_days * 86400, future),
        )
    return deleted


async def get_net_conn_events(agent_id: str, before_ts: float | None = None,
                              before_id: int | None = None, limit: int = 200,
                              proto: str | None = None, raddr: str | None = None,
                              port: int | None = None, pid: int | None = None) -> list:
    clauses = ["agent_id = ?"]
    params: list = [agent_id]
    # Keyset pagination on (ts, id): every event from one collect tick shares the same
    # server ts, and a tick can hold more rows than one page — a ts-only cursor would
    # skip the same-ts rows past the page boundary. (ts, id) is a stable total order so
    # no row is skipped or duplicated across pages (review P2).
    if before_ts is not None and before_id is not None:
        clauses.append("(ts < ? OR (ts = ? AND id < ?))")
        params.extend([before_ts, before_ts, before_id])
    elif before_ts is not None:
        clauses.append("ts < ?"); params.append(before_ts)
    if proto:
        clauses.append("proto = ?"); params.append(proto)
    if raddr:
        clauses.append("raddr = ?"); params.append(raddr)
    if port is not None:
        clauses.append("(lport = ? OR rport = ?)"); params.extend([port, port])
    if pid is not None:
        clauses.append("pid = ?"); params.append(pid)
    params.append(max(1, min(limit, 1000)))  # clamp lower bound too: SQLite LIMIT -1 is unbounded (review P2)
    rows = await _fetch_all(
        f"SELECT id, ts, event, proto, laddr, lport, raddr, rport, status, pid, pname, duration "
        f"FROM net_conn_events WHERE {' AND '.join(clauses)} ORDER BY ts DESC, id DESC LIMIT ?",
        params,
    )
    return [dict(r) for r in rows]


async def get_net_flow_rollup(agent_id: str, start: float, end: float) -> list:
    rows = await _fetch_all(
        "SELECT ts, data FROM net_flow_rollup WHERE agent_id = ? AND ts >= ? AND ts <= ? ORDER BY ts ASC",
        (agent_id, start, end),
    )
    out = []
    for r in rows:
        d = json.loads(r["data"])
        out.append({"ts": r["ts"], "interfaces": d.get("interfaces", []),
                    "top_talkers": d.get("top_talkers", [])})
    return out


# ── Alert config ─────────────────────────────────────


async def get_alert_config_row() -> str | None:
    """Raw stored alert/SMTP config JSON, or None. A named accessor rather than
    a public generic read helper — the read path stays module-private."""
    row = await _fetch_one("SELECT config FROM alert_config WHERE id = 1")
    return row["config"] if row else None


# ── Users ────────────────────────────────────────────


async def get_user(email: str) -> dict | None:
    row = await _fetch_one("SELECT * FROM users WHERE email = ?", (email,))
    if not row:
        return None
    return {
        "email": row["email"],
        "password_hash": row["password_hash"],
        "totp_secret": row["totp_secret"],
        "totp_enabled": bool(row["totp_enabled"]),
        "must_change_password": bool(row["must_change_password"]),
        "role": row["role"] if "role" in row.keys() else "user",
        "is_active": bool(row["is_active"]) if "is_active" in row.keys() else True,
        "tokens_valid_after": row["tokens_valid_after"] if "tokens_valid_after" in row.keys() else 0,
        "created_at": row["created_at"],
    }


async def list_users() -> list[dict]:
    rows = await _fetch_all(
        "SELECT email, role, is_active, totp_enabled, must_change_password, created_at "
        "FROM users ORDER BY created_at ASC"
    )
    return [
        {
            "email": r["email"],
            "role": r["role"],
            "is_active": bool(r["is_active"]),
            "totp_enabled": bool(r["totp_enabled"]),
            "must_change_password": bool(r["must_change_password"]),
            "created_at": r["created_at"],
        }
        for r in rows
    ]


async def create_user(email: str, password_hash: str, role: str = "user", must_change_password: bool = True) -> bool:
    try:
        async with write_transaction() as tx:
            await tx.execute(
                "INSERT INTO users (email, password_hash, must_change_password, created_at, role, is_active) "
                "VALUES (?, ?, ?, ?, ?, 1)",
                (email, password_hash, 1 if must_change_password else 0, time.time(), role),
            )
        return True
    except DatabaseFailStop:
        # A restart-required DB is not a "this email is taken" style failure —
        # reporting it as a plain False would tell the admin to try again on a
        # database that cannot accept any write.
        raise
    except Exception:
        logger.exception("create_user failed for %s", email)
        return False


async def delete_user(email: str) -> bool:
    async with write_transaction() as tx:
        result = await tx.execute("DELETE FROM users WHERE email = ?", (email,))
    return result.rowcount > 0


async def count_active_admins() -> int:
    row = await _fetch_one(
        "SELECT COUNT(*) FROM users WHERE role = 'admin' AND is_active = 1"
    )
    return row[0]


async def get_user_host_accounts(user_email: str) -> dict[str, str]:
    rows = await _fetch_all(
        "SELECT agent_id, host_user FROM user_host_accounts WHERE user_email = ?",
        (user_email,),
    )
    return {r["agent_id"]: r["host_user"] for r in rows}


async def set_user_host_accounts(user_email: str, mapping: dict[str, str]) -> None:
    """Replace the user's host account map atomically. Empty `host_user` removes the entry."""
    async with write_transaction() as tx:
        await tx.execute("DELETE FROM user_host_accounts WHERE user_email = ?", (user_email,))
        for agent_id, host_user in mapping.items():
            if not host_user:
                continue
            await tx.execute(
                "INSERT INTO user_host_accounts (user_email, agent_id, host_user) VALUES (?, ?, ?)",
                (user_email, agent_id, host_user),
            )


# ── Runtime Config ───────────────────────────────────


async def get_runtime_config() -> dict[str, str]:
    rows = await _fetch_all("SELECT key, value FROM runtime_config")
    return {row["key"]: row["value"] for row in rows}


async def set_runtime_config(key: str, value: str) -> None:
    from app.runtime_config_validate import validate_config_value
    ALLOWED_KEYS = {
        "enable_gpu", "enable_docker", "collect_interval",
        "terminal_user", "allowed_ips",
    }
    if key not in ALLOWED_KEYS:
        return
    validate_config_value(key, value)  # defense-in-depth: reject bad values at the setter too
    async with write_transaction() as tx:
        await tx.execute(
            "INSERT OR REPLACE INTO runtime_config (key, value) VALUES (?, ?)",
            (key, value),
        )


async def set_runtime_configs(configs: dict[str, str]) -> None:
    for key, value in configs.items():
        await set_runtime_config(key, value)


# ── Token Blacklist ──────────────────────────────────


async def blacklist_token(token_hash: str, expires_at: float) -> None:
    async with write_transaction() as tx:
        await tx.execute(
            "INSERT OR IGNORE INTO token_blacklist (token_hash, expires_at) VALUES (?, ?)",
            (token_hash, expires_at),
        )


async def is_token_blacklisted(token_hash: str) -> bool:
    row = await _fetch_one(
        "SELECT 1 FROM token_blacklist WHERE token_hash = ?", (token_hash,)
    )
    return row is not None


async def cleanup_blacklist() -> int:
    async with write_transaction() as tx:
        result = await tx.execute(
            "DELETE FROM token_blacklist WHERE expires_at < ?", (time.time(),)
        )
    return result.rowcount


# ── Audit log ────────────────────────────────────────


async def audit(user: str, action: str, agent_id: str = "", detail: dict | None = None) -> None:
    """Best-effort audit insert — never raises, so it can't block the action it
    records. A wedged audit DB must not stop an admin from killing a runaway process."""
    try:
        async with write_transaction() as tx:
            await tx.execute(
                "INSERT INTO audit_log (timestamp, user_email, action, agent_id, detail) "
                "VALUES (?, ?, ?, ?, ?)",
                (time.time(), (user or "")[:256], action, agent_id or "", json.dumps(detail or {})),
            )
    except Exception:
        # Still best-effort: the boundary has already closed its cursors and
        # ended the transaction by the time this runs, so swallowing the error
        # cannot leave the connection wedged.
        logger.exception("audit insert failed: %s %s", user, action)


async def get_audit_log(limit: int = 200, before: float | None = None,
                        user: str | None = None, action: str | None = None) -> list[dict]:
    clauses, params = [], []
    if before is not None:
        clauses.append("timestamp < ?"); params.append(before)
    if user:
        clauses.append("user_email = ?"); params.append(user)
    if action:
        clauses.append("action = ?"); params.append(action)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(max(1, min(limit, 1000)))
    rows = await _fetch_all(
        f"SELECT timestamp, user_email, action, agent_id, detail FROM audit_log{where} "
        "ORDER BY timestamp DESC LIMIT ?", params,
    )
    out = []
    for r in rows:
        try:
            detail = json.loads(r["detail"])
        except (ValueError, TypeError):
            detail = {}
        out.append({"timestamp": r["timestamp"], "user": r["user_email"],
                    "action": r["action"], "agent_id": r["agent_id"], "detail": detail})
    return out


async def cleanup_audit_log(max_age_days: int = 90, max_rows: int = 100_000) -> int:
    """Two-tier prune: drop rows older than max_age_days, then FIFO-cap at max_rows."""
    cutoff = time.time() - max_age_days * 86400
    async with write_transaction() as tx:
        result = await tx.execute("DELETE FROM audit_log WHERE timestamp < ?", (cutoff,))
        removed = result.rowcount
        result = await tx.execute(
            "DELETE FROM audit_log WHERE id NOT IN "
            "(SELECT id FROM audit_log ORDER BY id DESC LIMIT ?)", (max_rows,),
        )
        removed += result.rowcount
    return removed


# ── Users ────────────────────────────────────────────

_ALLOWED_USER_FIELDS = {"password_hash", "totp_secret", "totp_enabled", "must_change_password", "role", "is_active", "tokens_valid_after"}


async def update_user(email: str, **fields) -> bool:
    if not fields:
        return False
    # Whitelist columns to prevent SQL injection via key names
    safe_fields = {k: v for k, v in fields.items() if k in _ALLOWED_USER_FIELDS}
    if not safe_fields:
        return False
    set_clause = ", ".join(f"{k} = ?" for k in safe_fields)
    values = list(safe_fields.values()) + [email]
    async with write_transaction() as tx:
        await tx.execute(f"UPDATE users SET {set_clause} WHERE email = ?", values)
    return True
