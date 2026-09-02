
import os
import subprocess
import threading
import time
from pathlib import Path

import psutil
import requests


class EmbeddingManager:
    def __init__(self, root, load_config, save_config):
        self.root = Path(root)
        self.load_config = load_config
        self.save_config = save_config
        self.lock = threading.RLock()
        self.last_error = ""
        self.auto_start_attempted = False

    def _cfg(self):
        return self.load_config().get("embedding_server", {})

    def _port_pid(self, port):
        try:
            for conn in psutil.net_connections(kind="tcp"):
                if (
                    conn.status == psutil.CONN_LISTEN
                    and conn.laddr
                    and int(conn.laddr.port) == int(port)
                ):
                    return conn.pid
        except Exception:
            pass
        return None

    def _health(self):
        c = self._cfg()
        host = c.get("host", "127.0.0.1")
        port = int(c.get("port", 8081))
        try:
            r = requests.get(f"http://{host}:{port}/health", timeout=0.7)
            if r.status_code == 200:
                return "ready", "HTTP 200"
            if r.status_code == 503:
                return "loading", "HTTP 503"
            return "error", f"HTTP {r.status_code}"
        except Exception as e:
            return "stopped", str(e)

    def status(self):
        c = self._cfg()
        port = int(c.get("port", 8081))
        pid = self._port_pid(port)
        state, detail = self._health()
        with self.lock:
            return {
                "pid": pid,
                "state": state,
                "detail": detail,
                "running": pid is not None,
                "last_error": self.last_error,
                "auto_start": bool(c.get("auto_start", True)),
                "model_path": c.get("model_path", ""),
                "threads": int(c.get("threads", 4)),
                "port": port,
            }

    def _build_args(self):
        c = self._cfg()
        exe = Path(c.get("llama_server_path", r"C:\llama.cpp\llama-server.exe"))
        model = Path(c.get(
            "model_path",
            r"C:\Models\Qwen3-Embedding-0.6B\Qwen3-Embedding-0.6B-Q8_0.gguf"
        ))

        if not exe.exists():
            raise FileNotFoundError(f"llama-server not found: {exe}")
        if not model.exists():
            raise FileNotFoundError(f"Embedding model not found: {model}")

        return [
            str(exe),
            "-m", str(model),
            "--embedding",
            "--pooling", str(c.get("pooling", "last")),
            "-c", str(int(c.get("context", 32768))),
            "-ub", str(int(c.get("ubatch", 2048))),
            "-t", str(int(c.get("threads", 4))),
            "-a", str(c.get("alias", "novel-embed")),
            "--host", str(c.get("host", "127.0.0.1")),
            "--port", str(int(c.get("port", 8081))),
        ]

    def start(self):
        c = self._cfg()
        port = int(c.get("port", 8081))

        pid = self._port_pid(port)
        if pid:
            return {
                "ok": True,
                "message": "Embedding server is already running.",
                **self.status(),
            }

        args = self._build_args()

        logs = self.root / "logs"
        runtime = self.root / "runtime"
        logs.mkdir(parents=True, exist_ok=True)
        runtime.mkdir(parents=True, exist_ok=True)

        out_f = open(logs / "embed_stdout.log", "a", encoding="utf-8", buffering=1)
        err_f = open(logs / "embed_stderr.log", "a", encoding="utf-8", buffering=1)

        flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW on Windows
        proc = subprocess.Popen(
            args,
            stdout=out_f,
            stderr=err_f,
            cwd=str(Path(args[0]).parent),
            creationflags=flags,
        )

        (runtime / "embed.pid").write_text(str(proc.pid), encoding="ascii")

        with self.lock:
            self.last_error = ""

        return {
            "ok": True,
            "message": "Embedding process started. It may show HTTP 503 while loading.",
            "pid": proc.pid,
        }

    def _terminate_pid(self, pid, force=False):
        if not pid:
            return
        try:
            p = psutil.Process(pid)
            if force:
                p.kill()
                p.wait(timeout=10)
            else:
                p.terminate()
                try:
                    p.wait(timeout=12)
                except psutil.TimeoutExpired:
                    p.kill()
                    p.wait(timeout=10)
        except psutil.NoSuchProcess:
            pass

    def stop(self, force=False):
        c = self._cfg()
        pid = self._port_pid(int(c.get("port", 8081)))
        self._terminate_pid(pid, force=force)
        try:
            (self.root / "runtime" / "embed.pid").unlink(missing_ok=True)
        except Exception:
            pass
        return {"ok": True, "forced": bool(force)}

    def restart(self):
        self.stop(force=False)
        time.sleep(0.5)
        return self.start()

    def ensure_started_async(self):
        """
        Called once when NovelAgent Web starts.
        It performs one startup check only. It is NOT a watchdog:
        if the user manually stops the embedding server, it stays stopped.
        """
        c = self._cfg()
        if not bool(c.get("auto_start", True)):
            return

        with self.lock:
            if self.auto_start_attempted:
                return
            self.auto_start_attempted = True

        def worker():
            try:
                state, _ = self._health()
                if state in ("ready", "loading"):
                    return
                if self._port_pid(int(c.get("port", 8081))):
                    return
                self.start()
            except Exception as e:
                with self.lock:
                    self.last_error = repr(e)

        threading.Thread(target=worker, daemon=True).start()
