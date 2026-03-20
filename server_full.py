"""
server_full.py — Полный сервер: HTTP + WebSocket
Раздаёт HTML файлы и обрабатывает игру
"""
import asyncio
import json
import random
import time
import os
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from http.server import HTTPServer, SimpleHTTPRequestHandler
import threading
import websockets
from websockets.server import serve

# === КОНФИГУРАЦИЯ ===
CONFIG = {
    "GRID_COLS": 40,
    "GRID_ROWS": 50,
    "BOT_SPEED": 5.0,
    "PLAYER_SPEED": 5.0,
    "BASE_FOOD": 25,
    "MAX_PLAYERS": 10,
    "PLAYERS_PER_ROUND": 3,
    "SPAWN_LEN": 3,
    "CORPSE_FOOD": 3,
    "HTTP_PORT": 8080,
    "WS_PORT": 8765,
    
    # Безопасность
    "MAX_CONNECTIONS_PER_IP": 3,
    "RATE_LIMIT_MESSAGES": 20,
    "MAX_SNAKE_SPEED": 10.0,
}

# === НАПРАВЛЕНИЯ ===
UP = {"x": 0, "y": -1}
DOWN = {"x": 0, "y": 1}
LEFT = {"x": -1, "y": 0}
RIGHT = {"x": 1, "y": 0}

OPPOSITE = {"UP": "DOWN", "DOWN": "UP", "LEFT": "RIGHT", "RIGHT": "LEFT"}

DIR_MAP = {
    "UP": UP, "DOWN": DOWN, "LEFT": LEFT, "RIGHT": RIGHT,
    "up": UP, "down": DOWN, "left": LEFT, "right": RIGHT
}

COLORS = [
    "#00ff96", "#ffc800", "#c864ff", "#00c8ff", "#ff64c8",
    "#ff9632", "#64ff32", "#9696ff", "#ffffff", "#ff3232", "#3232ff"
]

NAMES = ["Trump", "Biden", "Putin", "Merkel", "Xi", "Macron", "Scholz", "Obama", "Bush", "Clinton", "Reagan", "Kennedy"]
COUNTRIES = ["USA", "RUS", "GER", "CHN", "FRA", "GBR"]


class RateLimiter:
    def __init__(self):
        self.ip_connections = defaultdict(list)
        self.ip_messages = defaultdict(list)
        self.banned_ips = set()
    
    def is_ip_allowed(self, ip):
        if ip in self.banned_ips:
            return False
        now = time.time()
        self.ip_connections[ip] = [t for t in self.ip_connections[ip] if now - t < 60]
        if len(self.ip_connections[ip]) >= CONFIG["MAX_CONNECTIONS_PER_IP"]:
            return False
        return True
    
    def record_connection(self, ip):
        self.ip_connections[ip].append(time.time())
    
    def check_message_rate(self, ip):
        now = time.time()
        self.ip_messages[ip] = [t for t in self.ip_messages[ip] if now - t < 1]
        if len(self.ip_messages[ip]) >= CONFIG["RATE_LIMIT_MESSAGES"]:
            return False
        self.ip_messages[ip].append(now)
        return True
    
    def ban_ip(self, ip):
        self.banned_ips.add(ip)


class Snake:
    def __init__(self, owner_name, country, is_bot, start_pos):
        self.owner_name = owner_name
        self.country = country
        self.is_bot = is_bot
        self.alive = True
        self.color = random.choice(COLORS)
        x, y = start_pos
        self.body = [{"x": x, "y": y}]
        for i in range(1, CONFIG["SPAWN_LEN"]):
            self.body.append({"x": x - i, "y": y})
        self.direction = RIGHT
        self.grow_pending = 0
        self.speed = CONFIG["BOT_SPEED"] if is_bot else CONFIG["PLAYER_SPEED"]
        self.dt_accum = random.uniform(0.0, 1.0 / self.speed)
        self.starvation_timer = 150
        self.max_starvation = 150
        self.score = 0
        self.last_move_time = time.time()
        self.move_count = 0
    
    def head(self):
        return self.body[0] if self.body else None
    
    def set_direction(self, new_dir):
        now = time.time()
        self.move_count += 1
        if self.move_count > 10:
            if now - self.last_move_time < 1.0:
                return
            self.move_count = 0
            self.last_move_time = now
        
        if isinstance(new_dir, dict):
            new_dir = "UP" if new_dir == UP else "DOWN" if new_dir == DOWN else "LEFT" if new_dir == LEFT else "RIGHT"
        
        if new_dir != OPPOSITE.get(self.direction):
            self.direction = new_dir
    
    def to_dict(self):
        return {
            "owner_name": self.owner_name, "country": self.country, "is_bot": self.is_bot,
            "alive": self.alive, "color": self.color, "body": self.body,
            "direction": self.direction, "score": self.score
        }


class Food:
    def __init__(self, pos, is_star=False):
        self.pos = pos
        self.is_star = is_star
    def to_dict(self):
        return {"pos": self.pos, "is_star": self.is_star}


class Bomb:
    def __init__(self, owner_name, pos):
        self.owner_name = owner_name
        self.pos = pos
    def to_dict(self):
        return {"owner_name": self.owner_name, "pos": self.pos}


class GameRoom:
    def __init__(self, room_id):
        self.room_id = room_id
        self.players = {}
        self.player_names = set()
        self.snakes = {}
        self.foods = []
        self.bombs = []
        self.round_over = False
        self.restart_timer = 0
        self.scores = {}
    
    def add_player(self, websocket, name):
        if len(self.players) >= CONFIG["MAX_PLAYERS"]:
            return False
        base_name = (name or f"Player_{len(self.players) + 1}")[:15]
        base_name = "".join(c for c in base_name if c.isalnum() or c in "_- ")
        unique_name = base_name
        counter = 1
        while unique_name in self.player_names:
            unique_name = f"{base_name}_{counter}"
            counter += 1
        self.player_names.add(unique_name)
        pos = self.find_empty_spawn()
        if pos:
            snake = Snake(unique_name, "USER", False, pos)
            self.players[websocket] = snake
            self.snakes[unique_name] = snake
            self.scores[unique_name] = 0
            return unique_name
        return None
    
    def remove_player(self, websocket):
        if websocket in self.players:
            snake = self.players[websocket]
            snake.alive = False
            del self.players[websocket]
            if snake.owner_name in self.snakes:
                del self.snakes[snake.owner_name]
            self.player_names.discard(snake.owner_name)
    
    def find_empty_spawn(self):
        occupied = set()
        for snake in self.snakes.values():
            if snake.alive:
                for segment in snake.body:
                    occupied.add((segment["x"], segment["y"]))
        for _ in range(50):
            x = random.randint(3, CONFIG["GRID_COLS"] - 4)
            y = random.randint(3, CONFIG["GRID_ROWS"] - 4)
            safe = True
            for dx in range(-1, 2):
                for dy in range(-1, 2):
                    if (x + dx, y + dy) in occupied:
                        safe = False
                        break
                if not safe:
                    break
            if safe:
                return (x, y)
        return None
    
    def spawn_food(self):
        occupied = set()
        for snake in self.snakes.values():
            if snake.alive:
                for segment in snake.body:
                    occupied.add((segment["x"], segment["y"]))
        for food in self.foods:
            occupied.add((food.pos["x"], food.pos["y"]))
        for _ in range(50):
            x = random.randint(0, CONFIG["GRID_COLS"] - 1)
            y = random.randint(0, CONFIG["GRID_ROWS"] - 1)
            if (x, y) not in occupied:
                self.foods.append(Food({"x": x, "y": y}, False))
                return
    
    def spawn_bomb(self, owner_name):
        bomb_count = sum(1 for b in self.bombs if b.owner_name == owner_name)
        if bomb_count >= 3:
            return
        occupied = set()
        for snake in self.snakes.values():
            if snake.alive:
                for segment in snake.body:
                    occupied.add((segment["x"], segment["y"]))
        for food in self.foods:
            occupied.add((food.pos["x"], food.pos["y"]))
        for bomb in self.bombs:
            occupied.add((bomb.pos["x"], bomb.pos["y"]))
        if len(self.bombs) >= 20:
            return
        for _ in range(50):
            x = random.randint(1, CONFIG["GRID_COLS"] - 2)
            y = random.randint(1, CONFIG["GRID_ROWS"] - 2)
            if (x, y) not in occupied:
                self.bombs.append(Bomb(owner_name, {"x": x, "y": y}))
                return
    
    def start_round(self):
        self.snakes = {}
        self.foods = []
        self.bombs = []
        self.round_over = False
        self.restart_timer = 0
        for ws, old_snake in self.players.items():
            pos = self.find_empty_spawn()
            if pos:
                snake = Snake(old_snake.owner_name, "USER", False, pos)
                snake.score = old_snake.score
                self.players[ws] = snake
                self.snakes[snake.owner_name] = snake
        while len(self.snakes) < CONFIG["PLAYERS_PER_ROUND"]:
            name = random.choice(NAMES)
            counter = 1
            while name in self.snakes:
                name = f"{random.choice(NAMES)}_{counter}"
                counter += 1
            pos = self.find_empty_spawn()
            if pos:
                self.snakes[name] = Snake(name, random.choice(COUNTRIES), True, pos)
        while len(self.foods) < CONFIG["BASE_FOOD"]:
            self.spawn_food()
    
    def update_bots(self):
        dangers = set()
        for snake in self.snakes.values():
            if snake.alive:
                for segment in snake.body:
                    dangers.add((segment["x"], segment["y"]))
        for bomb in self.bombs:
            dangers.add((bomb.pos["x"], bomb.pos["y"]))
        for snake in self.snakes.values():
            if not snake.alive or not snake.is_bot:
                continue
            if random.random() < 0.15:
                self.bot_find_path(snake, dangers)
    
    def bot_find_path(self, bot, dangers):
        head = bot.head()
        if not head:
            return
        viable = []
        for dir_name, dir_vec in [("UP", UP), ("DOWN", DOWN), ("LEFT", LEFT), ("RIGHT", RIGHT)]:
            if bot.direction != OPPOSITE.get(dir_name):
                nx = (head["x"] + dir_vec["x"]) % CONFIG["GRID_COLS"]
                ny = (head["y"] + dir_vec["y"]) % CONFIG["GRID_ROWS"]
                if (nx, ny) not in dangers:
                    viable.append((dir_name, dir_vec))
        if not viable:
            return
        nearest = None
        min_dist = float('inf')
        for food in self.foods:
            dist = abs(food.pos["x"] - head["x"]) + abs(food.pos["y"] - head["y"])
            if dist < min_dist:
                min_dist = dist
                nearest = food.pos
        if nearest:
            possible = []
            if nearest["x"] > head["x"]:
                possible.append(("RIGHT", RIGHT))
            elif nearest["x"] < head["x"]:
                possible.append(("LEFT", LEFT))
            if nearest["y"] > head["y"]:
                possible.append(("DOWN", DOWN))
            elif nearest["y"] < head["y"]:
                possible.append(("UP", UP))
            safe_possible = [p for p in possible if any(p[0] == v[0] for v in viable)]
            if safe_possible:
                bot.direction = random.choice(safe_possible)[0]
                return
        bot.direction = random.choice(viable)[0]
    
    def move_snake(self, snake, direction):
        if isinstance(direction, str):
            direction = DIR_MAP.get(direction, RIGHT)
        head = snake.head()
        if not head:
            return
        max_step = 1.0 / snake.speed * 1.5
        if snake.dt_accum > max_step:
            snake.dt_accum = max_step
        nx = (head["x"] + direction["x"]) % CONFIG["GRID_COLS"]
        ny = (head["y"] + direction["y"]) % CONFIG["GRID_ROWS"]
        snake.body.insert(0, {"x": nx, "y": ny})
        if snake.grow_pending > 0:
            snake.grow_pending -= 1
        else:
            snake.body.pop()
        snake.starvation_timer -= 1
        if snake.starvation_timer <= 0:
            snake.alive = False
            for i, pos in enumerate(snake.body):
                if i % (CONFIG["CORPSE_FOOD"] * 2) == 0:
                    self.foods.append(Food(pos, False))
            snake.body = []
    
    def resolve_collisions(self, snakes_moved):
        occupied = {}
        for snake in self.snakes.values():
            if snake.alive:
                for i, segment in enumerate(snake.body):
                    key = (segment["x"], segment["y"])
                    if key not in occupied:
                        occupied[key] = []
                    occupied[key].append({"snake": snake, "is_head": i == 0})
        dead = set()
        for snake in snakes_moved:
            if not snake.alive:
                continue
            head = snake.head()
            if not head:
                continue
            key = (head["x"], head["y"])
            hits = occupied.get(key, [])
            for hit in hits:
                if hit["snake"] == snake and hit["is_head"]:
                    continue
                dead.add(snake)
                if hit["is_head"] and hit["snake"] != snake:
                    dead.add(hit["snake"])
            for i, bomb in enumerate(self.bombs):
                if bomb.pos["x"] == head["x"] and bomb.pos["y"] == head["y"]:
                    dead.add(snake)
                    self.bombs.pop(i)
                    break
        for snake in dead:
            if not snake.alive:
                continue
            snake.alive = False
            for i, pos in enumerate(snake.body):
                if i % CONFIG["CORPSE_FOOD"] == 0:
                    self.foods.append(Food(pos, True))
            snake.body = []
        food_map = {(f.pos["x"], f.pos["y"]): i for i, f in enumerate(self.foods)}
        for snake in snakes_moved:
            if snake in dead or not snake.alive or not snake.body:
                continue
            head = snake.head()
            key = (head["x"], head["y"])
            if key in food_map:
                food = self.foods[food_map[key]]
                snake.grow(2 if food.is_star else 1)
                snake.starvation_timer = snake.max_starvation
                self.foods.pop(food_map[key])
                del food_map[key]
                snake.score += 1
        while len(self.foods) < CONFIG["BASE_FOOD"]:
            self.spawn_food()
    
    def update(self, dt):
        if self.round_over:
            self.restart_timer -= dt
            if self.restart_timer <= 0:
                self.start_round()
            return
        self.update_bots()
        alive_snakes = [s for s in self.snakes.values() if s.alive]
        snakes_moved = set()
        for snake in alive_snakes:
            snake.dt_accum += dt
            step_interval = 1.0 / snake.speed
            if snake.dt_accum > step_interval * 10:
                snake.dt_accum = step_interval * 10
            while snake.dt_accum >= step_interval:
                snake.dt_accum -= step_interval
                snakes_moved.add(snake)
                self.move_snake(snake, DIR_MAP.get(snake.direction, RIGHT))
        if snakes_moved:
            self.resolve_collisions(snakes_moved)
        alive = [s for s in self.snakes.values() if s.alive]
        if len(alive) == 1 and len(self.snakes) > 1:
            winner = alive[0]
            winner.score += 5
            if winner.owner_name in self.scores:
                self.scores[winner.owner_name] += 5
            self.round_over = True
            self.restart_timer = 3.0
        elif len(alive) == 0 and len(self.snakes) > 0:
            self.round_over = True
            self.restart_timer = 1.5
    
    def get_state(self):
        return {
            "snakes": {name: s.to_dict() for name, s in self.snakes.items()},
            "foods": [f.to_dict() for f in self.foods],
            "bombs": [b.to_dict() for b in self.bombs],
            "round_over": self.round_over,
            "scores": self.scores
        }


# === ГЛОБАЛЬНЫЕ ===
rooms = {}
player_rooms = {}
rate_limiter = RateLimiter()


# === HTTP СЕРВЕР ===
class GameHTTPHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        # Перенаправляем корень на online.html
        if self.path == '/':
            self.path = '/online.html'
        return super().do_GET()
    
    def log_message(self, format, *args):
        print(f"[HTTP] {args[0]}")


def run_http_server():
    """Запускает HTTP сервер в отдельном потоке"""
    server_address = ('', CONFIG["HTTP_PORT"])
    httpd = HTTPServer(server_address, GameHTTPHandler)
    print(f"🌐 HTTP сервер запущен: http://localhost:{CONFIG['HTTP_PORT']}")
    httpd.serve_forever()


# === WEBSOCKET ОБРАБОТЧИК ===
async def handle_client(websocket):
    room_id = "default"
    room = None
    
    try:
        ip = websocket.remote_address[0] if websocket.remote_address else "unknown"
    except:
        ip = "unknown"
    
    if not rate_limiter.is_ip_allowed(ip):
        await websocket.close(1008, "Too many connections")
        return
    
    rate_limiter.record_connection(ip)
    print(f"✅ WS: {ip}")
    
    try:
        async for message in websocket:
            if not rate_limiter.check_message_rate(ip):
                continue
            
            try:
                data = json.loads(message)
            except:
                continue
            
            cmd = data.get("command")
            
            if cmd == "join":
                room_id = data.get("room", "default")
                player_name = data.get("name", "Anonymous")
                if room_id not in rooms:
                    rooms[room_id] = GameRoom(room_id)
                room = rooms[room_id]
                name = room.add_player(websocket, player_name)
                if name:
                    player_rooms[websocket] = room_id
                    await websocket.send(json.dumps({"type": "joined", "name": name, "room": room_id}))
                    await websocket.send(json.dumps({"type": "game_state", "state": room.get_state()}))
                    print(f"🎮 Игрок: {name}")
            
            elif cmd == "move":
                if websocket in player_rooms:
                    room = rooms[player_rooms[websocket]]
                    snake = room.players.get(websocket)
                    if snake and snake.alive:
                        snake.set_direction(data.get("direction"))
            
            elif cmd == "bomb":
                if websocket in player_rooms:
                    room = rooms[player_rooms[websocket]]
                    snake = room.players.get(websocket)
                    if snake and snake.alive:
                        room.spawn_bomb(snake.owner_name)
    
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        if websocket in player_rooms:
            room = rooms[player_rooms[websocket]]
            room.remove_player(websocket)
            del player_rooms[websocket]


async def game_loop():
    while True:
        await asyncio.sleep(1.0 / 60.0)
        dt = 1.0 / 60.0
        for room in list(rooms.values()):
            room.update(dt)
            state = room.get_state()
            message = json.dumps({"type": "game_state", "state": state})
            disconnected = []
            for ws in room.players.keys():
                try:
                    await ws.send(message)
                except:
                    disconnected.append(ws)
            for ws in disconnected:
                if ws in player_rooms:
                    room.remove_player(ws)
                    del player_rooms[ws]


async def main():
    print("=" * 60)
    print("🐍 Snake Battle Royale — Полный Сервер (HTTP + WS)")
    print("=" * 60)
    print(f"\n🌐 HTTP: http://localhost:{CONFIG['HTTP_PORT']}")
    print(f"🔌 WebSocket: ws://localhost:{CONFIG['WS_PORT']}")
    print(f"\n🔒 Защита включена")
    print("=" * 60)
    
    # Запуск HTTP сервера в потоке
    http_thread = threading.Thread(target=run_http_server, daemon=True)
    http_thread.start()
    
    # Запуск WebSocket сервера
    async with serve(handle_client, "0.0.0.0", CONFIG["WS_PORT"]):
        loop_task = asyncio.create_task(game_loop())
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
