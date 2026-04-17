import asyncio
import json
import logging
import threading
import ssl
import sys
import os
import queue
import certifi

import requests
import websockets

import customtkinter as ctk

# ------------- Config -------------
SERVER_URL = "wss://esatici.az/ws/v1/terminal"
LOG_FILE = os.path.expanduser("~/esatici_agent.log")
CONFIG_FILE = os.path.expanduser("~/.esatici_config.json")

# File + console logging so we can debug crashes
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger(__name__)

# ------------- State -------------
class AgentState:
    def __init__(self):
        self.token = ""
        self.is_connected = False
        self.should_run = False
        self.loop: asyncio.AbstractEventLoop | None = None
        
        # Load from disk
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r") as f:
                    data = json.load(f)
                    self.token = data.get("token", "")
                    log.info(f"Loaded saved token: {self.token[:8]}...")
            except Exception as e:
                log.error(f"Failed to load config: {e}")

    def save(self):
        try:
            with open(CONFIG_FILE, "w") as f:
                json.dump({"token": self.token}, f)
            log.info("Config saved.")
        except Exception as e:
            log.error(f"Failed to save config: {e}")

state = AgentState()

# ------------- SSL Context -------------
def get_ssl_context():
    """Create SSL context that works inside PyInstaller bundles on macOS."""
    ctx = ssl.create_default_context(cafile=certifi.where())
    return ctx

# ------------- eKassam Auth Token Generation -------------
import hashlib
import secrets as _secrets
from datetime import datetime as _dt

def generate_ekassam_headers(key: str) -> dict:
    """Generate SHA-256 auth headers for eKassam terminal API."""
    dt_str = _dt.now().strftime("%Y%m%d%H%M%S")
    nonce = _secrets.token_hex(4)
    sha_dt = hashlib.sha256(dt_str.encode()).hexdigest()
    token = hashlib.sha256(f"{sha_dt}:{nonce}:{key}".encode()).hexdigest()
    return {"dt": dt_str, "nonce": nonce, "token": token}

# Actions that use GET (info/status queries)
_GET_ACTIONS = {"kas_info", "kas_shift", "kas_lastdoc", "kas_xreport"}

# ------------- WebSocket Client -------------

async def websocket_loop(gui_queue):
    """Background WebSocket loop. Communicates with GUI only via gui_queue."""
    ws_url = f"{SERVER_URL}/{state.token}"
    ssl_ctx = get_ssl_context()
    log.info(f"WebSocket target: {ws_url}")

    while state.should_run:
        try:
            gui_queue.put(("Подключение...", "orange"))
            log.info("Connecting to WebSocket...")
            
            async with websockets.connect(
                ws_url,
                ping_interval=20,
                ping_timeout=20,
                ssl=ssl_ctx,
            ) as websocket:
                state.is_connected = True
                gui_queue.put(("Подключено ✓", "#00d68f"))
                log.info("🟢 Connected to eSatici server!")
                
                async for message in websocket:
                    if not state.should_run:
                        break
                    
                    try:
                        data = json.loads(message)
                        action = data.get("action")
                        ip = data.get("ip")
                        port = data.get("port")
                        key = data.get("key", "")
                        payload = data.get("payload", {})
                        request_id = data.get("request_id")
                        
                        target_url = f"http://{ip}:{port}/api/{action}"
                        log.info(f"Forwarding: {action} → {target_url}")
                        
                        # Generate eKassam auth headers
                        headers = generate_ekassam_headers(key) if key else {}
                        
                        try:
                            if action in _GET_ACTIONS:
                                response = requests.get(target_url, headers=headers, timeout=15)
                            else:
                                headers["Content-Type"] = "application/json"
                                response = requests.post(target_url, headers=headers, json=payload, timeout=25)
                            result = response.json()
                            log.info(f"Terminal response code: {result.get('code', '?')}")
                        except Exception as e:
                            log.error(f"Local terminal error: {e}")
                            result = {"code": 500, "message": str(e), "data": None}
                            
                        response_back = {
                            "request_id": request_id,
                            "type": "terminal_response",
                            "data": result
                        }
                        await websocket.send(json.dumps(response_back))
                    except Exception as e:
                        log.error(f"Error handling message: {e}")
                        
        except websockets.exceptions.ConnectionClosed as e:
            state.is_connected = False
            gui_queue.put(("Связь прервана. Переподключение...", "red"))
            log.warning(f"Connection closed: {e}")
            await asyncio.sleep(3)
        except Exception as e:
            state.is_connected = False
            gui_queue.put(("Ошибка сети. Ожидание...", "red"))
            log.error(f"WebSocket error: {type(e).__name__}: {e}")
            await asyncio.sleep(5)

    state.is_connected = False
    gui_queue.put(("Остановлено", "red"))
    log.info("WebSocket loop stopped.")

def start_background_loop(gui_queue):
    """Runs the async WebSocket loop in a separate thread."""
    try:
        loop = asyncio.new_event_loop()
        state.loop = loop
        asyncio.set_event_loop(loop)
        loop.run_until_complete(websocket_loop(gui_queue))
    except Exception as e:
        log.error(f"Background loop crashed: {type(e).__name__}: {e}")
        gui_queue.put((f"Критическая ошибка", "red"))

# ------------- GUI -------------

class ESaticiApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("eSatici eKassa Agent")
        self.geometry("400x280")
        self.resizable(False, False)
        
        # Thread-safe queue for UI updates (text, color)
        self.gui_queue = queue.Queue()
        
        # UI Elements
        self.lbl_title = ctk.CTkLabel(self, text="eSatici eKassa", font=ctk.CTkFont(size=20, weight="bold"))
        self.lbl_title.pack(pady=(20, 5))
        
        self.lbl_status = ctk.CTkLabel(self, text="Не подключено", text_color="red")
        self.lbl_status.pack(pady=5)

        self.token_var = ctk.StringVar(value=state.token)
        self.entry_token = ctk.CTkEntry(self, textvariable=self.token_var, placeholder_text="Укажите Token из сайта", width=250)
        self.entry_token.pack(pady=10)

        self.btn_toggle = ctk.CTkButton(self, text="Подключить", command=self.toggle_connection)
        self.btn_toggle.pack(pady=10)
        
        self.lbl_info = ctk.CTkLabel(self, text="Агент работает в фоновом режиме", font=ctk.CTkFont(size=10), text_color="gray")
        self.lbl_info.pack(pady=5)
        
        self.lbl_log = ctk.CTkLabel(self, text=f"Лог: {LOG_FILE}", font=ctk.CTkFont(size=9), text_color="gray")
        self.lbl_log.pack(pady=(0, 10))

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        
        self.worker_thread = None
        
        # Start polling the queue for UI updates
        self.poll_queue()

    def poll_queue(self):
        """Safely process GUI updates from the background thread."""
        try:
            while True:
                text, color = self.gui_queue.get_nowait()
                self.lbl_status.configure(text=text, text_color=color)
        except queue.Empty:
            pass
        except Exception as e:
            log.error(f"poll_queue error: {e}")
        self.after(150, self.poll_queue)

    def toggle_connection(self):
        if state.should_run:
            # Stop
            state.should_run = False
            if state.loop:
                state.loop.call_soon_threadsafe(state.loop.stop)
            self.btn_toggle.configure(text="Подключить", fg_color=["#3B8ED0", "#1F6AA5"])
            self.lbl_status.configure(text="Остановлено", text_color="red")
            self.entry_token.configure(state="normal")
        else:
            # Start
            token = self.token_var.get().strip()
            if not token:
                self.lbl_status.configure(text="Укажите Token!", text_color="orange")
                return
            state.token = token
            state.save()
            state.should_run = True
            self.entry_token.configure(state="disabled")
            self.btn_toggle.configure(text="Отключить", fg_color="#C8504B", hover_color="#8c3632")
            
            # Pass queue, NOT self, to the background thread
            self.worker_thread = threading.Thread(
                target=start_background_loop,
                args=(self.gui_queue,),
                daemon=True,
            )
            self.worker_thread.start()

    def on_close(self):
        state.should_run = False
        if state.loop:
            state.loop.call_soon_threadsafe(state.loop.stop)
        self.quit()

if __name__ == "__main__":
    ctk.set_appearance_mode("Dark")
    ctk.set_default_color_theme("blue")
    
    log.info("=== eSatici eKassa Agent starting ===")
    log.info(f"Python: {sys.version}")
    log.info(f"SSL: {ssl.OPENSSL_VERSION}")
    log.info(f"Certifi: {certifi.where()}")
    
    app = ESaticiApp()
    
    # Auto start if token exists
    if state.token:
        app.toggle_connection()
        
    app.mainloop()
