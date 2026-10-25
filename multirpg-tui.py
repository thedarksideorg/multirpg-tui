import asyncio
import fnmatch
import os
import re
import sqlite3
import ssl
import sys
import time
import random
from datetime import datetime
from collections import deque

# ==============================================================================
# CORE CONFIGURATION & HELPERS
# ==============================================================================

DB_FILE = "multirpg.db"
CHAT_BUFFER_SIZE = 100
MIN_WIDTH = 100
MIN_HEIGHT = 30
APP_TITLE = "MultiRPG TUI"
APP_VERSION = "v1.0.0"

def get_term_size():
    try:
        return os.get_terminal_size()
    except OSError:
        return os.terminal_size((100, 30))

def clear_screen():
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()

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
            ch = msvcrt.getch()
            if ch == b'\x13': return 'CTRL_S' 
            elif ch == b'\x1b': return 'ESC'
            elif ch == b'\x08': return 'BACKSPACE'
            elif ch == b'\t': return 'TAB'
            elif ch in [b'\r', b'\n']: return 'ENTER'
            elif ch == b' ': return 'SPACE'
            elif ch == b'\xe0': 
                arr = msvcrt.getch()
                if arr == b'H': return 'UP'
                elif arr == b'P': return 'DOWN'
                elif arr == b'K': return 'LEFT'
                elif arr == b'M': return 'RIGHT'
            return ch.decode('utf-8', errors='ignore')
    else:
        import select
        dr, dw, de = select.select([sys.stdin], [], [], 0.0)
        if dr:
            ch = sys.stdin.read(1)
            if ch == '\x13': return 'CTRL_S'
            elif ch == '\x1b': 
                dr2, _, _ = select.select([sys.stdin], [], [], 0.001)
                if dr2:
                    ch2 = sys.stdin.read(1)
                    if ch2 == '[':
                        dr3, _, _ = select.select([sys.stdin], [], [], 0.001)
                        if dr3:
                            ch3 = sys.stdin.read(1)
                            if ch3 == 'A': return 'UP'
                            elif ch3 == 'B': return 'DOWN'
                            elif ch3 == 'C': return 'RIGHT'
                            elif ch3 == 'D': return 'LEFT'
                            elif ch3 == 'Z': return 'SHIFT_TAB'
                return 'ESC'
            elif ch in ['\x08', '\x7f']: return 'BACKSPACE'
            elif ch == '\t': return 'TAB'
            elif ch == '\n': return 'ENTER'
            elif ch == ' ': return 'SPACE'
            return ch
    return None

def mirc2ansi(text):
    clean = re.sub(r'\x03\d{1,2}(,\d{1,2})?', '\033[32m', text) 
    clean = clean.replace('\x02', '\033[1m').replace('\x0f', '\033[0m')
    return clean + '\033[0m'

def wrap_chat_line(line, max_width):
    ansi_line = mirc2ansi(line)
    chunks, current_chunk, current_len, active_color = [], "", 0, ""
    color_regex = re.compile(r'(\x1B\[[0-9;]*m)')
    
    for part in color_regex.split(ansi_line):
        if color_regex.match(part):
            active_color = part
            if current_chunk or not chunks: current_chunk += part
        else:
            for i, word in enumerate(part.split(' ')):
                w_len = len(word)
                if current_len + w_len + (1 if current_len > 0 else 0) <= max_width:
                    if i > 0 and current_len > 0:
                        current_chunk += " "
                        current_len += 1
                    current_chunk += word
                    current_len += w_len
                else:
                    if current_chunk: chunks.append(current_chunk + '\033[0m')
                    current_chunk = active_color + word
                    current_len = w_len
    if current_chunk: chunks.append(current_chunk + '\033[0m')
    return chunks if chunks else [""]

# ==============================================================================
# DATABASE ENGINE
# ==============================================================================

def init_database():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS Global_Settings (key TEXT PRIMARY KEY, value TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS botnet (
        botid TEXT PRIMARY KEY, nick TEXT, altnick TEXT, identd TEXT, server TEXT, port INTEGER, 
        use_znc BOOLEAN, use_ssl BOOLEAN, target_bot TEXT, target_chan TEXT, game_password TEXT,
        class TEXT, gender TEXT, alignment TEXT, priority TEXT, min_gold INTEGER,
        auth_service TEXT, auth_cmd TEXT, usermode TEXT, wait_mode BOOLEAN, verify_vhost BOOLEAN,
        team_name TEXT, make_team BOOLEAN, auto_fight BOOLEAN, auto_challenge BOOLEAN, 
        auto_bet BOOLEAN, auto_slay BOOLEAN, is_master BOOLEAN DEFAULT 0, status TEXT DEFAULT 'Stopped'
    )''')
    
    # Initialize defaults if not exist
    c.execute("INSERT OR IGNORE INTO Global_Settings (key, value) VALUES ('queue_delay_ms', '3000')")
    c.execute("INSERT OR IGNORE INTO Global_Settings (key, value) VALUES ('cnc_enabled', '0')")
    
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
    c.execute('''INSERT OR REPLACE INTO botnet 
        (botid, nick, altnick, identd, server, port, use_znc, use_ssl, target_bot, target_chan, 
         game_password, class, gender, alignment, priority, min_gold, auth_service, auth_cmd, 
         usermode, wait_mode, verify_vhost, team_name, make_team, auto_fight, auto_challenge, auto_bet, auto_slay)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (b.get('botid', ''), b.get('nick', ''), b.get('altnick', ''), b.get('identd', ''), 
         b.get('server', ''), int(b.get('port') or 6667), int(b.get('use_znc') or 0), 
         int(b.get('use_ssl') or 0), b.get('target_bot', ''), b.get('target_chan', ''), 
         b.get('game_password', ''), b.get('class', ''), b.get('gender', 'Male'), b.get('alignment', 'Human'), 
         b.get('priority', 'Engineer > Hero'), int(b.get('min_gold') or 200), b.get('auth_service', ''), 
         b.get('auth_cmd', ''), b.get('usermode', ''), int(b.get('wait_mode') or 1), 
         int(b.get('verify_vhost') or 0), b.get('team_name', ''), int(b.get('make_team') or 0),
         int(b.get('auto_fight') or 0), int(b.get('auto_challenge') or 0), int(b.get('auto_bet') or 0), int(b.get('auto_slay') or 0)))
    conn.commit()
    conn.close()

# ==============================================================================
# IDENTD SERVER
# ==============================================================================

async def identd_server_loop(reader, writer):
    try:
        data = await asyncio.wait_for(reader.readline(), timeout=5.0)
        line = data.decode('utf-8').strip()
        if "," in line:
            ports = line.split(",")
            writer.write(f"{ports[0].strip()}, {ports[1].strip()} : USERID : UNIX : multirpg\r\n".encode())
            await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()

async def start_identd():
    try:
        server = await asyncio.start_server(identd_server_loop, '0.0.0.0', 113)
        async with server: await server.serve_forever()
    except PermissionError: pass 

# ==============================================================================
# IRC SEQUENCER & BOT ENGINE
# ==============================================================================

class GlobalQueue:
    def __init__(self, ui_ref):
        self.queue = asyncio.Queue()
        self.recent_actions = deque(maxlen=50) 
        self.ui = ui_ref

    async def add_action(self, bot_obj, action_type, payload, display_name=""):
        disp = display_name if display_name else payload
        await self.queue.put({
            "bot": bot_obj,
            "type": action_type,
            "payload": payload,
            "display": disp
        })
        self.recent_actions.appendleft(f"{bot_obj.data['botid']}: {disp}")
        self.ui.draw()

    async def worker_loop(self):
        while True:
            item = await self.queue.get()
            bot = item["bot"]
            atype = item["type"]
            payload = item["payload"]
            disp = item["display"]

            if bot.cnc_connected and bot.cnc_writer and atype in ["CONNECT_GAME", "SEND_GAME_CMD"]:
                cnc_chan = db_get_global("cnc_chan", "#darkside")
                game_nick = bot.data.get("nick", bot.data["botid"])
                announce_msg = f"PRIVMSG {cnc_chan} :{game_nick} -> {disp}\r\n"
                bot.cnc_writer.write(announce_msg.encode("utf-8", errors="ignore"))
                await bot.cnc_writer.drain()

            try:
                if atype == "CONNECT_CNC":
                    await bot.exec_cnc_connect()
                elif atype == "CONNECT_GAME":
                    await bot.exec_game_connect()
                elif atype == "SEND_CNC_CMD":
                    if bot.cnc_connected and bot.cnc_writer:
                        bot.cnc_writer.write(f"{payload}\r\n".encode("utf-8", errors="ignore"))
                        await bot.cnc_writer.drain()
                elif atype == "SEND_GAME_CMD":
                    if bot.game_connected and bot.game_writer:
                        bot.game_writer.write(f"{payload}\r\n".encode("utf-8", errors="ignore"))
                        await bot.game_writer.drain()
            except Exception:
                pass

            delay_ms = int(db_get_global("queue_delay_ms", 3000))
            await asyncio.sleep(delay_ms / 1000.0)
            self.queue.task_done()

class MultiRPGBot:
    def __init__(self, bot_data: dict, global_queue: GlobalQueue, ui_ref):
        self.data = bot_data
        self.global_queue = global_queue
        self.ui = ui_ref
        
        self.cnc_reader = None
        self.cnc_writer = None
        self.cnc_connected = False
        
        self.game_reader = None
        self.game_writer = None
        self.game_connected = False

    async def start(self):
        self.data["status"] = "Queued..."
        self.ui.draw()
        cnc_enabled = int(db_get_global("cnc_enabled", 0))
        if cnc_enabled:
            await self.global_queue.add_action(self, "CONNECT_CNC", None, "Connecting to CNC")
        else:
            await self.global_queue.add_action(self, "CONNECT_GAME", None, "Connecting to Game")

    async def exec_cnc_connect(self):
        host = db_get_global("cnc_host", "irc.the-darkside.org")
        port = int(db_get_global("cnc_port", 6697))
        use_ssl = int(db_get_global("cnc_ssl", 1))
        
        try:
            ctx = ssl.create_default_context() if use_ssl else None
            if ctx:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE

            self.cnc_reader, self.cnc_writer = await asyncio.open_connection(host, port, ssl=ctx)
            self.cnc_connected = True
            self.data["status"] = "CNC Online"
            self.ui.draw()

            botid = self.data['botid']
            cnc_pass = db_get_global("cnc_pass", "")
            if cnc_pass:
                await self.global_queue.add_action(self, "SEND_CNC_CMD", f"PASS {cnc_pass}", "AUTH CNC")
                
            await self.global_queue.add_action(self, "SEND_CNC_CMD", f"NICK {botid}", "CNC NICK")
            await self.global_queue.add_action(self, "SEND_CNC_CMD", f"USER {botid} 0 * :MultiRPG CNC Uplink", "CNC USER")
            
            asyncio.create_task(self.cnc_reader_loop())
        except Exception:
            self.data["status"] = "CNC Error"
            self.ui.draw()

    async def cnc_reader_loop(self):
        cnc_chan = db_get_global("cnc_chan", "#darkside")
        joined = False
        try:
            while self.cnc_connected:
                raw = await self.cnc_reader.readline()
                if not raw: break
                line = raw.decode("utf-8", errors="ignore").strip()
                
                if line.startswith("PING"):
                    self.cnc_writer.write(f"PONG :{line.split(':', 1)[1]}\r\n".encode())
                    await self.cnc_writer.drain()
                    continue
                    
                if " 001 " in line and not joined:
                    await self.global_queue.add_action(self, "SEND_CNC_CMD", f"JOIN {cnc_chan}", f"JOIN {cnc_chan}")
                    
                if f" JOIN {cnc_chan}" in line and self.data['botid'] in line and not joined:
                    joined = True
                    await self.global_queue.add_action(self, "CONNECT_GAME", None, "Connecting to Game Server")
                    
        except Exception:
            pass
        finally:
            self.cnc_connected = False

    async def exec_game_connect(self):
        self.data["status"] = "Game Conn..."
        current_nick = self.data['nick']
        
        host = db_get_global("znc_host") if self.data.get("use_znc") else self.data["server"]
        port = int(db_get_global("znc_port", 5000)) if self.data.get("use_znc") else int(self.data["port"])
        use_ssl = int(db_get_global("znc_ssl", 0)) if self.data.get("use_znc") else self.data["use_ssl"]

        try:
            ctx = ssl.create_default_context() if use_ssl else None
            if ctx:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE

            self.game_reader, self.game_writer = await asyncio.open_connection(host, port, ssl=ctx)
            self.game_connected = True
            self.data["status"] = "Game Online"
            self.ui.draw()

            if self.data.get("use_znc"):
                znc_user = db_get_global("znc_user")
                znc_pass = db_get_global("znc_pass")
                znc_auth = f"{znc_user}/{self.data['botid']}:{znc_pass}"
                await self.global_queue.add_action(self, "SEND_GAME_CMD", f"PASS {znc_auth}", "AUTH ZNC")

            await self.global_queue.add_action(self, "SEND_GAME_CMD", f"NICK {current_nick}", "NICK")
            await self.global_queue.add_action(self, "SEND_GAME_CMD", f"USER {self.data['identd']} 0 * :{self.data['class']}", "USER")

            asyncio.create_task(self.game_reader_loop(current_nick))
        except Exception:
            self.data["status"] = "Game Error"
            self.ui.draw()

    async def game_reader_loop(self, current_nick):
        auth_sent = False
        auth_verified = False
        mode_sent = False
        mode_verified = False
        vhost_sent = False
        vhost_verified = False
        join_sent = False

        try:
            while self.game_connected:
                raw = await self.game_reader.readline()
                if not raw: break
                line = raw.decode("utf-8", errors="ignore").strip()

                if line.startswith("PING"):
                    self.game_writer.write(f"PONG :{line.split(':', 1)[1]}\r\n".encode())
                    await self.game_writer.drain()
                    continue

                if re.match(r"^\[\d{2}:\d{2}:\d{2}\]", line): continue

                if " 433 " in line:
                    if current_nick == self.data['nick'] and self.data['altnick']: current_nick = self.data['altnick']
                    else: current_nick = f"{self.data['nick']}{random.randint(100,999)}"
                    await self.global_queue.add_action(self, "SEND_GAME_CMD", f"NICK {current_nick}", f"NICK FALLBACK")

                if " 001 " in line and not auth_sent:
                    auth_sent = True
                    if self.data.get('auth_cmd'):
                        await self.global_queue.add_action(self, "SEND_GAME_CMD", self.data['auth_cmd'], "AUTH SERVICES")
                    else:
                        auth_verified = True 

                if auth_sent and not auth_verified:
                    if self.data.get('auth_service') and self.data.get('auth_cmd'):
                        match = re.match(r"^:([^!]+).*? NOTICE " + re.escape(current_nick) + r" :", line)
                        if match and match.group(1).lower() == self.data['auth_service'].lower():
                            auth_verified = True
                    else:
                        auth_verified = True

                if auth_verified and not mode_sent:
                    mode_sent = True
                    if self.data.get('usermode'):
                        await self.global_queue.add_action(self, "SEND_GAME_CMD", f"MODE {current_nick} {self.data['usermode']}", "SET MODE")
                    else:
                        mode_verified = True

                if mode_sent and not mode_verified:
                    if self.data.get('usermode') and self.data.get('wait_mode'):
                        search_mode = self.data['usermode'].replace("+", "").replace("-", "")
                        if f" MODE {current_nick} " in line and search_mode in line:
                            mode_verified = True
                    else:
                        mode_verified = True

                if mode_verified and not vhost_sent:
                    vhost_sent = True
                    if self.data.get('verify_vhost') and self.data.get('use_znc'):
                        await self.global_queue.add_action(self, "SEND_GAME_CMD", f"WHOIS {current_nick}", "VERIFY HOST")
                    else:
                        vhost_verified = True

                if vhost_sent and not vhost_verified:
                    if " 378 " in line or " 311 " in line or " 338 " in line:
                        vhost_verified = True

                if vhost_verified and not join_sent:
                    is_motd_end = " 376 " in line or " 422 " in line
                    is_whois_end = " 318 " in line and current_nick in line
                    wait_for = is_whois_end if (self.data.get('verify_vhost') and self.data.get('use_znc')) else is_motd_end
                    if wait_for:
                        join_sent = True
                        await self.global_queue.add_action(self, "SEND_GAME_CMD", f"JOIN {self.data['target_chan']}", "JOIN CHAN")
                        if self.data.get("game_password"):
                            await self.global_queue.add_action(self, "SEND_GAME_CMD", f"PRIVMSG {self.data['target_bot']} :LOGIN {current_nick} {self.data['game_password']}", "LOGIN GAME")

                if f" PRIVMSG {self.data['target_chan']} :" in line:
                    match = re.match(r"^:([^!]+)![^ ]+ PRIVMSG " + re.escape(self.data['target_chan']) + r" :(.*)$", line)
                    if match:
                        nick, msg = match.groups()
                        self.ui.append_chat(f"{datetime.now().strftime('%H%M')} <{nick}> {msg}")

        except Exception as e:
            pass
        finally:
            self.game_connected = False
            self.data["status"] = "Offline"
            self.ui.draw()

# ==============================================================================
# MASTER TUI RENDERER & STATE MACHINE
# ==============================================================================

class Dashboard:
    def __init__(self):
        self.state = "BOOTLOADER" 
        self.chat_buffer = [] 
        self.bots = db_get_all_bots()
        self.active_clients = {}
        self.global_queue = GlobalQueue(self)
        self.term_width, self.term_height = get_term_size()
        
        self.form_title = ""
        self.form_fields = []
        self.form_data = {}
        self.active_field_idx = 0
        
        self.modal_active = False
        self.modal_field = ""

    def append_chat(self, line):
        self.chat_buffer.append(line)
        while len(self.chat_buffer) > CHAT_BUFFER_SIZE: self.chat_buffer.pop(0)
        if self.state == "DASHBOARD": self.draw()

    def draw_universal_frame(self, content_lines, left_title="", right_title="", footer=""):
        clear_screen()
        w, h = self.term_width, self.term_height
        
        lt = f"[ {left_title} ]" if left_title else ""
        rt = f"[ {right_title} ]" if right_title else ""
        used_top = ansi_len(lt) + ansi_len(rt)
        
        if used_top < w - 2:
            pad = max(0, w - 2 - used_top)
            sys.stdout.write(f"╔{lt}{'═' * pad}{rt}╗\n")
        else:
            sys.stdout.write("╔" + "═" * (w - 2) + "╗\n")
            
        for i in range(h - 2):
            if i < len(content_lines):
                line = content_lines[i]
                padding = max(0, w - 4 - ansi_len(line))
                sys.stdout.write(f"║ {line}{' ' * padding} ║\n")
            else:
                sys.stdout.write(f"║ {' ' * (w - 4)} ║\n")
                
        bot_title = f"[ {footer} ]" if footer else ""
        b_len = ansi_len(bot_title)
        pad = max(0, w - 2 - b_len) if b_len else w - 2
        sys.stdout.write("╚" + "═" * pad + bot_title + ("╝" if b_len else "╝"))
        sys.stdout.flush()

    def draw_z_modal(self):
        w, h = self.term_width, self.term_height
        box_w = max(50, len(self.modal_field) + 20)
        box_h = 5
        start_y = (h // 2) - (box_h // 2)
        start_x = (w - box_w) // 2
        val = self.form_data.get(self.modal_field, "")
        
        lines = [
            f"╔{'═'*(box_w-2)}╗",
            f"║{f'[ {self.modal_field.upper()} ]':^{box_w-2}}║",
            f"║ {'Enter new value:':<{box_w-3}}║",
            f"║ > {val:<{box_w-5}}║",
            f"╚{'═'*(box_w-2)}╝"
        ]
        
        for i, text in enumerate(lines):
            sys.stdout.write(f"\033[{start_y + i + 1};{start_x + 1}H{text}")
            
        cursor_x = start_x + 6 + len(str(val))
        sys.stdout.write(f"\033[{start_y + 4};{cursor_x}H\033[?25h")
        sys.stdout.flush()

    def draw_too_small(self):
        w, h = self.term_width, self.term_height
        msg = [
            f"{'TERMINAL TOO SMALL':^{w-4}}", "",
            f"{f'Current Size: {w}x{h}':^{w-4}}",
            f"{f'Minimum Size: {MIN_WIDTH}x{MIN_HEIGHT}':^{w-4}}",
        ]
        pad_top = max(0, (h - 2 - len(msg)) // 2)
        self.draw_universal_frame([""] * pad_top + msg, left_title=APP_TITLE, footer=APP_VERSION)

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
        pad_top = max(0, (h // 2) - len(art) - 2)
        lines.extend([""] * pad_top)
        for art_line in art: lines.append(pad_str(art_line, w-4, 'center'))
        lines.extend([""] * 7)
        lines.append(pad_str("[ PRESS ANY KEY TO CONTINUE ]", w-4, 'center'))
        self.draw_universal_frame(lines, left_title=APP_TITLE, footer=APP_VERSION)

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
            " (O)nly Show Chat", " (C)onfigure Application", " (S)etup CNC Server", " (B)ot Screen", " (A)dd Bot",
            " (D)elete Bot", " (Z)NC Setup", " (V)iew Raw I/O", " (Q)uit Application"
        ]

        hk_pad = " " * 26
        lines.append(f"{hk_pad} ┌{'─' * bot_w}┐ ┌{'─' * queue_w}┐")
        
        for i in range(top_h):
            hk_str = pad_str(hotkeys[i] if i < len(hotkeys) else "", 26)
            if i < len(self.bots):
                b = self.bots[i]
                status = self.active_clients[b['botid']].data['status'] if b['botid'] in self.active_clients else b.get('status', 'Offline')
                name_w = bot_w - 11 
                b_text = f" {b['botid'][:name_w]:<{name_w}} {status[:9]:>9} "
            else:
                b_text = ""
                
            q_list = list(self.global_queue.recent_actions)
            q_text = f" {q_list[i][:queue_w-2]}" if i < len(q_list) else ""
            lines.append(f"{hk_str} │{pad_str(b_text, bot_w)}│ │{pad_str(q_text, queue_w)}│")
            
        lines.append(f"{hk_pad} └{'─' * bot_w}┘ └{'─' * queue_w}┘")
        
        master = next((b for b in self.bots if b.get('is_master')), self.bots[0] if self.bots else None)
        if master: lines.append(pad_str(f"Master Bot: {master['botid']}  Network: {master['server']}  Channel: {master['target_chan']}", w-4, 'center'))
        else: lines.append("")
            
        chat_inner = w - 6
        wrapped_chat = []
        for raw_line in self.chat_buffer: wrapped_chat.extend(wrap_chat_line(raw_line, chat_inner - 2))
        chat_slice = wrapped_chat[-chat_h:]
        while len(chat_slice) < chat_h: chat_slice.insert(0, "")
            
        lines.append(f"┌{'─' * chat_inner}┐")
        sb = [' '] * chat_h
        if chat_h >= 3: sb[-1], sb[-2], sb[-3] = '█', '█', '█'
        for i in range(chat_h): lines.append(f"│{pad_str(' ' + chat_slice[i], chat_inner - 1)}{sb[i]}│")
        lines.append(f"└{'─' * chat_inner}┘")
        self.draw_universal_frame(lines, left_title=APP_TITLE, footer=APP_VERSION)

    def draw_bot_screen(self):
        w, h = self.term_width, self.term_height
        lines = ["", ""]
        
        lines.append(pad_str("AVAILABLE BOTS:", w-4, 'center'))
        lines.append("")
        
        # Bot List (Selectable)
        for idx, bot in enumerate(self.bots):
            marker = ">" if idx == self.active_field_idx else " "
            status = self.active_clients[bot['botid']].data['status'] if bot['botid'] in self.active_clients else bot.get('status', 'Offline')
            lines.append(pad_str(f" {marker} [{bot['botid']:<15}] - {bot['server']:<25} ({status})", w-4, 'center'))
            
        used = len(lines) + 2 
        for _ in range((h - 2) - used): lines.append("")
        lines.append(pad_str("[UP/DOWN] Select   [ENTER] Launch Bot   [L] Launch All   [ESC] Back", w-4, 'center'))
        self.draw_universal_frame(lines, left_title=APP_TITLE, right_title=self.form_title, footer=APP_VERSION)


    def _render_field(self, field, c_w, is_active):
        if not field: return " " * c_w
        marker = ">" if is_active else " "
        val = self.form_data.get(field, "")
        
        labels = {
            'botid': 'BOT ID', 'nick': 'NICK', 'altnick': 'ALTNICK', 'identd': 'IDENTD',
            'server': 'SERVER', 'port': 'PORT', 'auth_service': 'SERVICE', 'auth_cmd': 'COMMAND',
            'usermode': 'USERMODE', 'target_bot': 'BOT', 'target_chan': 'CHAN',
            'game_password': 'PASS', 'class': 'CLASS', 'gender': 'GENDER', 'alignment': 'ALIGNMENT',
            'priority': 'PRIORITY', 'min_gold': 'MIN GOLD', 'team_name': 'TEAM NAME',
            'queue_delay_ms': 'QUEUE DELAY MS', 'cnc_host': 'CNC HOST', 'cnc_port': 'CNC PORT',
            'cnc_chan': 'CNC CHAN', 'cnc_pass': 'CNC PASS'
        }
        t_labels = {
            'use_znc': 'USE ZNC BOUNCER', 'use_ssl': 'USE SSL/TLS CONNECTION',
            'wait_mode': 'WAIT FOR USERMODE TO JOIN', 'verify_vhost': 'VERIFY VHOST BEFORE JOINING',
            'make_team': 'MAKE TEAM', 'auto_fight': 'AUTO FIGHT', 'auto_challenge': 'AUTO CHALLENGE',
            'auto_bet': 'AUTO BET', 'auto_slay': 'AUTO SLAY', 'cnc_ssl': 'USE SSL/TLS CONNECTION',
            'cnc_enabled': 'ENABLE CNC UPLINK FOR BOTS', 'znc_ssl': 'USE SSL/TLS CONNECTION'
        }
        
        if field in labels:
            bracket_w = c_w - 18
            if field in ["gender", "alignment", "priority"]:
                core = f" {marker} {labels[field]:<10} [ {str(val)[:bracket_w-4]:^{bracket_w-4}} ] "
            else:
                core = f" {marker} {labels[field]:<10} [ {str(val)[:bracket_w-4]:<{bracket_w-4}} ] "
            return pad_str(core, c_w)
        elif field in t_labels:
            box = "[X]" if str(val) == "1" or val is True else "[ ]"
            return pad_str(f" {marker} {box} {t_labels[field]}", c_w)
        return " " * c_w

    def draw_bot_form(self):
        w, h = self.term_width, self.term_height
        lines = ["", ""]
        c1_w = (w - 4) // 2
        c2_w = (w - 4) - c1_w
        
        for i in range(14):
            f1 = self.form_fields[i*2] if i*2 < len(self.form_fields) else None
            f2 = self.form_fields[i*2+1] if i*2+1 < len(self.form_fields) else None
            if f1 is None and f2 is None:
                lines.append("")
            else:
                lines.append(f"{self._render_field(f1, c1_w, f1 == self.form_fields[self.active_field_idx])}{self._render_field(f2, c2_w, f2 == self.form_fields[self.active_field_idx])}")
            
        used = len(lines) + 2 
        for _ in range((h - 2) - used): lines.append("")
        lines.append(pad_str("[TAB]/[SHIFT+TAB] Navigate   [ARROWS] Move   [SPACE] Cycle/Toggle   [ENTER] Type Value   [CTRL+S] Save   [ESC] Cancel", w-4, 'center'))
        
        self.draw_universal_frame(lines, left_title=APP_TITLE, right_title=self.form_title, footer=APP_VERSION)

    def draw_single_col_form(self):
        w, h = self.term_width, self.term_height
        lines = ["", "", "", "", ""]
        form_w = 60
        pad_l = (w - 4 - form_w) // 2
        pad_r = (w - 4) - form_w - pad_l
        
        lines.append(" " * pad_l + "┌" + "─" * (form_w-2) + "┐" + " " * pad_r)
        for idx, field in enumerate(self.form_fields):
            marker = ">" if idx == self.active_field_idx else " "
            val = self.form_data[field]
            label = field.upper().replace("_", " ")[:14]
            is_tick = field in ['znc_ssl', 'cnc_ssl', 'cnc_enabled']
            
            if is_tick:
                text = f" {marker} {label:<14}: {'[X]' if str(val) == '1' else '[ ]'}"
            elif field in ["znc_pass", "cnc_pass"] and val:
                text = f" {marker} {label:<14}: [ {'*******':<23} ]"
            else:
                text = f" {marker} {label:<14}: [ {str(val)[:23]:<23} ]"
            lines.append(" " * pad_l + "│" + pad_str(text, form_w-2) + "│" + " " * pad_r)
            
        lines.append(" " * pad_l + "└" + "─" * (form_w-2) + "┘" + " " * pad_r)
        used = len(lines) + 2 
        for _ in range((h - 2) - used): lines.append("")
        lines.append(pad_str("[TAB]/[SHIFT+TAB] Navigate   [ARROWS] Move   [SPACE] Toggle   [ENTER] Type Value   [CTRL+S] Save   [ESC] Cancel", w-4, 'center'))
        self.draw_universal_frame(lines, left_title=APP_TITLE, right_title=self.form_title, footer=APP_VERSION)

    def draw(self):
        new_w, new_h = get_term_size()
        if new_w != self.term_width or new_h != self.term_height: self.term_width, self.term_height = new_w, new_h
        if self.term_width < MIN_WIDTH or self.term_height < MIN_HEIGHT: return self.draw_too_small()

        if self.state == "BOOTLOADER": self.draw_bootloader()
        elif self.state == "DASHBOARD": self.draw_dashboard()
        elif self.state == "BOT_SCREEN": self.draw_bot_screen()
        elif self.state == "ADD_BOT": self.draw_bot_form()
        elif self.state in ["ZNC_SETUP", "CNC_SETUP", "APP_CONFIG"]: self.draw_single_col_form()
        
        if self.modal_active: self.draw_z_modal()

    def move_idx(self, delta):
        new_idx = self.active_field_idx + delta
        while 0 <= new_idx < len(self.form_fields):
            if self.form_fields[new_idx] is not None:
                self.active_field_idx = new_idx
                return
            new_idx += delta if delta != 0 else 1

    async def run_ui_loop(self):
        self.draw()
        if os.name != 'nt':
            import tty, termios
            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
            new_settings = termios.tcgetattr(fd)
            new_settings[0] = new_settings[0] & ~termios.IXON
            tty.setcbreak(sys.stdin.fileno())
            termios.tcsetattr(fd, termios.TCSANOW, new_settings)

        try:
            while True:
                key = await get_async_keystroke()
                if not key:
                    new_w, new_h = get_term_size()
                    if new_w != self.term_width or new_h != self.term_height: self.draw()
                    await asyncio.sleep(0.05)
                    continue
                    
                if self.term_width < MIN_WIDTH or self.term_height < MIN_HEIGHT: continue 

                if self.modal_active:
                    if key == 'ENTER':
                        self.modal_active = False
                        sys.stdout.write("[?25l") 
                        if self.modal_field == "team_name":
                            if not self.form_data['team_name']: self.form_data['make_team'] = 0
                            elif self.form_data['make_team'] == 1:
                                db_bots = db_get_all_bots()
                                conflict = any(b['team_name'] == self.form_data['team_name'] and b['make_team'] and b['botid'] != self.form_data['botid'] for b in db_bots)
                                if conflict: self.form_data['make_team'] = 0
                        self.draw()
                    elif key == 'ESC':
                        self.modal_active = False
                        sys.stdout.write("[?25l")
                        self.form_data[self.modal_field] = self.modal_backup_val
                        self.draw()
                    elif key == 'BACKSPACE':
                        self.form_data[self.modal_field] = str(self.form_data[self.modal_field])[:-1]
                        self.draw()
                    elif len(key) == 1 and key.isprintable():
                        self.form_data[self.modal_field] = str(self.form_data[self.modal_field]) + key
                        self.draw()
                    continue
                
                if self.state == "BOOTLOADER":
                    self.state = "DASHBOARD"
                    self.draw()
                    
                elif self.state == "DASHBOARD":
                    if key.lower() == 'q': break
                    elif key.lower() == 'a':
                        self.state = "ADD_BOT"
                        self.form_title = "BOT CONFIGURATION"
                        # Interleaved array for perfect 2-column navigation grid
                        self.form_fields = [
                            'botid', 'target_bot',
                            'nick', 'target_chan',
                            'altnick', 'game_password',
                            'identd', 'class',
                            'server', 'gender',
                            'port', 'alignment',
                            'auth_service', 'priority',
                            'auth_cmd', 'min_gold',
                            'usermode', 'team_name',
                            None, None, 
                            'use_znc', 'make_team',
                            'use_ssl', 'auto_fight',
                            'wait_mode', 'auto_challenge',
                            'verify_vhost', 'auto_bet',
                            None, 'auto_slay'
                        ]
                        self.form_data = {f: "" for f in self.form_fields if f}
                        self.form_data['port'], self.form_data['min_gold'] = 6667, 200
                        for f in ['use_ssl', 'wait_mode', 'verify_vhost', 'use_znc', 'make_team', 'auto_fight', 'auto_challenge', 'auto_bet', 'auto_slay']: 
                            self.form_data[f] = 0
                        self.form_data['wait_mode'] = 1
                        self.form_data['alignment'], self.form_data['gender'], self.form_data['priority'] = "Human", "Male", "Engineer > Hero"
                        self.active_field_idx = 0
                        self.draw()
                    elif key.lower() == 'z':
                        self.state = "ZNC_SETUP"
                        self.form_title = "GLOBAL ZNC CONFIGURATION"
                        self.form_fields = ["znc_host", "znc_port", "znc_user", "znc_pass", "znc_ssl"]
                        self.form_data = {f: db_get_global(f) for f in self.form_fields}
                        if not self.form_data['znc_ssl']: self.form_data['znc_ssl'] = 0
                        self.active_field_idx = 0
                        self.draw()
                    elif key.lower() == 'c':
                        self.state = "APP_CONFIG"
                        self.form_title = "CONFIGURE APPLICATION"
                        self.form_fields = ["queue_delay_ms"]
                        self.form_data = {f: db_get_global(f) for f in self.form_fields}
                        self.active_field_idx = 0
                        self.draw()
                    elif key.lower() == 's':
                        self.state = "CNC_SETUP"
                        self.form_title = "CNC SERVER SETUP"
                        self.form_fields = ["cnc_host", "cnc_port", "cnc_chan", "cnc_pass", "cnc_ssl", "cnc_enabled"]
                        self.form_data = {f: db_get_global(f) for f in self.form_fields}
                        if not self.form_data['cnc_ssl']: self.form_data['cnc_ssl'] = 0
                        if not self.form_data['cnc_enabled']: self.form_data['cnc_enabled'] = 0
                        self.active_field_idx = 0
                        self.draw()
                    elif key.lower() == 'b':
                        self.state = "BOT_SCREEN"
                        self.form_title = "BOT MANAGEMENT"
                        self.bots = db_get_all_bots()
                        self.active_field_idx = 0
                        self.draw()

                elif self.state == "BOT_SCREEN":
                    if key == 'ESC':
                        self.state = "DASHBOARD"
                        self.draw()
                    elif key == 'UP' and self.active_field_idx > 0:
                        self.active_field_idx -= 1
                        self.draw()
                    elif key == 'DOWN' and self.active_field_idx < len(self.bots) - 1:
                        self.active_field_idx += 1
                        self.draw()
                    elif key == 'ENTER' and self.bots:
                        b = self.bots[self.active_field_idx]
                        if b['botid'] not in self.active_clients:
                            client = MultiRPGBot(b, self.global_queue, self)
                            self.active_clients[b['botid']] = client
                            asyncio.create_task(client.start())
                        self.state = "DASHBOARD"
                        self.draw()
                    elif key.lower() == 'l':
                        for b in self.bots:
                            if b['botid'] not in self.active_clients:
                                client = MultiRPGBot(b, self.global_queue, self)
                                self.active_clients[b['botid']] = client
                                asyncio.create_task(client.start())
                        self.state = "DASHBOARD"
                        self.draw()

                elif self.state in ["ADD_BOT", "ZNC_SETUP", "CNC_SETUP", "APP_CONFIG"]:
                    if key == 'ESC': 
                        self.state = "DASHBOARD"
                        self.draw()
                    elif key in ['UP', 'DOWN', 'LEFT', 'RIGHT', 'TAB', 'SHIFT_TAB']:
                        if key == 'UP': self.move_idx(-2 if self.state == "ADD_BOT" else -1)
                        elif key == 'DOWN': self.move_idx(2 if self.state == "ADD_BOT" else 1)
                        elif key == 'LEFT' and self.state == "ADD_BOT" and self.active_field_idx % 2 == 1: self.move_idx(-1)
                        elif key == 'RIGHT' and self.state == "ADD_BOT" and self.active_field_idx % 2 == 0: self.move_idx(1)
                        elif key == 'TAB': self.move_idx(1)
                        elif key == 'SHIFT_TAB': self.move_idx(-1)
                        self.draw()
                    elif key == 'CTRL_S': 
                        if self.state == "ADD_BOT":
                            if not self.form_data.get('usermode'): self.form_data['wait_mode'] = 0
                            if not self.form_data.get('team_name'): self.form_data['make_team'] = 0
                            if not self.form_data.get('use_znc'): self.form_data['verify_vhost'] = 0
                            
                            db_upsert_bot(self.form_data)
                            self.bots = db_get_all_bots()
                        else:
                            for k, v in self.form_data.items(): db_set_global(k, v)
                        self.state = "DASHBOARD"
                        self.draw()
                    elif key in ['SPACE', 'ENTER']:
                        active = self.form_fields[self.active_field_idx]
                        is_tick = active in ['use_znc', 'use_ssl', 'wait_mode', 'verify_vhost', 'make_team', 'auto_fight', 'auto_challenge', 'auto_bet', 'auto_slay', 'znc_ssl', 'cnc_ssl', 'cnc_enabled']
                        is_cycle = active in ["alignment", "gender", "priority"]
                        
                        if is_tick:
                            if active == "make_team" and not self.form_data['team_name']: pass
                            elif active == "make_team" and self.form_data['make_team'] == 0:
                                db_bots = db_get_all_bots()
                                conflict = any(b['team_name'] == self.form_data['team_name'] and b['make_team'] and b['botid'] != self.form_data['botid'] for b in db_bots)
                                if not conflict: self.form_data[active] = 1
                            elif active == "verify_vhost" and not self.form_data['use_znc']: pass
                            elif active == "use_znc":
                                self.form_data[active] = 0 if str(self.form_data[active]) == "1" else 1
                                if str(self.form_data[active]) == "0": self.form_data['verify_vhost'] = 0
                            else: self.form_data[active] = 0 if str(self.form_data[active]) == "1" else 1
                            self.draw()
                        elif is_cycle:
                            if active == "alignment": cycle = {"Human": "Priest", "Priest": "Undead", "Undead": "Human"}
                            elif active == "gender": cycle = {"Male": "Female", "Female": "Male"}
                            elif active == "priority": cycle = {"Engineer > Hero": "Hero > Engineer", "Hero > Engineer": "Engineer > Hero"}
                            self.form_data[active] = cycle.get(self.form_data[active], "Human" if active=="alignment" else ("Male" if active=="gender" else "Engineer > Hero"))
                            self.draw()
                        elif key == 'ENTER':
                            self.modal_active = True
                            self.modal_field = active
                            self.modal_backup_val = self.form_data[active]
                            self.draw()
        finally:
            if os.name != 'nt': termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                
# ==============================================================================
# BOOTLOADER 
# ==============================================================================

async def main():
    init_database()
    asyncio.create_task(start_identd())
    dashboard = Dashboard()
    asyncio.create_task(dashboard.global_queue.worker_loop())
    await dashboard.run_ui_loop()

if __name__ == "__main__":
    sys.stdout.write("[?1049h")
    sys.stdout.flush()
    exit_msg = "[MultiRPG] TUI shut down successfully."
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        exit_msg = "[MultiRPG] TUI abruptly stopped by user (CTRL+C)."
    except Exception as e:
        exit_msg = f"[MultiRPG] TUI encountered a fatal error: {e}"
    finally:
        sys.stdout.write("[?1049l")
        sys.stdout.write("[?25h")
        sys.stdout.flush()
        print(exit_msg)
