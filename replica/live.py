import asyncio
import importlib
from pathlib import Path

# store.py stays out: the open Store instance keeps the class it was created with.
MODULES = (
    "replica.catalog",
    "replica.quota",
    "replica.access",
    "replica.descriptions",
    "replica.chat",
    "replica.screens",
    "replica.machine",
    "replica.billing.entitlements",
    "replica.billing_ui",
    "replica.payments",
    "replica.admin",
    "replica.telegram",
    "replica.live",
)


def source_paths() -> tuple[Path, ...]:
    paths: list[Path] = []
    for name in MODULES:
        module = importlib.import_module(name)
        file = getattr(module, "__file__", None)
        if file is not None:
            paths.append(Path(file).resolve())
    return tuple(paths)


def reload_code() -> None:
    for name in MODULES:
        importlib.reload(importlib.import_module(name))


async def watch_code() -> None:
    seen = {path: path.stat().st_mtime_ns for path in source_paths()}
    while True:
        await asyncio.sleep(0.4)
        changed: list[str] = []
        for path in source_paths():
            try:
                mtime = path.stat().st_mtime_ns
            except OSError:
                continue
            if seen.get(path) != mtime:
                seen[path] = mtime
                changed.append(path.name)
        if not changed:
            continue
        try:
            reload_code()
        except Exception as exc:
            print(f"Не смог подхватить изменения: {exc}", flush=True)
            continue
        print("Подхватил изменения: " + ", ".join(changed), flush=True)
