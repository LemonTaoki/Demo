"""
Single-file Monero/XMRig controller for Windows CMD.

This program does not contain a hidden miner and never starts mining on launch.
Mining starts only after the local command "start" or an authorized Telegram
command "/start".

The actual RandomX work is performed by the official XMRig executable. Python
handles configuration, lifecycle, status, logs, Telegram, and optional payout
monitoring. This is intentional: a pure-Python RandomX loop would be many
orders of magnitude slower than a native miner and would not be production
quality.

Python standard library only.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


# ============================================================================
# CONFIGURATION - edit this section
# ============================================================================

COIN_NAME = "Monero"
ALGORITHM = "RandomX"

# HashVault public Monero endpoint. Port 443 is their TLS Stratum endpoint.
POOL_HOST = "pool.hashvault.pro"
POOL_PORT = 443

# Public receiving address only. Never put a seed phrase or private key here.
WALLET_ADDRESS = "835P6vhLc9WWDDxyZhGqCn6PNS7oYGrijFQ4i3haZqL1bkHPVyoScPuS5pauL5ep8G5tnc74i1g4r8mZzkhD6DWDGwi8UNF"
WORKER_NAME = "python-controller"
THREAD_COUNT = 1

# Set XMRIG_PATH to "xmrig.exe" if it is on PATH, or to a full Windows path.
XMRIG_PATH = "xmrig.exe"
XMRIG_API_HOST = "127.0.0.1"
XMRIG_API_PORT = 18080

# Telegram is optional. Prefer environment variables so the token is not
# stored in this source file:
#   set TELEGRAM_BOT_TOKEN=123456:replace_me
#   set TELEGRAM_CHAT_ID=123456789
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8883850768:AAGIuRKdFPE70y_JEixvAuWwKqt0Yg7RJYA")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "8816004505")

STATUS_INTERVAL = 5
TELEGRAM_STATUS_INTERVAL = 60
RECONNECT_MAX_DELAY = 60

# HashVault's documented wallet payment endpoint. {wallet} is replaced at
# runtime with WALLET_ADDRESS. Amounts returned by this endpoint are atomic
# units (1 XMR = 1,000,000,000,000 atomic units).
PAYOUT_API_URL = (
    "https://api.hashvault.pro/v3/monero/wallet/{wallet}/payments"
)
# CoinGecko's public simple-price response is:
# {"monero":{"usd":123.45}}. This is an estimated market value, not payout.
PRICE_API_URL = (
    "https://api.coingecko.com/api/v3/simple/price"
    "?ids=monero&vs_currencies=usd"
)
POOL_FEE_PERCENT = 0.9
DEFAULT_PAYOUT_THRESHOLD_XMR = 0.001
PAYOUT_POLL_INTERVAL = 300

LOG_FILE = "miner.log"
STATE_FILE = "miner_state.json"
PAYOUT_HISTORY_FILE = "payout_history.json"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def timestamp() -> str:
    return utc_now().strftime("%Y-%m-%d %H:%M:%S UTC")


def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    days, total = divmod(total, 86400)
    hours, total = divmod(total, 3600)
    minutes, secs = divmod(total, 60)
    if days:
        return f"{days}d {hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def format_hashrate(value: float) -> str:
    value = max(0.0, float(value or 0.0))
    units = ("H/s", "KH/s", "MH/s", "GH/s", "TH/s")
    index = 0
    while value >= 1000.0 and index < len(units) - 1:
        value /= 1000.0
        index += 1
    return f"{value:.2f} {units[index]}"


def redact(text: str) -> str:
    """Remove likely secret material from text before it reaches a log."""
    if not text:
        return text
    if TELEGRAM_BOT_TOKEN:
        text = text.replace(TELEGRAM_BOT_TOKEN, "<telegram-token-redacted>")
    return text


class Logger:
    def __init__(self, path: str = LOG_FILE) -> None:
        self._logger = logging.getLogger("python_miner")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        if not self._logger.handlers:
            formatter = logging.Formatter(
                "%(asctime)s | %(levelname)s | %(message)s",
                "%Y-%m-%d %H:%M:%S",
            )
            file_handler = logging.FileHandler(path, encoding="utf-8")
            file_handler.setFormatter(formatter)
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(formatter)
            self._logger.addHandler(file_handler)
            self._logger.addHandler(console_handler)

    def info(self, message: str) -> None:
        self._logger.info(redact(message))

    def warning(self, message: str) -> None:
        self._logger.warning(redact(message))

    def error(self, message: str) -> None:
        self._logger.error(redact(message))

    def exception(self, message: str) -> None:
        self._logger.exception(redact(message))


@dataclass
class MiningConfig:
    coin_name: str = COIN_NAME
    algorithm: str = ALGORITHM
    pool_host: str = POOL_HOST
    pool_port: int = POOL_PORT
    wallet_address: str = WALLET_ADDRESS
    worker_name: str = WORKER_NAME
    thread_count: int = THREAD_COUNT
    xmrig_path: str = XMRIG_PATH
    api_host: str = XMRIG_API_HOST
    api_port: int = XMRIG_API_PORT
    telegram_token: str = TELEGRAM_BOT_TOKEN
    telegram_chat_id: str = TELEGRAM_CHAT_ID
    payout_api_url: str = PAYOUT_API_URL
    price_api_url: str = PRICE_API_URL

    @property
    def pool_display(self) -> str:
        return f"{self.pool_host}:{self.pool_port}"

    @property
    def telegram_enabled(self) -> bool:
        return bool(
            self.telegram_token
            and self.telegram_chat_id
            and self.telegram_token != "YOUR_BOT_TOKEN"
            and self.telegram_chat_id != "YOUR_CHAT_ID"
        )

    def validate(self, require_wallet: bool = True) -> List[str]:
        errors: List[str] = []
        if not self.coin_name.strip():
            errors.append("Coin name is empty.")
        if not self.algorithm.strip():
            errors.append("Algorithm is empty.")
        if not self.pool_host.strip() or any(
            char.isspace() for char in self.pool_host
        ):
            errors.append("Pool host is invalid.")
        if not isinstance(self.pool_port, int) or not 1 <= self.pool_port <= 65535:
            errors.append("Pool port must be an integer from 1 to 65535.")
        if not 1 <= int(self.thread_count) <= 1024:
            errors.append("THREAD_COUNT must be between 1 and 1024.")
        if not 1 <= int(self.api_port) <= 65535:
            errors.append("XMRIG_API_PORT must be from 1 to 65535.")
        if require_wallet:
            address = self.wallet_address.strip()
            if address.startswith("YOUR_"):
                errors.append("Replace WALLET_ADDRESS with a public receiving address.")
            elif len(address) not in (95, 106) or not re.fullmatch(
                r"[1-9A-HJ-NP-Za-km-z]+", address
            ):
                errors.append("WALLET_ADDRESS does not look like a Monero address.")
        if self.telegram_token and self.telegram_token == "YOUR_BOT_TOKEN":
            errors.append("Replace TELEGRAM_BOT_TOKEN or leave it empty to disable Telegram.")
        if self.telegram_token and not self.telegram_chat_id:
            errors.append("TELEGRAM_CHAT_ID is required when Telegram is enabled.")
        return errors

    def summary(self) -> str:
        telegram = "enabled" if self.telegram_enabled else "disabled"
        return (
            "\n========================================\n"
            "PYTHON MINER CONTROLLER\n"
            "========================================\n"
            f"Coin        : {self.coin_name}\n"
            f"Algorithm   : {self.algorithm}\n"
            f"Pool        : {self.pool_display}\n"
            f"Worker      : {self.worker_name}\n"
            f"Threads     : {self.thread_count}\n"
            f"Telegram    : {telegram}\n"
            "Mining      : NOT STARTED\n"
            "========================================\n"
        )


class HashrateTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.started_at: Optional[float] = None
        self.total_hashes = 0
        self.current_hashrate = 0.0
        self.average_hashrate = 0.0
        self.last_update = 0.0

    def start(self) -> None:
        with self._lock:
            self.started_at = time.monotonic()
            self.total_hashes = 0
            self.current_hashrate = 0.0
            self.average_hashrate = 0.0
            self.last_update = time.monotonic()

    def stop(self) -> None:
        with self._lock:
            self.current_hashrate = 0.0

    def update(self, total_hashes: Optional[int] = None,
               current_hashrate: Optional[float] = None) -> None:
        with self._lock:
            if total_hashes is not None:
                self.total_hashes = max(0, int(total_hashes))
            if current_hashrate is not None:
                self.current_hashrate = max(0.0, float(current_hashrate))
            self.last_update = time.monotonic()
            if self.started_at:
                elapsed = max(0.001, time.monotonic() - self.started_at)
                self.average_hashrate = self.total_hashes / elapsed

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            runtime = (
                max(0.0, time.monotonic() - self.started_at)
                if self.started_at else 0.0
            )
            return {
                "total_hashes": self.total_hashes,
                "current_hashrate": self.current_hashrate,
                "average_hashrate": self.average_hashrate,
                "runtime": runtime,
            }


class ShareTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.accepted = 0
        self.rejected = 0
        self.stale = 0
        self.last_accepted_at: Optional[str] = None

    def set_counts(self, accepted: int, rejected: int) -> None:
        with self._lock:
            self.accepted = max(0, int(accepted))
            self.rejected = max(0, int(rejected))
            if self.accepted:
                self.last_accepted_at = timestamp()

    def record_line(self, line: str) -> None:
        lowered = line.lower()
        with self._lock:
            if "accepted" in lowered:
                self.accepted += 1
                self.last_accepted_at = timestamp()
            elif "rejected" in lowered:
                self.rejected += 1
            if "stale" in lowered:
                self.stale += 1

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "accepted": self.accepted,
                "rejected": self.rejected,
                "stale": self.stale,
                "last_accepted_at": self.last_accepted_at or "Never",
            }


class TelegramManager:
    def __init__(self, config: MiningConfig, logger: Logger) -> None:
        self.config = config
        self.logger = logger
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._offset = 0
        self._handler: Optional[Callable[[str], str]] = None

    def set_command_handler(self, handler: Callable[[str], str]) -> None:
        self._handler = handler

    def _api(self, method: str, payload: Optional[Dict[str, Any]] = None) -> Any:
        if not self.config.telegram_enabled:
            return None
        url = f"https://api.telegram.org/bot{self.config.telegram_token}/{method}"
        data = json.dumps(payload or {}).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                result = json.loads(response.read().decode("utf-8"))
            if not result.get("ok"):
                self.logger.warning(f"Telegram API rejected {method}.")
                return None
            return result.get("result")
        except Exception as exc:
            self.logger.warning(f"Telegram {method} failed: {type(exc).__name__}.")
            return None

    def send(self, message: str) -> bool:
        if not self.config.telegram_enabled:
            return False
        result = self._api(
            "sendMessage",
            {"chat_id": self.config.telegram_chat_id, "text": message},
        )
        return result is not None

    def start(self) -> None:
        if not self.config.telegram_enabled:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop, name="telegram-poller", daemon=True
        )
        self._thread.start()
        self.logger.info("Telegram command polling enabled.")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            updates = self._api(
                "getUpdates",
                {"timeout": 20, "offset": self._offset + 1, "allowed_updates": ["message"]},
            )
            if updates is None:
                self._stop_event.wait(10)
                continue
            for update in updates:
                self._offset = max(self._offset, int(update.get("update_id", 0)))
                message = update.get("message") or {}
                chat = message.get("chat") or {}
                chat_id = str(chat.get("id", ""))
                if chat_id != str(self.config.telegram_chat_id):
                    continue
                text = str(message.get("text", "")).strip()
                if not text or not self._handler:
                    continue
                try:
                    reply = self._handler(text)
                    if reply:
                        self.send(reply)
                except Exception:
                    self.logger.exception("Telegram command handler failed.")


class MiningWorker:
    def __init__(
        self,
        config: MiningConfig,
        logger: Logger,
        hashrate: HashrateTracker,
        shares: ShareTracker,
        on_line: Callable[[str], None],
        on_exit: Callable[[int], None],
    ) -> None:
        self.config = config
        self.logger = logger
        self.hashrate = hashrate
        self.shares = shares
        self.on_line = on_line
        self.on_exit = on_exit
        self.process: Optional[subprocess.Popen[str]] = None
        self._reader: Optional[threading.Thread] = None

    def command(self) -> List[str]:
        return [
            self.config.xmrig_path,
            "--no-color",
            "--coin", "monero",
            "--algo", "rx/0",
            "--url", self.config.pool_display,
            "--user", self.config.wallet_address,
            "--pass", self.config.worker_name,
            "--threads", str(self.config.thread_count),
            "--tls",
            "--http-enabled",
            "--http-host", self.config.api_host,
            "--http-port", str(self.config.api_port),
        ]

    def start(self) -> bool:
        try:
            self.process = subprocess.Popen(
                self.command(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except FileNotFoundError:
            self.logger.error(
                f"XMRig executable not found: {self.config.xmrig_path}. "
                "Install XMRig or set XMRIG_PATH."
            )
            return False
        except Exception as exc:
            self.logger.error(f"Could not start XMRig: {type(exc).__name__}.")
            return False

        self._reader = threading.Thread(
            target=self._read_output, name="xmrig-output", daemon=True
        )
        self._reader.start()
        self.logger.info("XMRig process started.")
        return True

    def _read_output(self) -> None:
        if not self.process or not self.process.stdout:
            return
        try:
            for line in self.process.stdout:
                cleaned = line.strip()
                if cleaned:
                    self.on_line(cleaned)
        except Exception:
            self.logger.exception("XMRig output reader failed.")
        finally:
            if self.process:
                self.on_exit(int(self.process.poll() or 0))

    def stop(self) -> None:
        process = self.process
        if not process:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        self.process = None

    def is_running(self) -> bool:
        return bool(self.process and self.process.poll() is None)


class PoolManager:
    def __init__(
        self,
        config: MiningConfig,
        logger: Logger,
        hashrate: HashrateTracker,
        shares: ShareTracker,
        on_connection_error: Callable[[str], None],
    ) -> None:
        self.config = config
        self.logger = logger
        self.hashrate = hashrate
        self.shares = shares
        self.on_connection_error = on_connection_error
        self.worker: Optional[MiningWorker] = None
        self.connected = False
        self._last_api_update = 0.0
        self._api_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self) -> bool:
        self._stop_event.clear()
        self.worker = MiningWorker(
            self.config,
            self.logger,
            self.hashrate,
            self.shares,
            self._handle_line,
            self._handle_exit,
        )
        if not self.worker.start():
            self.connected = False
            return False
        self.connected = True
        self._api_thread = threading.Thread(
            target=self._api_loop, name="xmrig-api", daemon=True
        )
        self._api_thread.start()
        self.logger.info(f"Pool process connected to {self.config.pool_display}.")
        return True

    def _handle_line(self, line: str) -> None:
        lowered = line.lower()
        if "accepted" in lowered or "rejected" in lowered or "stale" in lowered:
            self.shares.record_line(line)
        if "connection error" in lowered or "connection refused" in lowered:
            self.connected = False
            self.on_connection_error(line[:300])
        if any(
            marker in lowered
            for marker in (
                "accepted",
                "rejected",
                "stale",
                "connection error",
                "connection refused",
                "error",
            )
        ):
            self.logger.info(f"XMRig: {line}")

    def _handle_exit(self, code: int) -> None:
        self.connected = False
        if not self._stop_event.is_set():
            self.on_connection_error(f"XMRig exited with code {code}.")

    def _api_loop(self) -> None:
        while not self._stop_event.is_set():
            self._poll_summary()
            self._stop_event.wait(STATUS_INTERVAL)

    def _poll_summary(self) -> None:
        url = f"http://{self.config.api_host}:{self.config.api_port}/2/summary"
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                data = json.loads(response.read().decode("utf-8"))
            results = data.get("results") or {}
            hashrate_data = data.get("hashrate") or {}
            total_hashes = results.get("hashes_total")
            total = hashrate_data.get("total")
            current = 0.0
            if isinstance(total, list):
                current = float(total[0] or 0.0) if total else 0.0
            elif isinstance(total, (int, float)):
                current = float(total)
            elif isinstance(total, dict):
                current = float(total.get("hashrate", 0.0) or 0.0)
            if total_hashes is not None or current:
                self.hashrate.update(total_hashes, current)
            if "shares_good" in results or "shares_total" in results:
                accepted = int(results.get("shares_good", 0) or 0)
                total_shares = int(results.get("shares_total", accepted) or accepted)
                self.shares.set_counts(accepted, max(0, total_shares - accepted))
            self.connected = True
            self._last_api_update = time.monotonic()
        except Exception:
            # XMRig's API can be disabled or unavailable during startup.
            # Output logging and process supervision still continue.
            pass

    def stop(self) -> None:
        self._stop_event.set()
        if self.worker:
            self.worker.stop()
        self.worker = None
        self.connected = False

    def is_running(self) -> bool:
        return bool(self.worker and self.worker.is_running())


class PayoutMonitor:
    def __init__(
        self,
        config: MiningConfig,
        logger: Logger,
        telegram: TelegramManager,
    ) -> None:
        self.config = config
        self.logger = logger
        self.telegram = telegram
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.history: List[Dict[str, Any]] = []
        self.last_payout = "Unavailable"
        self._initialized = False
        self.load_state()

    def load_state(self) -> None:
        for path, fallback in ((PAYOUT_HISTORY_FILE, []),):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    value = json.load(handle)
                if isinstance(value, list):
                    self.history = value
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                self.history = fallback
        if self.history:
            self.last_payout = str(self.history[-1].get("timestamp", "Unavailable"))
            self._initialized = True

    def _save(self) -> None:
        temporary = f"{PAYOUT_HISTORY_FILE}.tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(self.history[-100:], handle, indent=2)
        os.replace(temporary, PAYOUT_HISTORY_FILE)

    def start(self) -> None:
        if (
            not self.config.payout_api_url
            or self.config.wallet_address.startswith("YOUR_")
        ):
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, name="payout-monitor", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            self.poll_once()
            self._stop_event.wait(PAYOUT_POLL_INTERVAL)

    def poll_once(self) -> None:
        if (
            not self.config.payout_api_url
            or self.config.wallet_address.startswith("YOUR_")
        ):
            return
        try:
            wallet = urllib.parse.quote(self.config.wallet_address, safe="")
            url = self.config.payout_api_url.format(wallet=wallet)
            with urllib.request.urlopen(url, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
            payouts = payload if isinstance(payload, list) else payload.get("payouts", [])
            if not isinstance(payouts, list):
                raise ValueError("HashVault payments response must be a list")
            new_records: List[Dict[str, Any]] = []
            for payout in payouts:
                if not isinstance(payout, dict):
                    continue
                txid = str(payout.get("txnHash", payout.get("txid", ""))).strip()
                if not txid or any(item.get("txid") == txid for item in self.history):
                    continue
                # HashVault reports XMR amounts in atomic units.
                raw_amount = float(payout.get("amount", 0.0))
                amount = raw_amount / 1_000_000_000_000
                if amount <= 0:
                    continue
                estimated = self._price_value(amount)
                raw_ts = payout.get("ts")
                if raw_ts:
                    payout_time = datetime.fromtimestamp(
                        float(raw_ts), timezone.utc
                    ).strftime("%Y-%m-%d %H:%M:%S UTC")
                else:
                    payout_time = timestamp()
                record = {
                    "txid": txid,
                    "coin": self.config.coin_name,
                    "amount": amount,
                    "estimated_usdt_value": estimated,
                    "pool": self.config.pool_display,
                    "timestamp": payout_time,
                    "confirmation_status": "confirmed",
                    "explorer": f"https://xmrchain.net/tx/{txid}",
                }
                new_records.append(record)
            if not self._initialized:
                # The first successful poll imports existing confirmed history
                # without pretending those old payments just happened.
                with self._lock:
                    self.history.extend(reversed(new_records))
                    self.history.sort(key=lambda item: item.get("timestamp", ""))
                    if self.history:
                        self.last_payout = self.history[-1]["timestamp"]
                    self._save()
                self._initialized = True
                return
            for record in reversed(new_records):
                with self._lock:
                    self.history.append(record)
                    self.last_payout = record["timestamp"]
                    self._save()
                self.logger.info(f"Confirmed payout detected: {record['txid']}")
                self.telegram.send(self._notification(record))
        except Exception as exc:
            self.logger.warning(f"Payout API unavailable: {type(exc).__name__}.")

    def _price_value(self, amount: float) -> Optional[float]:
        if not self.config.price_api_url:
            return None
        try:
            with urllib.request.urlopen(self.config.price_api_url, timeout=10) as response:
                data = json.loads(response.read().decode("utf-8"))
            if "price_usdt" in data:
                price = float(data["price_usdt"])
            else:
                price = float(data["monero"]["usd"])
            return round(amount * price, 8)
        except Exception:
            self.logger.warning("Price API unavailable; USDT value not estimated.")
            return None

    def _notification(self, record: Dict[str, Any]) -> str:
        value = (
            f"${record['estimated_usdt_value']:.8f}"
            if record["estimated_usdt_value"] is not None
            else "unavailable"
        )
        return (
            "💰 PAYOUT RECEIVED\n\n"
            "Status: SUCCESS\n"
            f"Coin: {record['coin']}\n"
            f"Amount: {record['amount']:.8f} {record['coin']}\n"
            f"Estimated USDT value: {value}\n"
            f"Pool: {record['pool']}\n"
            f"Transaction ID: {record['txid']}\n"
            f"Timestamp: {record['timestamp']}\n"
            f"Explorer: {record['explorer']}"
        )

    def recent(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self.history[-10:])

    def summary(self) -> Tuple[float, Optional[float]]:
        with self._lock:
            total_coin = sum(float(item.get("amount", 0.0)) for item in self.history)
            values = [
                float(item["estimated_usdt_value"])
                for item in self.history
                if item.get("estimated_usdt_value") is not None
            ]
            return total_coin, (sum(values) if values else None)


class MiningManager:
    def __init__(
        self,
        config: MiningConfig,
        logger: Logger,
        telegram: TelegramManager,
        payout_monitor: PayoutMonitor,
    ) -> None:
        self.config = config
        self.logger = logger
        self.telegram = telegram
        self.payout_monitor = payout_monitor
        self.hashrate = HashrateTracker()
        self.shares = ShareTracker()
        self.pool = PoolManager(
            config,
            logger,
            self.hashrate,
            self.shares,
            self._connection_error,
        )
        self._lock = threading.Lock()
        self._supervisor: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self.mining = False
        self.started_at: Optional[float] = None
        self.last_connection_error = ""
        self._last_telegram_status = 0.0

    def start(self) -> str:
        with self._lock:
            if self.mining:
                return "Mining is already running."
            errors = self.config.validate(require_wallet=True)
            if errors:
                return "Cannot start:\n- " + "\n- ".join(errors)
            self._stop_event.clear()
            self.hashrate.start()
            self.started_at = time.monotonic()
            self.mining = True
            if not self.pool.start():
                self.mining = False
                self.hashrate.stop()
                return "XMRig could not be started. Check XMRIG_PATH and the log."
            self._supervisor = threading.Thread(
                target=self._supervise, name="miner-supervisor", daemon=True
            )
            self._supervisor.start()
        self.logger.info("Mining started by explicit user command.")
        self.telegram.send(self._started_message())
        return "Mining started."

    def stop(self) -> str:
        with self._lock:
            if not self.mining:
                return "Mining is not running."
            self._stop_event.set()
            self.pool.stop()
            self.mining = False
            self.hashrate.stop()
        self.logger.info("Mining stopped by user command.")
        self.telegram.send(self._stopped_message())
        return "Mining stopped."

    def restart(self) -> str:
        self.stop()
        time.sleep(1)
        return self.start()

    def _supervise(self) -> None:
        delay = 3
        while not self._stop_event.is_set():
            if self.pool.is_running():
                delay = 3
                self._maybe_telegram_status()
                self._stop_event.wait(2)
                continue
            if self._stop_event.is_set():
                break
            self.logger.warning(f"Reconnecting to pool in {delay} seconds.")
            self.telegram.send(
                f"🔄 POOL RECONNECTING\n\nPool: {self.config.pool_display}\n"
                f"Attempt delay: {delay}s"
            )
            if self._stop_event.wait(delay):
                break
            self.pool.stop()
            if self.pool.start():
                self.telegram.send(
                    f"🟢 POOL CONNECTED\n\nPool: {self.config.pool_display}\n"
                    f"Worker: {self.config.worker_name}"
                )
                delay = 3
            else:
                delay = min(RECONNECT_MAX_DELAY, delay * 2)

    def _maybe_telegram_status(self) -> None:
        if not self.config.telegram_enabled:
            return
        now = time.monotonic()
        if now - self._last_telegram_status >= TELEGRAM_STATUS_INTERVAL:
            self._last_telegram_status = now
            self.telegram.send(self._hashrate_message())

    def _connection_error(self, error: str) -> None:
        self.last_connection_error = error
        self.logger.warning(f"Pool connection error: {error}")
        self.telegram.send(
            f"⚠️ POOL CONNECTION ERROR\n\nPool: {self.config.pool_display}\n"
            f"Error: {error}"
        )

    def snapshot(self) -> Dict[str, Any]:
        data = {}
        data.update(self.hashrate.snapshot())
        data.update(self.shares.snapshot())
        data["mining"] = self.mining
        data["connected"] = self.pool.connected
        data["threads"] = self.config.thread_count
        data["worker"] = self.config.worker_name
        data["runtime"] = (
            max(0.0, time.monotonic() - self.started_at)
            if self.started_at and self.mining else data["runtime"]
        )
        return data

    def _started_message(self) -> str:
        return (
            "⛏️ MINING STARTED\n\n"
            f"Coin: {self.config.coin_name}\n"
            f"Algorithm: {self.config.algorithm}\n"
            f"Pool: {self.config.pool_display}\n"
            f"Worker: {self.config.worker_name}\n"
            f"Threads: {self.config.thread_count}\n"
            f"Time: {timestamp()}"
        )

    def _stopped_message(self) -> str:
        state = self.snapshot()
        return (
            "🛑 MINING STOPPED\n\n"
            f"Runtime: {format_duration(state['runtime'])}\n"
            f"Total Hashes: {state['total_hashes']}\n"
            f"Accepted: {state['accepted']}\n"
            f"Rejected: {state['rejected']}"
        )

    def _hashrate_message(self) -> str:
        state = self.snapshot()
        return (
            "📊 HASHRATE UPDATE\n\n"
            f"Current: {format_hashrate(state['current_hashrate'])}\n"
            f"Average: {format_hashrate(state['average_hashrate'])}\n"
            f"Accepted: {state['accepted']}\n"
            f"Rejected: {state['rejected']}"
        )

    def telegram_command(self, command: str) -> str:
        command = command.lower().split()[0] if command.strip() else ""
        if command == "/start":
            return self.start()
        if command == "/stop":
            return self.stop()
        if command == "/restart":
            return self.restart()
        if command in ("/status", "/hashrate", "/workers"):
            return self.status_text()
        return (
            "Commands: /start /stop /status /hashrate /workers /restart"
        )

    def status_text(self) -> str:
        state = self.snapshot()
        return (
            f"Coin: {self.config.coin_name}\n"
            f"Algorithm: {self.config.algorithm}\n"
            f"Pool: {self.config.pool_display}\n"
            f"Worker: {state['worker']}\n"
            f"Connection: {'CONNECTED' if state['connected'] else 'DISCONNECTED'}\n"
            f"Mining: {'MINING' if state['mining'] else 'STOPPED'}\n"
            f"Threads: {state['threads']}\n"
            f"Current Hashrate: {format_hashrate(state['current_hashrate'])}\n"
            f"Average Hashrate: {format_hashrate(state['average_hashrate'])}\n"
            f"Total Hashes: {state['total_hashes']}\n"
            f"Accepted: {state['accepted']}\n"
            f"Rejected: {state['rejected']}\n"
            f"Stale: {state['stale']}\n"
            f"Runtime: {format_duration(state['runtime'])}\n"
            f"Last Accepted: {state['last_accepted_at']}\n"
            f"Last Payout: {self.payout_monitor.last_payout}\n"
        )


class TerminalUI:
    def __init__(
        self,
        config: MiningConfig,
        manager: MiningManager,
        telegram: TelegramManager,
        payout_monitor: PayoutMonitor,
        logger: Logger,
    ) -> None:
        self.config = config
        self.manager = manager
        self.telegram = telegram
        self.payout_monitor = payout_monitor
        self.logger = logger
        self._stop_event = threading.Event()
        self._screen_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._screen_thread = threading.Thread(
            target=self._screen_loop, name="terminal-status", daemon=True
        )
        self._screen_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._screen_thread and self._screen_thread.is_alive():
            self._screen_thread.join(timeout=2)

    def _screen_loop(self) -> None:
        while not self._stop_event.is_set():
            if self.manager.mining:
                self._render()
            self._stop_event.wait(STATUS_INTERVAL)

    def _render(self) -> None:
        state = self.manager.snapshot()
        try:
            os.system("cls" if os.name == "nt" else "clear")
        except Exception:
            pass
        print(
            "========================================\n"
            "PYTHON MINER\n"
            "========================================"
        )
        print(f"Coin        : {self.config.coin_name}")
        print(f"Algorithm   : {self.config.algorithm}")
        print(f"Pool        : {self.config.pool_display}")
        print(f"Worker      : {state['worker']}")
        print(f"Status      : {'MINING' if state['mining'] else 'STOPPED'}")
        print(f"Connection  : {'CONNECTED' if state['connected'] else 'DISCONNECTED'}")
        print(f"Hashrate    : {format_hashrate(state['current_hashrate'])}")
        print(f"Avg Hashrate: {format_hashrate(state['average_hashrate'])}")
        print(f"Total Hashes: {state['total_hashes']}")
        print(f"Accepted    : {state['accepted']}")
        print(f"Rejected    : {state['rejected']}")
        print(f"Stale       : {state['stale']}")
        print(f"Runtime     : {format_duration(state['runtime'])}")
        print(f"Last Share  : {state['last_accepted_at']}")
        print(f"Last Payout : {self.payout_monitor.last_payout}")
        print("\nType a command: status | stop | help | exit")

    def payouts(self) -> str:
        entries = self.payout_monitor.recent()
        if not entries:
            return "Payout information unavailable. No confirmed payouts recorded."
        lines = ["Recent confirmed payouts:"]
        for item in entries:
            value = (
                f"${item['estimated_usdt_value']:.8f}"
                if item.get("estimated_usdt_value") is not None
                else "unavailable"
            )
            lines.append(
                f"- {item['timestamp']} | {item['amount']:.8f} {item['coin']} | "
                f"USDT estimate: {value} | tx: {item['txid']}"
            )
        return "\n".join(lines)

    def help_text(self) -> str:
        return (
            "Commands:\n"
            "  start     Start XMRig mining\n"
            "  stop      Stop mining\n"
            "  status    Show complete status\n"
            "  hashrate  Show current and average hashrate\n"
            "  workers   Show worker status\n"
            "  restart   Restart the mining process\n"
            "  payouts   Show confirmed payout history\n"
            "  help      Show this help\n"
            "  exit      Stop everything and exit\n"
            "\nMining never starts automatically."
        )

    def run(self) -> None:
        print(self.config.summary())
        errors = self.config.validate(require_wallet=False)
        if errors:
            print("Configuration warnings:")
            for error in errors:
                print(f"- {error}")
        print(self.help_text())
        try:
            while True:
                command = input("\nminer> ").strip().lower()
                if command == "start":
                    print(self.manager.start())
                elif command == "stop":
                    print(self.manager.stop())
                elif command == "status":
                    print(self.manager.status_text())
                elif command in ("hashrate", "workers"):
                    print(self.manager.status_text())
                elif command == "restart":
                    print(self.manager.restart())
                elif command == "payouts":
                    print(self.payouts())
                elif command == "help":
                    print(self.help_text())
                elif command in ("exit", "quit"):
                    break
                elif command:
                    print("Unknown command. Type help.")
        except (KeyboardInterrupt, EOFError):
            print("\nShutdown requested.")
        finally:
            self.manager.stop()
            self.payout_monitor.stop()
            self.telegram.stop()
            self.stop()
            self.logger.info("Application exited.")


def main() -> None:
    config = MiningConfig()
    logger = Logger()
    telegram = TelegramManager(config, logger)
    payout_monitor = PayoutMonitor(config, logger, telegram)
    manager = MiningManager(config, logger, telegram, payout_monitor)
    telegram.set_command_handler(manager.telegram_command)
    telegram.start()
    payout_monitor.start()
    ui = TerminalUI(config, manager, telegram, payout_monitor, logger)
    ui.start()
    logger.info("Application ready; waiting for explicit start command.")
    ui.run()


if __name__ == "__main__":
    main()
