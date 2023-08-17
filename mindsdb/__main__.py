from mindsdb.utilities import log

logger = log.getLogger("mindsdb")
logger.debug("Starting MindsDB...")

import asyncio
import atexit
import json
import os
import secrets
import signal
import sys
import threading
from textwrap import dedent
import time
import traceback

import psutil
import torch.multiprocessing as mp
from packaging import version

import mindsdb.interfaces.storage.db as db
from mindsdb.__about__ import __version__ as mindsdb_version
from mindsdb.api.http.start import start as start_http
from mindsdb.api.mongo.start import start as start_mongo
from mindsdb.api.mysql.start import start as start_mysql
from mindsdb.api.postgres.start import start as start_postgres
from mindsdb.interfaces.tasks.task_monitor import start as start_tasks
from mindsdb.interfaces.jobs.scheduler import start as start_scheduler
from mindsdb.utilities.config import Config
from mindsdb.utilities.ps import is_pid_listen_port, get_child_pids
from mindsdb.utilities.functions import args_parse, get_versions_where_predictors_become_obsolete
from mindsdb.utilities import log
from mindsdb.interfaces.database.integrations import integration_controller
import mindsdb.interfaces.storage.db as db
from mindsdb.integrations.utilities.install import install_dependencies
from mindsdb.interfaces.chatbot.chatbot_monitor import start as start_chatbot
from mindsdb.interfaces.database.integrations import integration_controller
from mindsdb.interfaces.jobs.scheduler import start as start_scheduler
from mindsdb.utilities.auth import get_aws_meta_data, register_oauth_client
from mindsdb.utilities.config import Config
from mindsdb.utilities.context import context as ctx
from mindsdb.utilities.fs import (
    clean_process_marks,
    clean_unlinked_process_marks,
    create_dirs_recursive,
)
from mindsdb.utilities.functions import (
    args_parse,
    get_versions_where_predictors_become_obsolete,
)
from mindsdb.utilities.ps import get_child_pids, is_pid_listen_port
from mindsdb.utilities.telemetry import disable_telemetry, telemetry_file_exists

try:
    mp.set_start_method("spawn")
except RuntimeError:
    logger.info("Torch multiprocessing context already set, ignoring...")


_stop_event = threading.Event()


def close_api_gracefully(apis):
    _stop_event.set()
    try:
        for api in apis.values():
            process = api["process"]
            childs = get_child_pids(process.pid)
            for p in childs:
                try:
                    os.kill(p, signal.SIGTERM)
                except Exception:
                    p.kill()
            sys.stdout.flush()
            process.terminate()
            process.join()
            sys.stdout.flush()
    except KeyboardInterrupt:
        sys.exit(0)
    except psutil.NoSuchProcess:
        pass


def do_clean_process_marks():
    while _stop_event.wait(timeout=5) is False:
        clean_unlinked_process_marks()


if __name__ == "__main__":
    # ----------------  __init__.py section ------------------
    clean_process_marks()
    ctx.set_default()
    args = args_parse()

    # ---- CHECK SYSTEM ----
    if not (sys.version_info[0] >= 3 and sys.version_info[1] >= 6):
        print(
            """
     MindsDB server requires Python >= 3.7 to run

     Once you have Python 3.7 installed you can tun mindsdb as follows:

     1. create and activate venv:
     python3.7 -m venv venv
     source venv/bin/activate

     2. install MindsDB:
     pip3 install mindsdb

     3. Run MindsDB
     python3.7 -m mindsdb

     More instructions in https://docs.mindsdb.com
         """
        )
        exit(1)

    # --- VERSION MODE ----
    if args is not None and args.version:
        print(f"MindsDB {mindsdb_version}")
        sys.exit(0)

    # --- MODULE OR LIBRARY IMPORT MODE ----
    if args is not None and args.config is not None:
        config_path = args.config
        with open(config_path, "r") as fp:
            user_config = json.load(fp)
    else:
        user_config = {}
        config_path = "absent"
    os.environ["MINDSDB_CONFIG_PATH"] = config_path
    mindsdb_config = Config()

    create_dirs_recursive(mindsdb_config["paths"])

    if telemetry_file_exists(mindsdb_config["storage_dir"]):
        os.environ["CHECK_FOR_UPDATES"] = "0"
        logger.info("x telemetry disabled")
    elif os.getenv("CHECK_FOR_UPDATES", "1").lower() in [
        "0",
        "false",
        "False",
    ] or mindsdb_config.get("cloud", False):
        disable_telemetry(mindsdb_config["storage_dir"])
        logger.info("x telemetry disabled")
    else:
        logger.info("✓ telemetry enabled")

    if os.environ.get("FLASK_SECRET_KEY") is None:
        os.environ["FLASK_SECRET_KEY"] = secrets.token_hex(32)

    # -------------------------------------------------------

    # initialization
    db.init()

    mp.freeze_support()
    config = Config()

    environment = config.get("environment")
    if environment == "aws_marketplace":
        try:
            register_oauth_client()
        except Exception as e:
            logger.error(f"Something went wrong during client register: {e}")
    elif environment != "local":
        try:
            aws_meta_data = get_aws_meta_data()
            Config().update({"aws_meta_data": aws_meta_data})
        except Exception:
            pass

    is_cloud = config.get("cloud", False)
    # need configure migration behavior by env_variables
    # leave 'is_cloud' for now, but needs to be removed further
    run_migration_separately = os.environ.get("SEPARATE_MIGRATIONS", False)
    if run_migration_separately in (False, "false", "False", 0, "0", ""):
        run_migration_separately = False
        logger.info("Will run migrations here..")
    else:
        run_migration_separately = True
        logger.info("Migrations will be run separately..")

    if not is_cloud and not run_migration_separately:
        logger.info("Applying database migrations:")
        try:
            from mindsdb.migrations import migrate

            migrate.migrate_to_head()
        except Exception as e:
            logger.error(f"Error! Something went wrong during DB migrations: {e}")

    if args.verbose is True:
        # Figure this one out later
        pass

    if args.install_handlers is not None:
        handlers_list = [s.strip() for s in args.install_handlers.split(",")]
        # import_meta = handler_meta.get('import', {})
        for (
            handler_name,
            handler_meta,
        ) in integration_controller.get_handlers_import_status().items():
            if handler_name not in handlers_list:
                continue
            import_meta = handler_meta.get("import", {})
            if import_meta.get("success") is True:
                logger.info(f"{'{0: <18}'.format(handler_name)} - already installed")
                continue
            result = install_dependencies(import_meta.get("dependencies", []))
            if result.get("success") is True:
                logger.info(
                    f"{'{0: <18}'.format(handler_name)} - successfully installed"
                )
            else:
                logger.info(
                    f"{'{0: <18}'.format(handler_name)} - error during dependencies installation: {result.get('error_message', 'unknown error')}"
                )
        sys.exit(0)

    logger.info(f"Version: {mindsdb_version}")
    logger.info(f"Configuration file: {config.config_path}")
    logger.info(f"Storage path: {config['paths']['root']}")
    logger.debug(f"User config: {user_config}")


<<<<<<< HEAD
    for handler_name, handler_meta in integration_controller.get_handlers_import_status().items():
        import_meta = handler_meta.get('import', {})
        if import_meta.get('success', False) is not True:
            print(dedent('''
                Some handlers cannot be imported. You can check list of available handlers by execute command in sql editor:
                    select * from information_schema.handlers;
            '''))
            break
=======
    # @TODO Backwards compatibility for tests, remove later
    for (
        handler_name,
        handler_meta,
    ) in integration_controller.get_handlers_import_status().items():
        import_meta = handler_meta.get("import", {})
        dependencies = import_meta.get("dependencies")
        if import_meta.get("success", False) is not True:
            logger.info(
                f"Dependencies for the handler '{handler_name}' are not installed by default."
            )
            logger.info(
                f'If you want to use "{handler_name}" please install "{dependencies}"'
            )

    # from mindsdb.utilities.fs import get_marked_processes_and_threads
    # marks = get_marked_processes_and_threads()
>>>>>>> 64c9b3257 (WIP logging)

    if not is_cloud:
        # region creating permanent integrations
        for (
            integration_name,
            handler,
        ) in integration_controller.get_handlers_import_status().items():
            if handler.get("permanent"):
                integration_meta = integration_controller.get(name=integration_name)
                if integration_meta is None:
                    integration_record = db.Integration(
                        name=integration_name,
                        data={},
                        engine=integration_name,
                        company_id=None,
                    )
                    db.session.add(integration_record)
                    db.session.commit()
        # endregion

        # region Mark old predictors as outdated
        is_modified = False
        predictor_records = (
            db.session.query(db.Predictor)
            .filter(db.Predictor.deleted_at.is_(None))
            .all()
        )
        if len(predictor_records) > 0:
            (
                sucess,
                compatible_versions,
            ) = get_versions_where_predictors_become_obsolete()
            if sucess is True:
                compatible_versions = [version.parse(x) for x in compatible_versions]
                mindsdb_version_parsed = version.parse(mindsdb_version)
                compatible_versions = [
                    x for x in compatible_versions if x <= mindsdb_version_parsed
                ]
                if len(compatible_versions) > 0:
                    last_compatible_version = compatible_versions[-1]
                    for predictor_record in predictor_records:
                        if (
                            isinstance(predictor_record.mindsdb_version, str)
                            and version.parse(predictor_record.mindsdb_version)
                            < last_compatible_version
                        ):
                            predictor_record.update_status = "available"
                            is_modified = True
        if is_modified is True:
            db.session.commit()
        # endregion

<<<<<<< HEAD
=======
        for integration_name in config.get("integrations", {}):
            try:
                it = integration_controller.get(integration_name)
                if it is not None:
                    integration_controller.delete(integration_name)
                logger.info(f"Adding: {integration_name}")
                integration_data = config["integrations"][integration_name]
                engine = integration_data.get("type")
                if engine is not None:
                    del integration_data["type"]
                integration_controller.add(integration_name, engine, integration_data)
            except Exception as e:
                logger.error(
                    f"\n\nError: {e} adding database integration {integration_name}\n\n"
                )

    # @TODO Backwards compatibility for tests, remove later

>>>>>>> 64c9b3257 (WIP logging)
    if args.api is None:
        api_arr = ["http", "mysql"]
    else:
        api_arr = args.api.split(",")

    with_nlp = False
    if "nlp" in api_arr:
        with_nlp = True
        api_arr.remove("nlp")

    apis = {
        api: {"port": config["api"][api]["port"], "process": None, "started": False}
        for api in api_arr
    }

    start_functions = {
<<<<<<< HEAD
        'http': start_http,
        'mysql': start_mysql,
        'mongodb': start_mongo,
        'postgres': start_postgres,
        'jobs': start_scheduler,
        'tasks': start_tasks
=======
        "http": start_http,
        "mysql": start_mysql,
        "mongodb": start_mongo,
        "postgres": start_postgres,
        "jobs": start_scheduler,
        "chatbot": start_chatbot,
>>>>>>> 64c9b3257 (WIP logging)
    }

    if config.get("jobs", {}).get("disable") is not True:
        apis["jobs"] = {"process": None, "started": False}

    # disabled on cloud
<<<<<<< HEAD
    if config.get('tasks', {}).get('disable') is not True:
        apis['tasks'] = {
            'process': None,
            'started': False
        }
=======
    if config.get("chatbot", {}).get("disable") is not True and not is_cloud:
        apis["chatbot"] = {"process": None, "started": False}
>>>>>>> 64c9b3257 (WIP logging)

    # TODO this 'ctx' is eclipsing 'context' class imported as 'ctx'
    ctx = mp.get_context("spawn")
    for api_name, api_data in apis.items():
        if api_data["started"]:
            continue
        logger.info(f"{api_name} API: starting...")
        try:
            if api_name == "http":
                p = ctx.Process(
                    target=start_functions[api_name],
                    args=(args.verbose, args.no_studio, with_nlp),
                    name=api_name + "-Proc",
                )
            else:
                p = ctx.Process(
                    target=start_functions[api_name],
                    args=(args.verbose,),
                    name=api_name + "-Proc",
                )
            p.start()
            api_data["process"] = p
        except Exception as e:
            logger.error(
                f"Failed to start {api_name} API with exception {e}\n{traceback.format_exc()}"
            )
            close_api_gracefully(apis)
            raise e

    atexit.register(close_api_gracefully, apis=apis)

    async def wait_api_start(api_name, pid, port):
        timeout = 60
        start_time = time.time()
        started = is_pid_listen_port(pid, port)
        while (time.time() - start_time) < timeout and started is False:
            await asyncio.sleep(0.5)
            started = is_pid_listen_port(pid, port)
        return api_name, port, started

    async def wait_apis_start():
        futures = [
            wait_api_start(api_name, api_data["process"].pid, api_data["port"])
            for api_name, api_data in apis.items()
            if "port" in api_data
        ]
        for i, future in enumerate(asyncio.as_completed(futures)):
            api_name, port, started = await future
            if started:
                logger.info(f"{api_name} API: started on {port}")
            else:
                logger.error(f"ERROR: {api_name} API cant start on {port}")

    async def join_process(process, name):
        try:
            process.join()
        except KeyboardInterrupt:
            logger.info("Got keyboard interrupt, stopping APIs")
            close_api_gracefully(apis)
        finally:
            logger.info(f"{name} API: stopped")

    async def gather_apis():
        await asyncio.gather(
            *[
                join_process(api_data["process"], api_name)
                for api_name, api_data in apis.items()
            ],
            return_exceptions=False,
        )

    ioloop = asyncio.new_event_loop()
    ioloop.run_until_complete(wait_apis_start())

    threading.Thread(target=do_clean_process_marks).start()

    ioloop.run_until_complete(gather_apis())
    ioloop.close()
