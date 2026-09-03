"""The Temporal WORKER - hosts the workflow + activities and polls the task queue.

Run this in its own terminal (after `temporal server start-dev`). It connects to the
local Temporal server and waits for work. The Eno client (eno.py) starts workflows.
"""
import asyncio
import concurrent.futures

from temporalio.client import Client
from temporalio.worker import Worker

import os

import activities
from workflows import (BoardPollWorkflow, OttoWorkflow, PrReviewPollWorkflow, ReaperWorkflow,
                       SlackPollWorkflow)

TASK_QUEUE = os.environ.get("OTTO_TASK_QUEUE", "otto")

# Every @activity.defn in activities.py MUST be listed here, or the workflow will
# call an unregistered activity and Temporal retries it forever (silent hang).
# test_core.test_worker_registers_every_activity guards against drift.
ACTIVITIES = [
    activities.route_request, activities.clarify_request,
    activities.classify_request, activities.classify_followup,
    activities.plan_capability, activities.suggest_repo,
    activities.plan_swarm, activities.merge_results,
    activities.plan_task_steps, activities.execute_plan,
    activities.provision_workspace, activities.finalize_workspace,
    activities.cleanup_workspace, activities.recover_pr_branch,
    activities.pr_head_branch, activities.resolve_pr_target, activities.check_grounding,
    activities.snapshot_repos, activities.detect_repo_changes,
    activities.run_capability, activities.verify_capability,
    activities.qa_capability, activities.judge_qa,
    activities.review_capability, activities.judge_review,
    activities.record_attempt, activities.record_skip,
    activities.deliver_result, activities.open_chat, activities.record_chat,
    activities.poll_board, activities.poll_pr_reviews, activities.poll_slack,
    activities.finalize_terminal,
    activities.reap_stuck, activities.notify_human,
    activities.snapshot_settings, activities.estop_check,
]


async def main():
    client = await Client.connect("localhost:7233")
    # Sync activities (subprocess + file IO) run in a thread pool.
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        worker = Worker(
            client,
            task_queue=TASK_QUEUE,
            workflows=[OttoWorkflow, BoardPollWorkflow, PrReviewPollWorkflow, ReaperWorkflow,
                       SlackPollWorkflow],
            activities=ACTIVITIES,
            activity_executor=executor,
        )
        print(f"[worker] running on task queue '{TASK_QUEUE}'  (Ctrl-C to stop)")
        await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
