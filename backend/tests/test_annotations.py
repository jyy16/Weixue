"""Annotation-resolution guard for the whole backend.

Python 3.14 defers annotation evaluation (PEP 649), so a missing import used
inside a function signature only raises ``NameError`` on older interpreters --
"import main works on my machine, but fails on the clone". This test forces
annotation resolution for every project-defined function/class so that class
of bug is caught on any Python version.
"""

import inspect
import os
import sys
import typing


PROJECT_PREFIXES = (
    "api", "feishu", "grading", "database", "schemas", "asr",
    "audio_utils", "companion", "settings_store", "seed", "main",
)


def test_all_project_annotations_resolve():
    # Importing main also runs feishu.client's module-level load_dotenv, which
    # would leak real credentials into the parent pytest environment and break
    # the "unconfigured" tests that run after this file. Snapshot and restore.
    saved_env = dict(os.environ)
    try:
        import main  # noqa: F401 -- imports the whole app (lazy: run time)

        failures = []
        for mod_name, mod in list(sys.modules.items()):
            if not mod_name.startswith(PROJECT_PREFIXES):
                continue
            for name, obj in vars(mod).items():
                # Only objects defined in this module; imported library classes
                # (SQLAlchemy / pydantic) resolve inside their own namespace.
                if getattr(obj, "__module__", None) != mod_name:
                    continue
                if not (inspect.isfunction(obj) or inspect.isclass(obj)):
                    continue
                try:
                    typing.get_type_hints(obj)
                except Exception as exc:  # noqa: BLE001 -- surfaced for assert
                    failures.append(f"{mod_name}.{name}: {type(exc).__name__}: {exc}")
    finally:
        os.environ.clear()
        os.environ.update(saved_env)

    assert not failures, (
        "注解无法解析（通常是签名里用了未导入的名字，旧版 Python 下 import 会直接报错）：\n"
        + "\n".join(failures[:20])
    )
