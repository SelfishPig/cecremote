"""Small REST API for controlling HDMI-CEC through a persistent cec-client."""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("cec-api")


@dataclass(frozen=True)
class QueuedCommand:
    text: str
    description: str


class CecWorker:
    """Own a cec-client process and feed queued commands to its stdin."""

    def __init__(self) -> None:
        self._commands: queue.Queue[QueuedCommand | None] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen[str] | None = None
        self._executable = os.getenv("CEC_CLIENT", "cec-client")
        self._device_type = os.getenv("CEC_DEVICE_TYPE", "p")
        self._restart_delay = float(os.getenv("CEC_RESTART_DELAY", "2"))

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="cec-client-worker", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._commands.put(None)
        if self._thread:
            self._thread.join(timeout=5)
        self._terminate_process()

    def enqueue(self, command: QueuedCommand) -> int:
        if not self._thread or not self._thread.is_alive():
            raise RuntimeError("CEC worker is not running")
        self._commands.put(command)
        return self._commands.qsize()

    def _start_process(self) -> subprocess.Popen[str]:
        logger.info("Starting %s", self._executable)
        return subprocess.Popen(
            [self._executable, "-d", "8", "-t", self._device_type],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

    def _ensure_process(self) -> subprocess.Popen[str] | None:
        if self._process and self._process.poll() is None:
            return self._process

        self._terminate_process()
        try:
            self._process = self._start_process()
        except (OSError, ValueError) as exc:
            logger.error("Unable to start cec-client: %s", exc)
            return None
        return self._process

    def _send(self, command: QueuedCommand) -> bool:
        process = self._ensure_process()
        if process is None or process.stdin is None:
            return False

        try:
            logger.info("Sending CEC command: %s", command.description)
            process.stdin.write(command.text + "\n")
            process.stdin.flush()
            return True
        except (BrokenPipeError, OSError) as exc:
            logger.warning("cec-client stopped while sending command: %s", exc)
            self._terminate_process()
            return False

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                command = self._commands.get(timeout=1)
            except queue.Empty:
                if self._process and self._process.poll() is not None:
                    logger.warning("cec-client exited; restarting it")
                    self._terminate_process()
                if self._process is None:
                    self._ensure_process()
                continue

            if command is None:
                self._commands.task_done()
                break

            while not self._stop.is_set() and not self._send(command):
                self._stop.wait(self._restart_delay)
            self._commands.task_done()

        self._terminate_process()

    def _terminate_process(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        if process.stdin:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)


class SourceRequest(BaseModel):
    physical_address: str = Field(
        examples=["2.0.0.0"], description="CEC physical address of the HDMI source"
    )

    @field_validator("physical_address")
    @classmethod
    def validate_physical_address(cls, value: str) -> str:
        parts = value.split(".")
        if len(parts) != 4:
            raise ValueError("must contain four dot-separated hexadecimal digits")
        try:
            digits = [int(part, 16) for part in parts]
        except ValueError as exc:
            raise ValueError("each component must be hexadecimal") from exc
        if any(len(part) != 1 or digit > 15 for part, digit in zip(parts, digits)):
            raise ValueError("each component must be one hexadecimal digit (0-F)")
        return ".".join(part.upper() for part in parts)

    def as_bytes(self) -> str:
        digits = self.physical_address.split(".")
        return f"{digits[0]}{digits[1]}:{digits[2]}{digits[3]}"


class QueueResponse(BaseModel):
    status: str = "queued"
    command: str
    queue_depth: int


class SourceOption(BaseModel):
    physical_address: str
    name: str


worker = CecWorker()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    worker.start()
    try:
        yield
    finally:
        worker.stop()


app = FastAPI(title="CEC Remote API", version="1.0.0", lifespan=lifespan)
STATIC_PATH = Path(__file__).parent / "static"
INDEX_PATH = STATIC_PATH / "index.html"
SOURCES_PATH = Path(
    os.getenv("CEC_SOURCES_FILE", Path(__file__).parent / "sources.json")
)
app.mount("/static", StaticFiles(directory=STATIC_PATH), name="static")


def queue_command(text: str, description: str) -> QueueResponse:
    try:
        depth = worker.enqueue(QueuedCommand(text=text, description=description))
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    return QueueResponse(command=description, queue_depth=depth)


@app.get("/", include_in_schema=False, response_class=FileResponse)
def index() -> FileResponse:
    return FileResponse(INDEX_PATH)


@app.get("/service-worker.js", include_in_schema=False, response_class=FileResponse)
def service_worker() -> FileResponse:
    return FileResponse(
        STATIC_PATH / "service-worker.js", media_type="application/javascript"
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/sources", response_model=list[SourceOption])
def sources() -> list[SourceOption]:
    try:
        configured_sources = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("Unable to read source configuration %s: %s", SOURCES_PATH, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Source configuration could not be loaded",
        ) from exc

    if not isinstance(configured_sources, dict):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Source configuration must be an address-to-name object",
        )

    result: list[SourceOption] = []
    try:
        for address, name in configured_sources.items():
            source = SourceRequest(physical_address=address)
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"source {address} must have a non-empty name")
            result.append(
                SourceOption(physical_address=source.physical_address, name=name.strip())
            )
    except (TypeError, ValueError) as exc:
        logger.error("Invalid source configuration %s: %s", SOURCES_PATH, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Source configuration is invalid",
        ) from exc
    return result


@app.post("/power/on", response_model=QueueResponse, status_code=202)
def power_on() -> QueueResponse:
    return queue_command("tx 10:44:6D\ntx 10:45", "power on")


@app.post("/power/off", response_model=QueueResponse, status_code=202)
def power_off() -> QueueResponse:
    return queue_command("tx 10:44:6C\ntx 10:45", "power off")


@app.post("/source", response_model=QueueResponse, status_code=202)
def select_source(source: SourceRequest) -> QueueResponse:
    command = f"tx 1f:82:{source.as_bytes()}"
    source_name = next(
        (
            option.name
            for option in sources()
            if option.physical_address == source.physical_address
        ),
        source.physical_address,
    )
    return queue_command(command, f"select source {source_name}")
