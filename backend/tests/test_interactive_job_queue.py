"""Neoh's chat replies run on their own queue and worker pool.

Measured (Mission 8): on the shared queue with 2 workers, 10 concurrent
conversations never had more than 2 model calls in flight and 42 of ~100 turns
timed out at 90 s; any long periodic job held a slot for its whole run.
"""

import inspect

import ai_chat_api
import automation_jobs


def test_chat_turns_are_enqueued_on_the_interactive_queue():
    src = inspect.getsource(ai_chat_api)
    call = src[src.index('job_type="ai_chat:response"'):]
    call = call[:call.index(")\n")]
    assert "queue_name=INTERACTIVE_QUEUE" in call


def test_the_worker_pool_serves_both_queues_separately():
    w = automation_jobs.DurableJobWorkers(worker_count=3, interactive_count=5)
    assert len(w.worker_ids) == 3 and len(w.interactive_ids) == 5
    assert set(w.worker_ids).isdisjoint(w.interactive_ids)
    start = inspect.getsource(automation_jobs.DurableJobWorkers.start)
    assert "INTERACTIVE_QUEUE" in start


def test_the_interactive_pool_bounds_model_concurrency():
    # It is the global ceiling on concurrent chat model calls; it must be a
    # finite, modest number, not "one per request".
    assert 1 <= automation_jobs._INTERACTIVE_WORKER_COUNT <= 32


def test_claims_are_scoped_to_one_queue():
    src = inspect.getsource(automation_jobs.claim_next_job)
    assert "WHERE queue_name = $1" in src and "FOR UPDATE SKIP LOCKED" in src
