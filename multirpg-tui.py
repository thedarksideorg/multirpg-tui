import asyncio
import fnmatch
import os
import re
import sqlite3
import ssl
import sys
import time
from datetime import datetime
from collections import deque

# ----- Core Functions and Utilities ----- #

DB_FILE = "multirpg.db"
CHAT_BUFFER_SIZE = 100
MIN_WIDTH = 100
MIN_HEIGHT = 30

def get_term_size():
    try:
        return os.get_terminal_size()
    except OSError:
        return os.terminal_size((100, 30))

def clear_screen():
    print("\033[2J\033[H", end="")

def ansi_len(text):
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    return len(ansi_escape.sub('', text))

def pad_str(text, width, align='left'):
    visible = ansi_len(text)
    spaces = max(0, width - visible)
    if align == 'left': return text + ' ' * spaces
    if align == 'right': return ' ' * spaces + text
    if align == 'center':
        l = spaces // 2
        r = spaces - l
        return ' ' * l + text + ' ' * r

async def get_async_keystroke():
    if os.name == 'nt':
        import msvcrt
        if msvcrt.kbhit():
            return msvcrt.getch().decode('utf-8', errors='ignore')
    else:
        import select
        dr, dw, de = select.select([sys.stdin], [], [], 0.0)
        if dr:
            return sys.stdin.read(1)
    return None

def mirc2ansi(text):
    clean = re.sub(r'\x03\d{1,2}(,\d{1,2})?', '\033[32m', text) 
    clean = clean.replace('\x02', '\033[1m').replace('\x0f', '\033[0m')
    return clean + '\033[0m'

def wrap_chat_line(line, max_width):
    ansi_line = mirc2ansi(line)
    chunks = []
    current_chunk = ""
    current_len = 0
    active_color = ""
    
    color_regex = re.compile(r'(\x1B\[[0-9;]*m)')
    parts = color_regex.split(ansi_line)
    
    for part in parts:
        if color_regex.match(part):
            active_color = part
            if current_chunk or not chunks: 
                current_chunk += part
        else:
            words = part.split(' ')
            for i, word in enumerate(words):
                w_len = len(word)
                if current_len + w_len + (1 if current_len > 0 else 0) <= max_width:
                    if i > 0 and current_len > 0:
                        current_chunk += " "
                        current_len += 1
                    current_chunk += word
                    current_len += w_len
                else:
                    if current_chunk:
                        chunks.append(current_chunk + '\033[0m')
                    current_chunk = active_color + word
                    current_len = w_len
    if current_chunk:
        chunks.append(current_chunk + '\033[0m')
    return chunks if chunks else [""]
    
# ----- SQLITE DATABASE BACKEND ----- #

def init_database():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS Global_Settings (key TEXT PRIMARY KEY, value TEXT)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS botnet (
        bot_name TEXT PRIMARY KEY, network TEXT, server TEXT, port INTEGER, 
        ssl_enabled BOOLEAN, use_bouncer BOOLEAN, znc_host TEXT, znc_port INTEGER, 
        znc_user TEXT, game_password TEXT, znc_password TEXT, alignment TEXT, 
        assigned_color TEXT, status TEXT, services_cmd TEXT, user_modes TEXT,
        is_master BOOLEAN DEFAULT 0, target_gamebot TEXT DEFAULT 'multirpg',
        target_channel TEXT DEFAULT '#multirpg', ident TEXT DEFAULT 'idlerpg',
        team_name TEXT DEFAULT '', team_role TEXT DEFAULT 'None',
        sequence_mode INTEGER DEFAULT 0
    )''')
    conn.commit()
    conn.close()

def db_get_global(key, default=""):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT value FROM Global_Settings WHERE key=?", (key,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else default

def db_set_global(key, value):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO Global_Settings (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()

def db_get_all_bots():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM botnet")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

def db_upsert_bot(b):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    if b.get('is_master'):
        c.execute("UPDATE botnet SET is_master = 0")
        
    c.execute('''INSERT OR REPLACE INTO botnet 
        (bot_name, network, server, port, ssl_enabled, use_bouncer, target_gamebot, target_channel, ident)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (b.get('bot_name', ''), b.get('network', ''), b.get('server', ''), int(b.get('port', 6667) or 6667), 
         int(b.get('ssl_enabled', 1)), int(b.get('use_bouncer', 0)), b.get('target_gamebot', 'multirpg'), 
         b.get('target_channel', '#multirpg'), b.get('ident', 'idlerpg')))
    conn.commit()
    conn.close()
    
# ----- BACKGROUND SERVERS & SCRAPERS ----- #

class RawPlayers:
    @staticmethod
    async def is_character_online(char_name: str) -> bool:
        url = "https://multirpg.net/rawplayers3.php"
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=5) as resp:
                    if resp.status == 200:
                        text = await resp.text()
                        for line in text.splitlines():
                            parts = line.strip().split()
                            if parts and parts[0].lower() == char_name.lower():
                                return True
        except Exception:
            pass
        return False

async def identd_server_loop(reader, writer):
    try:
        data = await asyncio.wait_for(reader.readline(), timeout=5.0)
        line = data.decode('utf-8').strip()
        if "," in line:
            ports = line.split(",")
            response = f"{ports[0].strip()}, {ports[1].strip()} : USERID : UNIX : multirpg_user\r\n"
            writer.write(response.encode())
            await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()

async def start_identd():
    try:
        server = await asyncio.start_server(identd_server_loop, '0.0.0.0', 113)
        async with server:
            await server.serve_forever()
    except PermissionError:
        pass 
        
# ----- IRC BOT CLIENT & CONNECTION SEQUENCER ----- #

class GlobalQueue:
    def __init__(self, ui_ref):
        self.queue = asyncio.Queue()
        self.recent_actions = deque(maxlen=50) # Buffer enough history for large screens
        self.ui = ui_ref

    async def add_command(self, bot_obj, command_str: str, display_name: str = ""):
        disp = display_name if display_name else command_str
        await self.queue.put({"bot": bot_obj, "cmd": command_str})
        self.recent_actions.appendleft(f"{bot_obj.name}: {disp}")
        self.ui.draw()

    async def worker_loop(self):
        while True:
            item = await self.queue.get()
            bot = item["bot"]
            if bot.is_connected and bot.writer:
                try:
                    bot.writer.write(f"{item['cmd']}\r\n".encode("utf-8", errors="ignore"))
                    await bot.writer.drain()
                except Exception:
                    pass
            await asyncio.sleep(2.0)
            self.queue.task_done()

class MultiRPGBot:
    def __init__(self, bot_data: dict, global_queue: GlobalQueue, ui_ref):
        self.data = bot_data
        self.name = bot_data["bot_name"]
        self.target_bot = bot_data.get("target_gamebot", "multirpg")
        self.target_chan = bot_data.get("target_channel", "#multirpg")
        self.global_queue = global_queue
        self.ui = ui_ref
        self.reader = None
        self.writer = None
        self.is_connected = False
        self.is_on_quest = False
        self.task = None

    async def exec_connect(self):
        self.data["status"] = "Connecting..."
        is_already_online = await RawPlayers.is_character_online(self.name)
        
        znc_host = db_get_global("znc_host")
        znc_port = int(db_get_global("znc_port", 5000) or 5000)
        znc_pass = db_get_global("znc_pass")
        
        host = znc_host if self.data.get("use_bouncer") else self.data["server"]
        port = znc_port if self.data.get("use_bouncer") else self.data["port"]
        use_ssl = bool(self.data["ssl_enabled"])

        try:
            ssl_ctx = ssl.create_default_context() if use_ssl else None
            if ssl_ctx:
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE

            self.reader, self.writer = await asyncio.open_connection(host, port, ssl=ssl_ctx)
            self.is_connected = True
            self.data["status"] = "Online"
            self.ui.draw()

            if self.data.get("use_bouncer") and znc_pass:
                znc_user = db_get_global("znc_user", self.name)
                znc_auth = f"{znc_user}/{self.data['network']}:{znc_pass}"
                await self.global_queue.add_command(self, f"PASS {znc_auth}", "AUTH ZNC")

            await self.global_queue.add_command(self, f"NICK {self.name}", "NICK")
            ident = self.data.get("ident", "idlerpg")
            await self.global_queue.add_command(self, f"USER {ident} 0 * :{self.name}", "USER")

            if is_already_online:
                await self.global_queue.add_command(self, f"WHOIS {self.name}", "WHOIS (Self)")
            else:
                await self.global_queue.add_command(self, f"JOIN {self.target_chan}", f"JOIN {self.target_chan}")

            while self.is_connected:
                raw_bytes = await self.reader.readline()
                if not raw_bytes: break
                line = raw_bytes.decode("utf-8", errors="ignore").strip()

                if line.startswith("PING"):
                    self.writer.write(f"PONG :{line.split(':', 1)[1]}\r\n".encode("utf-8"))
                    await self.writer.drain()
                    continue

                if re.match(r"^\[\d{2}:\d{2}:\d{2}\]", line): continue

                if f" PRIVMSG {self.target_chan} :" in line:
                    match = re.match(r"^:([^!]+)![^ ]+ PRIVMSG " + re.escape(self.target_chan) + r" :(.*)$", line)
                    if match:
                        nick, msg = match.groups()
                        formatted_line = f"{datetime.now().strftime('%H%M')} <{nick}> {msg}"
                        self.ui.append_chat(formatted_line)

        except Exception as e:
            self.data["status"] = "Error"
        finally:
            self.is_connected = False
            self.data["status"] = "Offline"
            self.ui.draw()
            
# ----- DASHBOARD TUI RENDERER/STATE MACHINE ----- #

class Dashboard:
    def __init__(self):
        self.state = "BOOTLOADER" 
        self.chat_buffer = [] # Holds raw IRC strings for on-the-fly wrapping
        self.bots = db_get_all_bots()
        self.active_clients = {}
        self.global_queue = GlobalQueue(self)
        self.term_width, self.term_height = get_term_size()
        
        self.form_title = ""
        self.form_fields = []
        self.form_data = {}
        self.active_field_idx = 0

    def append_chat(self, line):
        self.chat_buffer.append(line)
        while len(self.chat_buffer) > CHAT_BUFFER_SIZE:
            self.chat_buffer.pop(0)
        if self.state == "DASHBOARD":
            self.draw()

    def draw_universal_frame(self, content_lines, title="", footer=""):
        clear_screen()
        w, h = self.term_width, self.term_height
        
        # Calculate stripped Top Title for accurate padding
        top_title = f"[ {title} ]" if title else ""
        t_len = ansi_len(top_title)
        if t_len:
            pad = max(0, w - 4 - t_len)
            print("╔══" + top_title + "═" * pad + "╗")
        else:
            print("╔" + "═" * (w - 2) + "╗")
            
        for i in range(h - 2):
            if i < len(content_lines):
                line = content_lines[i]
                padding = max(0, w - 4 - ansi_len(line))
                print(f"║ {line}{' ' * padding} ║")
            else:
                print(f"║ {' ' * (w - 4)} ║")
                
        bot_title = f"[ {footer} ]" if footer else ""
        b_len = ansi_len(bot_title)
        if b_len:
            pad = max(0, w - 4 - b_len)
            print("╚" + "═" * pad + bot_title + "══╝", end="", flush=True)
        else:
            print("╚" + "═" * (w - 2) + "╝", end="", flush=True)

    def draw_too_small(self):
        w, h = self.term_width, self.term_height
        msg = [
            f"{'TERMINAL TOO SMALL':^{w-4}}",
            "",
            f"{f'Current Size: {w}x{h}':^{w-4}}",
            f"{f'Minimum Size: {MIN_WIDTH}x{MIN_HEIGHT}':^{w-4}}",
        ]
        pad_top = max(0, (h - 2 - len(msg)) // 2)
        self.draw_universal_frame([""] * pad_top + msg, title="MultiRPG TUI", footer="v1.0.0")

    def draw_bootloader(self):
        w, h = self.term_width, self.term_height
        lines = []
        
        art = [
            r" __  __       _ _   _ ____  ____   ____      _____ _   _ ___ ",
            r"|  \/  |     | | | (_)  _ \|  _ \ / ___|    |_   _| | | |_ _|",
            r"| |\/| |_   _| | |_ _| |_) | |_) | |  _ _____ | | | | | || | ",
            r"| |  | | | | | | __| |  _ <|  __/| |_| |_____|| | | |_| || | ",
            r"|_|  |_|\__,_|_|\__|_|_| \_\_|    \____|      |_|  \___/|___|"
        ]
        
        disclaimer = [
            "by Daniel @ http://the-darkside.org",
            "Made for use with the popular IRC game, MultiRPG (IdleRPG). https://multirpg.net.",
            "I am just a fan and am not affiliated with any aspect of the game.",
            "This is just a fun project, don't abuse this.",
            "I'm not responsible for any action taken by the admins up to and including permanent bans.",
            "Use at your own risk."
        ]
        
        pad_top = max(0, (h // 2) - len(art) - 2)
        lines.extend([""] * pad_top)
        
        for art_line in art:
            lines.append(pad_str(art_line, w-4, 'center'))
            
        lines.extend([""] * 4) 
        
        for disc in disclaimer:
            lines.append(pad_str(disc, w-4, 'center'))
            
        lines.extend([""] * 3)
        lines.append(pad_str("[ PRESS ANY KEY TO CONTINUE ]", w-4, 'center'))
        
        self.draw_universal_frame(lines, title="MultiRPG TUI", footer="v1.0.0")

    def draw_dashboard(self):
        w, h = self.term_width, self.term_height
        lines = []
        
        extra_h = h - 30
        top_h = 10 + (extra_h + 1) // 2 
        chat_h = 13 + extra_h // 2
        
        total_box_inner = w - 36
        bot_w = total_box_inner // 3 
        queue_w = total_box_inner - bot_w 
        
        hotkeys = [
            " (C)onfigure Application",
            " (B)ot Screen",
            " (A)dd Bot",
            " (D)elete Bot",
            "",
            " (S)how Chat only",
            " (V)iew Raw I/O",
            " (E)nable CNC Server",
            " (Z)NC Setup",
            " (Q)uit Application"
        ]

        hk_pad = " " * 26
        lines.append(f"{hk_pad} ┌{'─' * bot_w}┐ ┌{'─' * queue_w}┐")
        

        for i in range(top_h):
            hk_str = pad_str(hotkeys[i] if i < len(hotkeys) else "", 26)
            
            if i < len(self.bots):
                b = self.bots[i]
                client = self.active_clients.get(b['bot_name'])
                status = client.data['status'] if client else b.get('status', 'Offline')
                name_w = bot_w - 11 
                b_text = f" {b['bot_name'][:name_w]:<{name_w}} {status[:9]:>9} "
            else:
                b_text = ""
                
            q_list = list(self.global_queue.recent_actions)
            q_text = f" {q_list[i][:queue_w-2]}" if i < len(q_list) else ""
            
            bot_str = pad_str(b_text, bot_w)
            queue_str = pad_str(q_text, queue_w)
            
            lines.append(f"{hk_str} │{bot_str}│ │{queue_str}│")
            
        lines.append(f"{hk_pad} └{'─' * bot_w}┘ └{'─' * queue_w}┘")
        
        master = next((b for b in self.bots if b.get('is_master')), self.bots[0] if self.bots else None)
        if master:
            mb_text = f"Master Bot: {master['bot_name']}  Network: {master['network']}  Channel: {master['target_channel']}  Server: {master['server']}"
            lines.append(pad_str(mb_text, w-4, 'center'))
        else:
            lines.append("")
            
        chat_inner = w - 6
        chat_text_w = chat_inner - 2
        
        wrapped_chat = []
        for raw_line in self.chat_buffer:
            wrapped_chat.extend(wrap_chat_line(raw_line, chat_text_w))
            
        chat_slice = wrapped_chat[-chat_h:]
        while len(chat_slice) < chat_h:
            chat_slice.insert(0, "")
            
        lines.append(f"┌{'─' * chat_inner}┐")
        
        sb = [' '] * chat_h
        if chat_h >= 3:
            sb[-1], sb[-2], sb[-3] = '█', '█', '█'
            
        for i in range(chat_h):
            c_line = pad_str(" " + chat_slice[i], chat_inner - 1)
            lines.append(f"│{c_line}{sb[i]}│")
            
        lines.append(f"└{'─' * chat_inner}┘")
        
        self.draw_universal_frame(lines, title="MultiRPG TUI", footer="v1.0.0")

    def draw_form(self):
        w, h = self.term_width, self.term_height
        lines = []
        
        lines.append("")
        lines.append(pad_str(f"{self.form_title} (Press ESC to Cancel)", w-4, 'center'))
        lines.append("")
        
        form_w = 60
        pad_l = (w - 4 - form_w) // 2
        pad_r = (w - 4) - form_w - pad_l
        
        lines.append(" " * pad_l + "┌" + "─" * (form_w-2) + "┐" + " " * pad_r)
        
        for idx, field in enumerate(self.form_fields):
            marker = ">" if idx == self.active_field_idx else " "
            val = self.form_data[field]
            if field in ["ssl_enabled", "use_bouncer"]:
                box = "[X]" if str(val) == "1" else "[ ]"
                text = f" {marker} {field.upper():<15}: {box} (Press Space to toggle)"
            else:
                text = f" {marker} {field.upper():<15}: [{val:<25}]"
                
            lines.append(" " * pad_l + "│" + pad_str(text, form_w-2) + "│" + " " * pad_r)
            
        lines.append(" " * pad_l + "└" + "─" * (form_w-2) + "┘" + " " * pad_r)
        
        used = len(lines) + 2 
        for _ in range((h - 2) - used):
            lines.append("")
            
        lines.append(pad_str("[TAB] Next Field   [ENTER] Save   [ESC] Cancel", w-4, 'center'))
        
        self.draw_universal_frame(lines, title="MultiRPG TUI", footer="v1.0.0")

    def draw(self):
        new_w, new_h = get_term_size()
        if new_w != self.term_width or new_h != self.term_height:
            self.term_width, self.term_height = new_w, new_h
            
        if self.term_width < MIN_WIDTH or self.term_height < MIN_HEIGHT:
            self.draw_too_small()
            return

        if self.state == "BOOTLOADER":
            self.draw_bootloader()
        elif self.state == "DASHBOARD":
            self.draw_dashboard()
        elif self.state in ["ADD_BOT", "ZNC_SETUP"]:
            self.draw_form()

    async def run_ui_loop(self):
        self.draw()
        if os.name != 'nt':
            import tty, termios
            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
            tty.setcbreak(sys.stdin.fileno())

        try:
            while True:
                key = await get_async_keystroke()
                if key:
                    if self.state == "BOOTLOADER":
                        self.state = "DASHBOARD"
                        self.draw()
                        
                    elif self.state == "DASHBOARD":
                        if key.lower() == 'q':
                            break
                        elif key.lower() == 'a':
                            self.state = "ADD_BOT"
                            self.form_title = "BOT CONFIGURATION"
                            self.form_fields = ["bot_name", "ident", "network", "server", "port", "ssl_enabled", "use_bouncer", "target_gamebot", "target_channel"]
                            self.form_data = {f: "" for f in self.form_fields}
                            self.form_data["ssl_enabled"] = "1"
                            self.form_data["use_bouncer"] = "0"
                            self.active_field_idx = 0
                            self.draw()
                        elif key.lower() == 'z':
                            self.state = "ZNC_SETUP"
                            self.form_title = "GLOBAL ZNC BOUNCER CONFIGURATION"
                            self.form_fields = ["znc_host", "znc_port", "znc_user", "znc_pass"]
                            self.form_data = {f: db_get_global(f) for f in self.form_fields}
                            self.active_field_idx = 0
                            self.draw()
                        elif key.lower() == 'l':
                            for b in self.bots:
                                if b['bot_name'] not in self.active_clients:
                                    client = MultiRPGBot(b, self.global_queue, self)
                                    self.active_clients[b['bot_name']] = client
                                    client.task = asyncio.create_task(client.exec_connect())
                            self.draw()

                    elif self.state in ["ADD_BOT", "ZNC_SETUP"]:
                        if key == '\x1b': 
                            self.state = "DASHBOARD"
                            self.draw()
                        elif key == '\t': 
                            self.active_field_idx = (self.active_field_idx + 1) % len(self.form_fields)
                            self.draw()
                        elif key == '\r' or key == '\n': 
                            if self.state == "ADD_BOT":
                                db_upsert_bot(self.form_data)
                                self.bots = db_get_all_bots()
                            elif self.state == "ZNC_SETUP":
                                for k, v in self.form_data.items():
                                    db_set_global(k, v)
                            self.state = "DASHBOARD"
                            self.draw()
                        elif key == ' ':
                            active = self.form_fields[self.active_field_idx]
                            if active in ["ssl_enabled", "use_bouncer"]:
                                current = self.form_data[active]
                                self.form_data[active] = "0" if current == "1" else "1"
                                self.draw()
                        elif key == '\x08' or key == '\x7f': 
                            active = self.form_fields[self.active_field_idx]
                            if active not in ["ssl_enabled", "use_bouncer"]:
                                self.form_data[active] = self.form_data[active][:-1]
                                self.draw()
                        else:
                            active = self.form_fields[self.active_field_idx]
                            if active not in ["ssl_enabled", "use_bouncer"]:
                                self.form_data[active] += key
                                self.draw()

                new_w, new_h = get_term_size()
                if new_w != self.term_width or new_h != self.term_height:
                    self.draw()
                    
                await asyncio.sleep(0.05) 
        finally:
            if os.name != 'nt':
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                
# ----- BOOTLOADER ----- # 

async def main():
    init_database()
    asyncio.create_task(start_identd())
    
    dashboard = Dashboard()
    asyncio.create_task(dashboard.global_queue.worker_loop())
    await dashboard.run_ui_loop()

if __name__ == "__main__":
    asyncio.run(main())