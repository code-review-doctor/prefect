import fastapi
from typing import List
from datetime import timedelta

import pendulum
import pydantic
import pytest

from prefect.orion import models
from prefect.orion.schemas import core, states, responses
from prefect.orion.utilities.database import get_session_factory
from prefect.orion.schemas.states import StateType

dt = pendulum.datetime(2021, 7, 1)


def parse_response(response: fastapi.Response, include=None):
    assert response.status_code == 200
    parsed = pydantic.parse_obj_as(List[responses.HistoryResponse], response.json())

    # for each interval...
    for p in parsed:
        # sort states arrays for comparison
        p.states = sorted(p.states, key=lambda s: s.state_name)
        # grab only requested fields in the states aggregation, to make comparison simple
        if include:
            p.states = [dict(**s.dict(include=set(include))) for s in p.states]

    return parsed


@pytest.fixture(autouse=True, scope="module")
async def clear_db():
    """Prevent automatic database-clearing behavior after every test"""
    pass


@pytest.fixture(autouse=True, scope="module")
async def data(database_engine):

    session_factory = await get_session_factory(bind=database_engine)
    async with session_factory() as session:

        create_flow = lambda flow: models.flows.create_flow(session=session, flow=flow)
        create_flow_run = lambda flow_run: models.flow_runs.create_flow_run(
            session=session, flow_run=flow_run
        )
        create_task_run = lambda task_run: models.task_runs.create_task_run(
            session=session, task_run=task_run
        )

        f_1 = await create_flow(flow=core.Flow(name="f-1", tags=["db", "blue"]))
        f_2 = await create_flow(flow=core.Flow(name="f-2", tags=["db"]))

        # have a completed flow every 12 hours except weekends
        for d in pendulum.period(dt.subtract(days=14), dt).range("hours", 12):

            # skip weekends
            if d.day_of_week in (0, 6):
                continue

            await create_flow_run(
                flow_run=core.FlowRun(
                    flow_id=f_1.id,
                    tags=["completed"],
                    state=states.Completed(timestamp=d),
                )
            )

        # have a failed flow every 36 hours except the last 3 days
        for d in pendulum.period(dt.subtract(days=14), dt).range("hours", 36):

            # skip recent runs
            if dt.subtract(days=3) <= d < dt:
                continue

            await create_flow_run(
                flow_run=core.FlowRun(
                    flow_id=f_1.id,
                    tags=["failed"],
                    state=states.Failed(timestamp=d),
                )
            )

        # a few running runs in the last two days
        for d in pendulum.period(dt.subtract(days=2), dt).range("hours", 6):
            await create_flow_run(
                flow_run=core.FlowRun(
                    flow_id=f_1.id,
                    tags=["running"],
                    state=states.Running(timestamp=d),
                )
            )

        # schedule new runs
        for d in pendulum.period(dt.subtract(days=1), dt.add(days=3)).range("hours", 6):
            await create_flow_run(
                flow_run=core.FlowRun(
                    flow_id=f_1.id,
                    tags=["scheduled"],
                    state=states.Scheduled(scheduled_time=d),
                )
            )

        # -------------- task runs
        fr = await create_flow_run(
            flow_run=core.FlowRun(
                flow_id=f_1.id,
                tags=["running"],
                state=states.Running(timestamp=dt),
            )
        )

        for r in range(10):
            await create_task_run(
                core.TaskRun(
                    flow_run_id=fr.id,
                    task_key=str(r),
                    state=states.Completed(timestamp=dt.add(minutes=r)),
                )
            )
            await create_task_run(
                core.TaskRun(
                    flow_run_id=fr.id,
                    task_key=str(r),
                    state=states.Failed(timestamp=dt.add(minutes=7 + r)),
                )
            )
            await create_task_run(
                core.TaskRun(
                    flow_run_id=fr.id,
                    task_key=str(r),
                    state=states.Running(timestamp=dt.add(minutes=14 + r)),
                )
            )

        await session.commit()


@pytest.mark.parametrize("route", ["/flow_runs/history/", "/task_runs/history/"])
@pytest.mark.parametrize(
    "start,end,interval,expected_bins",
    [
        (dt, dt.add(days=14), timedelta(days=1), 14),
        (dt, dt.add(days=10), timedelta(days=1, hours=1), 10),
        (dt, dt.add(days=10), timedelta(hours=6), 40),
        (dt, dt.add(days=10), timedelta(hours=1), 240),
        (dt, dt.add(days=1), timedelta(hours=1, minutes=6), 22),
        (dt, dt.add(hours=5), timedelta(minutes=1), 300),
        (dt, dt.add(days=1, hours=5), timedelta(minutes=15), 116),
    ],
)
async def test_history(
    client,
    route,
    start,
    end,
    interval,
    expected_bins,
):
    response = await client.post(
        route,
        json=dict(
            history_start=str(start),
            history_end=str(end),
            history_interval_seconds=interval.total_seconds(),
        ),
    )

    parsed = parse_response(response)
    assert len(parsed) == expected_bins
    assert min([r.interval_start for r in parsed]) == start
    assert parsed[0].interval_end - parsed[0].interval_start == interval
    assert (
        max([r.interval_start for r in parsed])
        == start + (expected_bins - 1) * interval
    )


@pytest.mark.parametrize("route", ["flow_runs", "task_runs"])
async def test_history_returns_maximum_items(client, route):
    response = await client.post(
        f"/{route}/history",
        json=dict(
            history_start=str(dt),
            history_end=str(dt.add(days=10)),
            history_interval_seconds=timedelta(minutes=1).total_seconds(),
        ),
    )

    assert response.status_code == 200

    # only first 500 items returned
    assert len(response.json()) == 500
    assert min([r["interval_start"] for r in response.json()]) == str(dt)
    assert max([r["interval_start"] for r in response.json()]) == str(
        dt.add(minutes=499)
    )


async def test_daily_bins_flow_runs(client):
    response = await client.post(
        "/flow_runs/history/",
        json=dict(
            history_start=str(dt.subtract(days=5)),
            history_end=str(dt.add(days=1)),
            history_interval_seconds=timedelta(days=1).total_seconds(),
        ),
    )

    parsed = parse_response(
        response, include={"state_name", "state_type", "count_runs"}
    )

    assert parsed == [
        dict(
            interval_start=dt.subtract(days=5),
            interval_end=dt.subtract(days=4),
            states=[
                dict(state_name="Failed", state_type=StateType.FAILED, count_runs=1)
            ],
        ),
        dict(
            interval_start=dt.subtract(days=4),
            interval_end=dt.subtract(days=3),
            states=[
                dict(state_name="Failed", state_type=StateType.FAILED, count_runs=1),
            ],
        ),
        dict(
            interval_start=dt.subtract(days=3),
            interval_end=dt.subtract(days=2),
            states=[
                dict(
                    state_name="Completed", state_type=StateType.COMPLETED, count_runs=2
                )
            ],
        ),
        dict(
            interval_start=dt.subtract(days=2),
            interval_end=dt.subtract(days=1),
            states=[
                dict(
                    state_name="Completed", state_type=StateType.COMPLETED, count_runs=2
                ),
                dict(state_name="Running", state_type=StateType.RUNNING, count_runs=4),
            ],
        ),
        dict(
            interval_start=dt.subtract(days=1),
            interval_end=dt,
            states=[
                dict(
                    state_name="Completed", state_type=StateType.COMPLETED, count_runs=2
                ),
                dict(state_name="Running", state_type=StateType.RUNNING, count_runs=4),
                dict(
                    state_name="Scheduled", state_type=StateType.SCHEDULED, count_runs=4
                ),
            ],
        ),
        dict(
            interval_start=dt,
            interval_end=dt.add(days=1),
            states=[
                dict(
                    state_name="Completed", state_type=StateType.COMPLETED, count_runs=1
                ),
                dict(state_name="Running", state_type=StateType.RUNNING, count_runs=2),
                dict(
                    state_name="Scheduled", state_type=StateType.SCHEDULED, count_runs=4
                ),
            ],
        ),
    ]


async def test_weekly_bins_flow_runs(client):
    response = await client.post(
        "/flow_runs/history/",
        json=dict(
            history_start=str(dt.subtract(days=16)),
            history_end=str(dt.add(days=6)),
            history_interval_seconds=timedelta(days=7).total_seconds(),
        ),
    )

    parsed = parse_response(
        response, include={"state_type", "state_name", "count_runs"}
    )

    assert parsed == [
        dict(
            interval_start=dt.subtract(days=16),
            interval_end=dt.subtract(days=9),
            states=[
                dict(
                    state_name="Completed", state_type=StateType.COMPLETED, count_runs=6
                ),
                dict(state_name="Failed", state_type=StateType.FAILED, count_runs=4),
            ],
        ),
        dict(
            interval_start=dt.subtract(days=9),
            interval_end=dt.subtract(days=2),
            states=[
                dict(
                    state_name="Completed",
                    state_type=StateType.COMPLETED,
                    count_runs=10,
                ),
                dict(state_name="Failed", state_type=StateType.FAILED, count_runs=4),
            ],
        ),
        dict(
            interval_start=dt.subtract(days=2),
            interval_end=dt.add(days=5),
            states=[
                dict(
                    state_name="Completed", state_type=StateType.COMPLETED, count_runs=5
                ),
                dict(state_name="Running", state_type=StateType.RUNNING, count_runs=10),
                dict(
                    state_name="Scheduled",
                    state_type=StateType.SCHEDULED,
                    count_runs=17,
                ),
            ],
        ),
        dict(
            interval_start=dt.add(days=5),
            interval_end=dt.add(days=12),
            states=[],
        ),
    ]


async def test_weekly_bins_with_filters_flow_runs(client):
    response = await client.post(
        "/flow_runs/history/",
        json=dict(
            history_start=str(dt.subtract(days=16)),
            history_end=str(dt.add(days=6)),
            history_interval_seconds=timedelta(days=7).total_seconds(),
            flow_runs=dict(state_type=dict(any_=["FAILED", "SCHEDULED"])),
        ),
    )

    parsed = parse_response(
        response, include={"state_type", "state_name", "count_runs"}
    )

    assert parsed == [
        dict(
            interval_start=dt.subtract(days=16),
            interval_end=dt.subtract(days=9),
            states=[
                dict(state_name="Failed", state_type=StateType.FAILED, count_runs=4),
            ],
        ),
        dict(
            interval_start=dt.subtract(days=9),
            interval_end=dt.subtract(days=2),
            states=[
                dict(state_name="Failed", state_type=StateType.FAILED, count_runs=4),
            ],
        ),
        dict(
            interval_start=dt.subtract(days=2),
            interval_end=dt.add(days=5),
            states=[
                dict(
                    state_name="Scheduled",
                    state_type=StateType.SCHEDULED,
                    count_runs=17,
                ),
            ],
        ),
        dict(
            interval_start=dt.add(days=5),
            interval_end=dt.add(days=12),
            states=[],
        ),
    ]


async def test_5_minute_bins_task_runs(client):
    response = await client.post(
        "/task_runs/history/",
        json=dict(
            history_start=str(dt.subtract(minutes=5)),
            history_end=str(dt.add(minutes=15)),
            history_interval_seconds=timedelta(minutes=5).total_seconds(),
        ),
    )

    parsed = parse_response(
        response, include={"state_type", "state_name", "count_runs"}
    )

    assert parsed == [
        dict(
            interval_start=pendulum.datetime(2021, 6, 30, 23, 55),
            interval_end=pendulum.datetime(2021, 7, 1, 0, 0),
            states=[],
        ),
        dict(
            interval_start=pendulum.datetime(2021, 7, 1, 0, 0),
            interval_end=pendulum.datetime(2021, 7, 1, 0, 5),
            states=[
                dict(
                    state_name="Completed", state_type=StateType.COMPLETED, count_runs=5
                )
            ],
        ),
        dict(
            interval_start=pendulum.datetime(2021, 7, 1, 0, 5),
            interval_end=pendulum.datetime(2021, 7, 1, 0, 10),
            states=[
                dict(
                    state_name="Completed", state_type=StateType.COMPLETED, count_runs=5
                ),
                dict(state_name="Failed", state_type=StateType.FAILED, count_runs=3),
            ],
        ),
        dict(
            interval_start=pendulum.datetime(2021, 7, 1, 0, 10),
            interval_end=pendulum.datetime(2021, 7, 1, 0, 15),
            states=[
                dict(state_name="Failed", state_type=StateType.FAILED, count_runs=5),
                dict(state_name="Running", state_type=StateType.RUNNING, count_runs=1),
            ],
        ),
    ]


async def test_5_minute_bins_task_runs_with_filter(client):
    response = await client.post(
        "/task_runs/history/",
        json=dict(
            history_start=str(dt.subtract(minutes=5)),
            history_end=str(dt.add(minutes=15)),
            history_interval_seconds=timedelta(minutes=5).total_seconds(),
            task_runs=dict(state_type=dict(any_=["COMPLETED", "RUNNING"])),
        ),
    )

    parsed = parse_response(
        response, include={"state_type", "state_name", "count_runs"}
    )

    assert parsed == [
        dict(
            interval_start=pendulum.datetime(2021, 6, 30, 23, 55),
            interval_end=pendulum.datetime(2021, 7, 1, 0, 0),
            states=[],
        ),
        dict(
            interval_start=pendulum.datetime(2021, 7, 1, 0, 0),
            interval_end=pendulum.datetime(2021, 7, 1, 0, 5),
            states=[
                dict(
                    state_name="Completed", state_type=StateType.COMPLETED, count_runs=5
                )
            ],
        ),
        dict(
            interval_start=pendulum.datetime(2021, 7, 1, 0, 5),
            interval_end=pendulum.datetime(2021, 7, 1, 0, 10),
            states=[
                dict(
                    state_name="Completed", state_type=StateType.COMPLETED, count_runs=5
                ),
            ],
        ),
        dict(
            interval_start=pendulum.datetime(2021, 7, 1, 0, 10),
            interval_end=pendulum.datetime(2021, 7, 1, 0, 15),
            states=[
                dict(state_name="Running", state_type=StateType.RUNNING, count_runs=1),
            ],
        ),
    ]


@pytest.mark.parametrize("route", ["flow_runs", "task_runs"])
async def test_last_bin_contains_end_date(client, route):
    """The last bin contains the end date, so its own end could be after the history end"""
    response = await client.post(
        f"/{route}/history",
        json=dict(
            history_start=str(dt),
            history_end=str(dt.add(days=1, minutes=30)),
            history_interval_seconds=timedelta(days=1).total_seconds(),
        ),
    )

    assert response.status_code == 200
    parsed = pydantic.parse_obj_as(List[responses.HistoryResponse], response.json())
    assert len(parsed) == 2
    assert parsed[0].interval_start == dt
    assert parsed[0].interval_end == dt.add(days=1)
    assert parsed[1].interval_start == dt.add(days=1)
    assert parsed[1].interval_end == dt.add(days=2)


async def test_flow_run_lateness(client, session):

    await session.execute("delete from flow where true;")

    f = await models.flows.create_flow(session=session, flow=core.Flow(name="lateness"))

    # started 3 seconds late
    fr = await models.flow_runs.create_flow_run(
        session=session,
        flow_run=core.FlowRun(
            flow_id=f.id, state=states.Pending(timestamp=dt.subtract(minutes=40))
        ),
    )
    await models.flow_runs.set_flow_run_state(
        session=session,
        flow_run_id=fr.id,
        state=states.Running(timestamp=dt.subtract(minutes=39, seconds=57)),
    )
    await models.flow_runs.set_flow_run_state(
        session=session,
        flow_run_id=fr.id,
        state=states.Completed(timestamp=dt),
        force=True,
    )

    # started 10 minutes late, still running
    fr2 = await models.flow_runs.create_flow_run(
        session=session,
        flow_run=core.FlowRun(
            flow_id=f.id,
            state=states.Scheduled(
                scheduled_time=dt.subtract(minutes=15),
            ),
        ),
    )
    await models.flow_runs.set_flow_run_state(
        session=session,
        flow_run_id=fr2.id,
        state=states.Pending(timestamp=dt.subtract(minutes=6)),
        force=True,
    )
    await models.flow_runs.set_flow_run_state(
        session=session,
        flow_run_id=fr2.id,
        state=states.Running(timestamp=dt.subtract(minutes=5)),
        force=True,
    )

    # never started
    fr3 = await models.flow_runs.create_flow_run(
        session=session,
        flow_run=core.FlowRun(
            flow_id=f.id,
            state=states.Scheduled(scheduled_time=dt.subtract(minutes=1)),
        ),
    )
    fr4 = await models.flow_runs.create_flow_run(
        session=session,
        flow_run=core.FlowRun(
            flow_id=f.id,
            state=states.Scheduled(scheduled_time=dt.subtract(seconds=25)),
        ),
    )

    await session.commit()

    response = await client.post(
        "/flow_runs/history",
        json=dict(
            history_start=str(dt.subtract(days=1)),
            history_end=str(dt.add(days=1)),
            history_interval_seconds=timedelta(days=2).total_seconds(),
            flows=dict(id=dict(any_=[str(f.id)])),
        ),
    )
    parsed = parse_response(response)
    interval = parsed[0]

    assert interval.interval_start == dt.subtract(days=1)
    assert interval.interval_end == dt.add(days=1)

    # -------------------------------- COMPLETED

    assert interval.states[0].state_type == StateType.COMPLETED
    assert interval.states[0].count_runs == 1
    assert interval.states[0].sum_estimated_run_time == timedelta(
        minutes=39, seconds=57
    )
    assert interval.states[0].sum_estimated_lateness == timedelta(seconds=3)

    # -------------------------------- RUNNING

    assert interval.states[1].state_type == StateType.RUNNING
    assert interval.states[1].count_runs == 1

    expected_lateness = pendulum.now("UTC") - dt.subtract(minutes=5)
    assert (
        expected_lateness - timedelta(seconds=2)
        < interval.states[1].sum_estimated_run_time
        < expected_lateness
    )
    assert interval.states[1].sum_estimated_lateness == timedelta(seconds=600)

    # -------------------------------- SCHEDULED

    assert interval.states[2].state_type == StateType.SCHEDULED
    assert interval.states[2].count_runs == 2
    assert interval.states[2].sum_estimated_run_time == timedelta(0)

    expected_lateness = (pendulum.now("UTC") - dt.subtract(minutes=1)) + (
        pendulum.now("UTC") - dt.subtract(seconds=25)
    )
    assert (
        expected_lateness - timedelta(seconds=2)
        < interval.states[2].sum_estimated_lateness
        < expected_lateness
    )