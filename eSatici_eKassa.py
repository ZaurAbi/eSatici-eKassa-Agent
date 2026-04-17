import asyncio
import json
import logging
import threading
import sys
import os
import requests
import websockets

import customtkinter as ctk
import pystray
from PIL import Image

# ------------- Config -------------
SERVER_URL = "wss://esatici.az/ws/v1/terminal"
CONFIG_FILE = "esatici_config.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

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
            except:
                pass

    def save(self):
        with open(CONFIG_FILE, "w") as f:
            json.dump({"token": self.token}, f)

state = AgentState()

# ------------- WebSocket Client -------------

async def websocket_loop(app_ui):
    ws_url = f"{SERVER_URL}/{state.token}"
    while state.should_run:
        try:
            app_ui.update_status("Подключение...", "orange")
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=20) as websocket:
                state.is_connected = True
                app_ui.update_status("Подключено", "green")
                logging.info("🟢 Успешно подключено к серверу eSatici!")
                
                async for message in websocket:
                    if not state.should_run:
                        break
                    
                    try:
                        data = json.loads(message)
                        action = data.get("action")
                        ip = data.get("ip")
                        port = data.get("port")
                        payload = data.get("payload", {})
                        request_id = data.get("request_id")
                        
                        target_url = f"http://{ip}:{port}/api/{action}"
                        try:
                            # Local HTTP request to physical eKassa terminal
                            response = requests.post(target_url, json=payload, timeout=15)
                            result = response.json()
                        except Exception as e:
                            logging.error(f"❌ Ошибка связи с локальной кассой: {e}")
                            result = {"code": 500, "message": str(e), "data": None}
                            
                        # Send back
                        response_back = {
                            "request_id": request_id,
                            "type": "terminal_response",
                            "data": result
                        }
                        await websocket.send(json.dumps(response_back))
                    except Exception as e:
                        logging.error(f"Error handling message: {e}")
                        
        except websockets.exceptions.ConnectionClosed:
            state.is_connected = False
            app_ui.update_status("Связь прервана. Переподключение...", "red")
            await asyncio.sleep(3)
        except Exception as e:
            state.is_connected = False
            app_ui.update_status(f"Ошибка сети. Ожидание...", "red")
            logging.error(f"WebSocket error: {e}")
            await asyncio.sleep(3)

def start_background_loop(app_ui):
    loop = asyncio.new_event_loop()
    state.loop = loop
    asyncio.set_event_loop(loop)
    loop.run_until_complete(websocket_loop(app_ui))

# ------------- GUI -------------

class ESaticiApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("eSatici eKassa Agent")
        self.geometry("400x250")
        self.resizable(False, False)
        
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
        self.lbl_info.pack(pady=10)

        self.protocol("WM_DELETE_WINDOW", self.hide_window)
        
        # System Tray icon
        self.tray_icon = None
        self.worker_thread = None

    def update_status(self, text, color):
        self.lbl_status.configure(text=text, text_color=color)

    def toggle_connection(self):
        if state.should_run:
            # Stop
            state.should_run = False
            if state.loop:
                state.loop.call_soon_threadsafe(state.loop.stop)
            self.btn_toggle.configure(text="Подключить", fg_color=["#3B8ED0", "#1F6AA5"])
            self.update_status("Остановлено", "red")
            self.entry_token.configure(state="normal")
        else:
            # Start
            token = self.token_var.get().strip()
            if not token:
                self.update_status("Укажите Token!", "orange")
                return
            state.token = token
            state.save()
            state.should_run = True
            self.entry_token.configure(state="disabled")
            self.btn_toggle.configure(text="Отключить", fg_color="#C8504B", hover_color="#8c3632")
            
            self.worker_thread = threading.Thread(target=start_background_loop, args=(self,), daemon=True)
            self.worker_thread.start()

    def hide_window(self):
        self.withdraw()
        if not self.tray_icon:
            image = Image.new('RGB', (64, 64), color=(0, 214, 143)) # Green block for simplicity
            menu = pystray.Menu(
                pystray.MenuItem('Открыть настройки', self.show_window),
                pystray.MenuItem('Выход', self.quit_app)
            )
            self.tray_icon = pystray.Icon("eSatici", image, "eSatici eKassa", menu)
            threading.Thread(target=self.tray_icon.run, daemon=True).start()

    def show_window(self, icon=None, item=None):
        if self.tray_icon:
            self.tray_icon.stop()
            self.tray_icon = None
        self.after(0, self.deiconify)

    def quit_app(self, icon=None, item=None):
        if self.tray_icon:
            self.tray_icon.stop()
        state.should_run = False
        if state.loop:
            state.loop.call_soon_threadsafe(state.loop.stop)
        self.quit()

if __name__ == "__main__":
    ctk.set_appearance_mode("Dark")  # Themes: "System", "Dark", "Light"
    ctk.set_default_color_theme("blue")
    
    app = ESaticiApp()
    
    # Auto start if token exists
    if state.token:
        app.toggle_connection()
        # Automatically hide on startup
        app.after(500, app.hide_window)
        
    app.mainloop()
