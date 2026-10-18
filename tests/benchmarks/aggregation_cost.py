"""Measured-service, synthetic-arrival benchmark using the production scheduler.

The clock advances virtually; the only database is this call's in-memory SQLite.
This module never constructs a model client, dispatcher or external transport.
"""
from __future__ import annotations

from datetime import datetime
import math
import statistics
from zoneinfo import ZoneInfo

from server.bond_tools import BondTools
from server.negotiation.application_store import ApplicationStore
from server.negotiation.engine import Engine
from server.negotiation.prices import LimitPolicy
from server.negotiation.service import Inquiry
from server.negotiation.settings import Aggregation


def _positive(value, label):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(label+' must be finite and positive')
    return float(value)


def candidate_windows(reference_ms):
    reference_ms = _positive(reference_ms, 'reference_ms')
    upper = math.ceil(reference_ms / 2) - 1
    grid = sorted({k * upper // 4 for k in range(5)})
    pairs = [(quiet, cap) for quiet in grid for cap in grid if quiet <= cap]
    for quiet, cap in pairs:
        Aggregation(quiet, cap, reference_ms, 'synthetic-arrival-benchmark')
    return pairs


def arrival_traces(reference_ms, analysis_ms, reply_ms):
    reference_ms = _positive(reference_ms, 'reference_ms')
    analysis_ms = _positive(analysis_ms, 'analysis_ms')
    reply_ms = _positive(reply_ms, 'reply_ms')
    hop = max(1, math.floor(reference_ms / 20))
    return {
        'single': [0],
        'simultaneous': [0, 0, 0, 0],
        'short_burst': [index * hop for index in range(4)],
        'spaced_burst': [index * 2 * hop for index in range(4)],
        'finite_continuous': [index * hop for index in range(21)],
        'inflight': sorted([0, max(0, analysis_ms-1), analysis_ms+1,
                            max(0, analysis_ms+reply_ms-1), analysis_ms+reply_ms+1]),
    }


def score_trace(*, analysis_calls, reply_calls, analysis_ms, reply_ms, window_waits_ms):
    for count in (analysis_calls, reply_calls):
        if type(count) is not int or count < 0:
            raise ValueError('Call counts must be nonnegative integers')
    analysis_ms = _positive(analysis_ms, 'analysis_ms')
    reply_ms = _positive(reply_ms, 'reply_ms')
    if not window_waits_ms or any(type(value) not in (int, float) or not math.isfinite(value) or value < 0
                                  for value in window_waits_ms):
        raise ValueError('Every arrival requires a finite nonnegative window wait')
    delay = statistics.mean(window_waits_ms)
    extra = max(0.0, analysis_calls*analysis_ms + reply_calls*reply_ms - analysis_ms - reply_ms)
    return {'D_ms': delay, 'extra_ms': extra, 'J_ms': delay+extra}


class _ObservedEngine(Engine):
    """Observe actual public entry results, leaving all transaction gates intact."""
    def __init__(self, simulation, *args, **kwargs):
        self.simulation = simulation
        super().__init__(*args, **kwargs)

    def analyze_thread(self, owner, thread_id, *, force=False):
        simulation = self.simulation
        with self.store.db.transaction(write=False) as tx:
            simulation.claim_thread = dict(self.store._thread(tx, owner, thread_id))
        before = len(simulation.analysis_events)
        result = super().analyze_thread(owner, thread_id, force=force)
        if len(simulation.analysis_events) != before:
            event = simulation.analysis_events[-1]
            if not result or not result.get('facts_committed') or result.get('rejected'):
                raise RuntimeError('Synthetic analysis failed its real fact gate: '+repr(result))
            event['state'] = 'COMMITTED'
            event['accepted_observations'] = len(result['accepted'])
            event['finished_ms'] = simulation.now_ms
            for message_id in event['message_ids']:
                simulation.by_id[message_id]['facts_ms'] = simulation.now_ms
        return result

    def decide_member(self, owner, member_id):
        simulation = self.simulation
        before = len(simulation.reply_events)
        started = simulation.now_ms
        with self.store.db.transaction(write=False) as tx:
            thread = self.store._thread(tx, owner, simulation.thread_id)
            pending = thread['analyzed_revision'] < thread['ingest_revision']
        result = super().decide_member(owner, member_id)
        invoked = len(simulation.reply_events) != before
        simulation.reply_attempts.append({'at_ms': started, 'pending_analysis': pending,
                                          'invoked': invoked, 'result_state': result.get('state') if result else None})
        if invoked:
            if not result or result.get('state') not in ('WAIT', 'REJECTED'):
                raise RuntimeError('Synthetic reply failed outside the modeled gates: '+repr(result))
            event = simulation.reply_events[-1]
            event.update(state=result['state'], reason=result.get('error', {}).get('reason'),
                         finished_ms=simulation.now_ms)
        return result


class _Simulation:
    owner = 'synthetic-aggregation-owner'
    day = '2026-10-17'

    def __init__(self, arrivals, analysis_ms, reply_ms, aggregation):
        self.arrivals = arrivals
        self.analysis_ms, self.reply_ms = analysis_ms, reply_ms
        self.aggregation = aggregation
        self.base = datetime(2026, 10, 17, 12, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        self.now_ms = 0.0
        self.next_arrival = 0
        self.message_events, self.analysis_events, self.reply_events, self.reply_attempts = [], [], [], []
        self.by_id = {}
        self.claim_thread = None
        self.store = ApplicationStore('sqlite:///:memory:', clock=self.clock, initialize=True)
        self.store.bind_account(self.owner, 'synthetic-aggregation-account', 'synthetic-aggregation-workbench')
        group = self.store.create_group(self.owner, bond_code='SYN-A', our_side='BUY',
            target_volume='1000000', trade_day=self.day, policy=LimitPolicy('ABSOLUTE', '3.12'), send_allowed=True)
        self.member_id, self.thread_id = self.store.add_member(self.owner, group,
            account_uid='synthetic-aggregation-account', peer_uid='synthetic-aggregation-peer')
        self.store.set_sending(self.owner, True)
        self.service = Inquiry(self.store)
        self.engine = _ObservedEngine(self, self.service, self, BondTools(None), aggregation=aggregation)

    def clock(self):
        return self.base + self.now_ms / 1000

    def advance(self, target):
        if target < self.now_ms:
            raise ValueError('Virtual time must not move backward')
        while self.next_arrival < len(self.arrivals) and self.arrivals[self.next_arrival] <= target:
            index = self.next_arrival
            self.now_ms = self.arrivals[index]
            message_id = self.store.ingest(self.owner, self.thread_id, event_id='synthetic-arrival-'+str(index),
                source_seq=index+1, text='SYN-A 3.10出1000万', trade_day=self.day, occurred_at=self.clock())
            event = {'index': index, 'message_id': message_id, 'received_ms': self.now_ms,
                     'snapshot_ms': None, 'facts_ms': None, 'window_wait_ms': None, 'scheduling_wait_ms': None}
            self.message_events.append(event)
            self.by_id[message_id] = event
            self.next_arrival += 1
        self.now_ms = target

    def analyze(self, snapshot):
        started = self.now_ms
        pending = (self.claim_thread['pending_since']-self.base)*1000
        latest = (self.claim_thread['last_ingest_at']-self.base)*1000
        ready_at = min(pending+self.aggregation.max_wait_ms, latest+self.aggregation.quiet_ms)
        messages = snapshot['messages']
        event = {'started_ms': started, 'finished_ms': None, 'message_ids': [m['message_id'] for m in messages],
                 'through_revision': snapshot['through_revision'], 'state': 'RUNNING',
                 'ready_at_ms': ready_at, 'first_pending_ms': pending, 'last_received_ms': latest}
        self.analysis_events.append(event)
        observations = []
        for message in messages:
            tracked = self.by_id[message['message_id']]
            if tracked['snapshot_ms'] is not None:
                raise RuntimeError('An input was included in two new analysis snapshots')
            arrival = tracked['received_ms']
            tracked.update(snapshot_ms=started, window_wait_ms=max(0.0, ready_at-arrival),
                           scheduling_wait_ms=max(0.0, started-max(ready_at, arrival)))
            changes = {'price': {'kind': 'YIELD', 'value': '3.10'}, 'volume_wan': '1000',
                       'assertion': 'QUOTE', 'validity': 'ACTIVE', 'finality': 'UNKNOWN', 'confirmed_day': self.day}
            observations.append({'member_id': self.member_id, 'side': 'ofr', 'changes': changes,
                'evidence': {field: [message['message_id']] for field in changes},
                'segments': [{'message_id': message['message_id'], 'text': message['text']}],
                'interpretation': {'quote_owner': 'COUNTERPARTY', 'intent': 'QUOTE',
                                   'negotiation_space': 'UNKNOWN', 'reason': '合成时序基准本人报价'}})
        self.advance(started+self.analysis_ms)
        return {'schema_version': '2.2', 'scopes': {m['message_id']: [self.member_id] for m in messages},
                'observations': observations}

    def decide(self, context):
        started = self.now_ms
        self.reply_events.append({'started_ms': started, 'finished_ms': None, 'state': 'RUNNING', 'reason': None})
        self.advance(started+self.reply_ms)
        return {'schema_version': '2.2', 'action': 'WAIT', 'proposal': None, 'trader_view': {}}

    def run(self, phase):
        self.advance(phase)
        for _ in range(10000):
            self.engine.run_once(force=False)
            with self.store.db.transaction(write=False) as tx:
                thread = self.store._thread(tx, self.owner, self.thread_id)
                remaining = thread['ingest_revision'] - thread['analyzed_revision']
                command_count = tx.execute('SELECT COUNT(*) AS n FROM xj2_command').fetchone()['n']
            if (self.next_arrival == len(self.arrivals) and remaining == 0 and self.reply_events
                    and self.reply_events[-1]['state'] == 'WAIT'
                    and self.reply_events[-1]['started_ms'] >= self.analysis_events[-1]['finished_ms']):
                if command_count or any(row['facts_ms'] is None for row in self.message_events):
                    raise RuntimeError('Synthetic run sent a command or lost visible facts')
                waits = [row['window_wait_ms'] for row in self.message_events]
                return {'analysis_calls': len(self.analysis_events), 'reply_calls': len(self.reply_events),
                        'window_waits_ms': waits, 'drained': True, 'remaining_messages': 0, 'external_im_sent': False,
                        'analysis_events': self.analysis_events, 'reply_events': self.reply_events,
                        'reply_attempts': self.reply_attempts, 'message_events': self.message_events, 'end_ms': self.now_ms,
                        'command_count': command_count,
                        **score_trace(analysis_calls=len(self.analysis_events), reply_calls=len(self.reply_events),
                                      analysis_ms=self.analysis_ms, reply_ms=self.reply_ms, window_waits_ms=waits)}
            self.advance(self.now_ms+250)
        raise RuntimeError('Finite synthetic input did not drain within the bounded loop')


def simulate_trace(*, arrivals_ms, analysis_ms, reply_ms, quiet_ms, max_wait_ms, reference_ms, poll_phase_ms=0):
    analysis_ms = _positive(analysis_ms, 'analysis_ms')
    reply_ms = _positive(reply_ms, 'reply_ms')
    if analysis_ms >= 150000 or reply_ms >= 150000:
        raise ValueError('Measured service duration must fit the declared stage budget')
    if (not isinstance(arrivals_ms, list) or not arrivals_ms
            or any(type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in arrivals_ms)
            or arrivals_ms != sorted(arrivals_ms)):
        raise ValueError('Arrivals must be a nonempty, ordered finite nonnegative list')
    if type(poll_phase_ms) not in (int, float) or not 0 <= poll_phase_ms < 250:
        raise ValueError('Polling phase must be in [0,250) milliseconds')
    aggregation = Aggregation(quiet_ms, max_wait_ms, reference_ms, 'synthetic-arrival-benchmark')
    simulation = _Simulation(list(arrivals_ms), analysis_ms, reply_ms, aggregation)
    try:
        return simulation.run(poll_phase_ms)
    finally:
        simulation.store.db._memory.close()
