# If you want to define a custom DAG, create
# a new file under orchestrate/dags/ and Airflow
# will pick it up automatically.

import os
import logging
from airflow import DAG
from airflow.operators.bash_operator import BashOperator
from datetime import datetime, time, timedelta, MINYEAR

from meltano.core.schedule_service import ScheduleService
from meltano.core.project import Project
from meltano.core.utils import coerce_datetime
from meltano.core.job import JobFinder
from meltano.core.db import project_engine


project = Project.find()
schedule_service = ScheduleService(project)

default_args = {
    "owner": "airflow",
    "depends_on_past": False,
    "email_on_failure": False,
    "email_on_retry": False,
    "catchup": False,
    "retries": 1,
    "retry_delay": timedelta(minutes=5),
    "concurrency": 1,
}

engine_uri = os.getenv(
    "MELTANO_DATABASE_URI", "sqlite:///$MELTANO_PROJECT_ROOT/.meltano/meltano.db"
)
engine_uri = engine_uri.replace("$MELTANO_PROJECT_ROOT", str(project.root))

engine, Session = project_engine(project, engine_uri, default=True)
session = Session()

for schedule in schedule_service.schedules():
    if schedule.interval == "@once":
        logging.info(
            f"No DAG created for schedule '{schedule.name}' because its interval is set to `@once`."
        )
        continue

    finder = JobFinder(schedule.name)
    last_successful_run = finder.latest_success(session)
    if not last_successful_run:
        logging.info(
            f"No DAG created for schedule '{schedule.name}' because it hasn't had a successful (manual) run yet."
        )
        continue

    args = default_args.copy()
    if schedule.start_date:
        args["start_date"] = coerce_datetime(schedule.start_date)

    dag_id = f"meltano_{schedule.name}"

    # from https://airflow.apache.org/docs/stable/scheduler.html#backfill-and-catchup
    #
    # It is crucial to set `catchup` to False so that Airflow only create a single job
    # at the tail end of date window we want to extract data.
    #
    # Because our extractors do not support date-window extraction, it serves no
    # purpose to enqueue date-chunked jobs for complete extraction window.
    dag = DAG(
        dag_id, catchup=False, default_args=args, schedule_interval=schedule.interval
    )

    elt = BashOperator(
        task_id="extract_load",
        bash_command=f"echo $PATH; echo $VIRTUAL_ENV; cd {str(project.root)}; .meltano/run/bin elt {schedule.extractor} {schedule.loader} --job_id={schedule.name} --transform={schedule.transform}",
        dag=dag,
        env={
            # inherit the current env
            **os.environ,
            **schedule.env,
        },
    )

    # register the dag
    globals()[dag_id] = dag
