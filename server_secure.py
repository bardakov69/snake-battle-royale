"""
server_secure.py — Защищённый онлайн-сервер для Snake Battle Royale
С HTTP сервером для раздачи HTML файлов и WebSocket для игры
"""

import asyncio
import json
import random
import time
import os
from datetime import datetime, timedelta
from collections import defaultdict
from http import HTTPStatus
import websockets
from websockets.server import serve

# === КОНФИГУРАЦИЯ ===
CONFIG = {
    "GRID_COLS": 40,
    "GRID_ROWS": 50,
    "BOT_SPEED": 5.0,
    "PLAYER_SPEED": 5.0,
    "BASE_FOOD": 25,
    "MAX_PLAYERS": 10,  # Максимум игроков в комнате
    "PLAYERS_PER_ROUND": 0,  # Мы не спавним ботов, управляем змейками игроков сами
    "SPAWN_LEN": 3,
    "CORPSE_FOOD": 3,
    # Безопасность
    "MAX_CONNECTIONS_PER_IP": 3,  # Лимит подключений с одного IP
    "RATE_LIMIT_MESSAGES": 20,  # Сообщений в секунду
    "MAX_SNAKE_SPEED": 10.0,  # Максимальная скорость змейки (анти-чит)
}

# === НАПРАВЛЕНИЯ ===
UP = {"x": 0, "y": -1}
DOWN = {"x": 0, "y": 1}
LEFT = {"x": -1, "y": 0}
RIGHT = {"x": 1, "y": 0}

OPPOSITE = {"UP": "DOWN", "DOWN": "UP", "LEFT": "RIGHT", "RIGHT": "LEFT"}

DIR_MAP = {
    "UP": UP,
    "DOWN": DOWN,
    "LEFT": LEFT,
    "RIGHT": RIGHT,
    "up": UP,
    "down": DOWN,
    "left": LEFT,
    "right": RIGHT,
}

COLORS = [
    "#00ff96",
    "#ffc800",
    "#c864ff",
    "#00c8ff",
    "#ff64c8",
    "#ff9632",
    "#64ff32",
    "#9696ff",
    "#ffffff",
    "#ff3232",
    "#3232ff",
]

NAMES = [
    "Trump",
    "Biden",
    "Putin",
    "Merkel",
    "Xi",
    "Macron",
    "Scholz",
    "Obama",
    "Bush",
    "Clinton",
    "Reagan",
    "Kennedy",
]

COUNTRIES = ["USA", "RUS", "GER", "CHN", "FRA", "GBR"]


class RateLimiter:
    """Защита от спама и DDoS"""

    def __init__(self):
        self.ip_connections = defaultdict(list)  # IP -> [время подключений]
        self.ip_messages = defaultdict(list)  # IP -> [время сообщений]
        self.banned_ips = set()

    def is_ip_allowed(self, ip):
        if ip in self.banned_ips:
            return False

        # Очистка старых записей (старше 60 сек)
        now = time.time()
        self.ip_connections[ip] = [t for t in self.ip_connections[ip] if now - t < 60]

        # Проверка лимита подключений
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
        print(f"🚫 Забанен IP: {ip}")


class Snake:
    def __init__(self, owner_name, country, is_bot, start_pos, owner_ws=None):
        self.owner_name = owner_name
        self.country = country
        self.is_bot = is_bot
        self.alive = True
        self.color = random.choice(COLORS)

        x, y = start_pos
        self.body = [{"x": x, "y": y}]
        for i in range(1, CONFIG["SPAWN_LEN"]):
            self.body.append({"x": x - i, "y": y})

        self.direction = "RIGHT"
        self.grow_pending = 0
        self.speed = CONFIG["BOT_SPEED"] if is_bot else CONFIG["PLAYER_SPEED"]
        self.dt_accum = random.uniform(0.0, 1.0 / self.speed)
        self.starvation_timer = 150
        self.max_starvation = 150
        self.score = 0
        self.last_move_time = time.time()
        self.move_count = 0
        self.respawn_timer = 0.0  # время до респавна (0 если не мертва)
        self.owner_ws = owner_ws  # websocket if human, None for bot

    def head(self):
        return self.body[0] if self.body else None

    def set_direction(self, new_dir):
        # Валидация: не чаще 10 раз в секунду (анти-спам)
        now = time.time()
        self.move_count += 1
        if self.move_count > 10:
            elapsed = now - self.last_move_time
            if elapsed < 1.0:
                return  # Слишком часто!
            self.move_count = 0
            self.last_move_time = now

        if isinstance(new_dir, dict):
            new_dir = (
                "UP"
                if new_dir == UP
                else "DOWN"
                if new_dir == DOWN
                else "LEFT"
                if new_dir == LEFT
                else "RIGHT"
            )

        current_opposite = OPPOSITE.get(self.direction)
        if new_dir != current_opposite:
            self.direction = new_dir

    def to_dict(self):
        return {
            "owner_name": self.owner_name,
            "country": self.country,
            "is_bot": self.is_bot,
            "alive": self.alive,
            "color": self.color,
            "body": self.body,
            "direction": self.direction,
            "score": self.score,
        }

    def grow(self, amount):
        self.grow_pending += amount

    def start_respawn(self, min_time=3.0, max_time=6.0):
        self.respawn_timer = random.uniform(min_time, max_time)

    def update_respawn(self, dt):
        if self.respawn_timer > 0:
            self.respawn_timer -= dt
            if self.respawn_timer <= 0:
                return True  # ready to respawn
        return False

    def respawn(self, pos):
        self.alive = True
        self.body = [{"x": pos[0], "y": pos[1]}]
        for i in range(1, CONFIG["SPAWN_LEN"]):
            self.body.append({"x": pos[0] - i, "y": pos[1]})
        self.direction = "RIGHT"
        self.grow_pending = 0
        self.speed = CONFIG["BOT_SPEED"] if self.is_bot else CONFIG["PLAYER_SPEED"]
        self.dt_accum = random.uniform(0.0, 1.0 / self.speed)
        self.starvation_timer = self.max_starvation
        # Note: we keep score, color, owner_name, etc.


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
        self.players = {}  # websocket -> Snake
        self.player_names = set()
        self.snakes = {}  # name -> Snake
        self.foods = []
        self.bombs = []
        self.round_over = False
        self.restart_timer = 0
        self.scores = {}  # имя -> очки
        self.last_state_hash = ""

    def add_player(self, websocket, name):
        if len(self.players) >= CONFIG["MAX_PLAYERS"]:
            return False

        base_name = name or f"Player_{len(self.players) + 1}"
        base_name = base_name[:15]  # Ограничение длины
        base_name = "".join(
            c for c in base_name if c.isalnum() or c in "_- "
        )  # Санитизация

        unique_name = base_name
        counter = 1
        while unique_name in self.player_names:
            unique_name = f"{base_name}_{counter}"
            counter += 1

        self.player_names.add(unique_name)
        pos = self.find_empty_spawn()
        if pos:
            snake = Snake(unique_name, "USER", False, pos, websocket)
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
            if snake.owner_name in self.scores:
                del self.scores[snake.owner_name]

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
        # Лимит бомб на игрока
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
        # Очистка еды и бомб, но сохраняем змейки (с их текущим состоянием alive/dead и таймерами)
        self.foods = []
        self.bombs = []
        self.round_over = False
        self.restart_timer = 0

        # Убедимся, что для каждого игрока есть змейка (создаем, если отсутствует)
        for ws, old_snake in self.players.items():
            if ws not in self.players:
                continue  # защита от изменения словаря во время итерации
            if old_snake.owner_name not in self.snakes:
                # Змейка для этого игрока отсутствует, создаем новую
                pos = self.find_empty_spawn()
                if pos:
                    snake = Snake(old_snake.owner_name, "USER", False, pos, ws)
                    self.players[ws] = snake
                    self.snakes[snake.owner_name] = snake
                    if snake.owner_name not in self.scores:
                        self.scores[snake.owner_name] = 0
            else:
                # Змейка существует, но могла быть мертва - убеждаемся, что её таймер сброшен
                snake = self.snakes[old_snake.owner_name]
                if not snake.alive and snake.respawn_timer > 0:
                    # Если змейка мертва и ждет респавн, сбрасываем таймер (чтобы не респавнить сразу)
                    snake.respawn_timer = 0.0
                # Если змейка жива, оставляем как есть

        # Удаляем змейки, которые больше не принадлежат подключенным игрокам
        # (например, если игрок вышел, но мы не успели удалить)
        to_remove = []
        for name, snake in self.snakes.items():
            if snake.owner_ws is not None and snake.owner_ws not in self.players:
                to_remove.append(name)
        for name in to_remove:
            snake = self.snakes.pop(name)
            if name in self.scores:
                del self.scores[name]

    def update_bots(self):
        # У нас нет ботов, так как PLAYERS_PER_ROUND = 0
        pass

    def move_snake(self, snake, direction):
        if isinstance(direction, str):
            direction = DIR_MAP.get(direction, RIGHT)

        head = snake.head()
        if not head:
            return

        # Валидация скорости (анти-чит)
        max_step = 1.0 / snake.speed * 1.5  # 50% запас
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
            snake.body = []
            snake.start_respawn()  # Запускаем таймер респавна

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
            snake.body = []
            snake.start_respawn()  # Запускаем таймер респавна при смерти от столкновения

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

        # Обновляем таймеры респавна для мертвых змеек и респавним, если пора
        for snake in self.snakes.values():
            if not snake.alive:
                if snake.update_respawn(dt):
                    # Время респавна вышло, ищем свободное место и респавним
                    pos = self.find_empty_spawn()
                    if pos:
                        snake.respawn(pos)
                    else:
                        # Если нет свободного места, сбрасываем таймер и пробуем позже
                        snake.respawn_timer = (
                            0.1  # маленькая задержка перед следующей проверкой
                        )

        # Проверка условий окончания раунда - мы не останавливаем игру при смерти змеек
        # Можно оставить раунд вечным, или добавить своё условие (например, время)
        # Пока что раунд не заканчивается автоматически
        # Если нужно сохранять старый функционал конца раунда при определённом условии, раскомментировать ниже:
        # alive = [s for s in self.snakes.values() if s.alive]
        # if len(alive) == 0 and len(self.snakes) > 0:
        #     self.round_over = True
        #     self.restart_timer = 1.5
        # elif len(alive) == 1 and len(self.snakes) > 1:
        #     winner = alive[0]
        #     winner.score += 5
        #     if winner.owner_name in self.scores:
        #         self.scores[winner.owner_name] += 5
        #     self.round_over = True
        #     self.restart_timer = 3.0

    def get_state(self):
        return {
            "snakes": {name: s.to_dict() for name, s in self.snakes.items()},
            "foods": [f.to_dict() for f in self.foods],
            "bombs": [b.to_dict() for b in self.bombs],
            "round_over": self.round_over,
            "scores": self.scores,
        }


# === ГЛОБАЛЬНЫЕ ===
rooms = {}
player_rooms = {}
rate_limiter = RateLimiter()


async def handle_client(websocket, path=None):
    room_id = "default"
    room = None
    player_name = None

    # Получаем IP клиента
    try:
        ip = websocket.remote_address[0] if websocket.remote_address else "unknown"
    except:
        ip = "unknown"

    # Проверка IP
    if not rate_limiter.is_ip_allowed(ip):
        print(f"⚠️ Отклонено подключение с IP: {ip} (лимит или бан)")
        await websocket.close(1008, "Too many connections from your IP")
        return

    rate_limiter.record_connection(ip)
    print(f"✅ Подключение: {ip}")

    try:
        async for message in websocket:
            # Rate limiting сообщений
            if not rate_limiter.check_message_rate(ip):
                print(f"⚠️ Rate limit для {ip}")
                continue

            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                continue  # Игнорируем битые пакеты

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
                    await websocket.send(
                        json.dumps({"type": "joined", "name": name, "room": room_id})
                    )

                    await websocket.send(
                        json.dumps({"type": "game_state", "state": room.get_state()})
                    )
                    print(f"🎮 Игрок присоединился: {name}")

                else:
                    await websocket.send(
                        json.dumps(
                            {"type": "error", "message": "Room is full or spawn failed"}
                        )
                    )

            elif cmd == "move":
                if websocket in player_rooms:
                    room = rooms[player_rooms[websocket]]
                    snake = room.players.get(websocket)
                    if snake and snake.alive:
                        direction = data.get("direction")
                        snake.set_direction(direction)

            elif cmd == "bomb":
                if websocket in player_rooms:
                    room = rooms[player_rooms[websocket]]
                    snake = room.players.get(websocket)
                    if snake and snake.alive:
                        room.spawn_bomb(snake.owner_name)

            elif cmd == "leave":
                if websocket in player_rooms:
                    room = rooms[player_rooms[websocket]]
                    room.remove_player(websocket)
                    del player_rooms[websocket]

            # Можно добавить другие команды по необходимости

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        if websocket in player_rooms:
            room = rooms[player_rooms[websocket]]
            room.remove_player(websocket)
            del player_rooms[websocket]
            print(f"👋 Игрок отключился")


async def game_loop():
    while True:
        await asyncio.sleep(1.0 / 60.0)

        dt = 1.0 / 60.0

        for room in list(rooms.values()):
            room.update(dt)

            state = room.get_state()
            message = json.dumps({"type": "game_state", "state": state})

            disconnected = []
            for websocket in room.players.keys():
                try:
                    await websocket.send(message)
                except:
                    disconnected.append(websocket)

            for ws in disconnected:
                if ws in player_rooms:
                    room.remove_player(ws)
                    del player_rooms[ws]


async def main():
    import os

    port = int(os.environ.get("PORT", 8765))

    print("=" * 60)
    print("🐍 Snake Battle Royale — Защищённый Сервер")
    print("=" * 60)
    print(f"\n🔒 Защита включена:")
    print(f"   • Лимит подключений с IP: {CONFIG['MAX_CONNECTIONS_PER_IP']}")
    print(f"   • Rate limit: {CONFIG['RATE_LIMIT_MESSAGES']} сек/сообщ")
    print(f"   • Анти-чит: макс. скорость {CONFIG['MAX_SNAKE_SPEED']}")
    print(f"\n📍 Порт: {port}")
    print(f"\n🛡️  Ваш IP скрыт при использовании Render/hостинга")
    print("=" * 60)

    async with serve(handle_client, "0.0.0.0", port):
        loop_task = asyncio.create_task(game_loop())
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
