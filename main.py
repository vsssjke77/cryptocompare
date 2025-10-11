import asyncio
import websockets
import json
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uuid

app = FastAPI()

# Монтируем статические файлы
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Загружаем конфигурацию
with open("config.json", "r") as f:
    config = json.load(f)

# Глобальные цены со всех бирж
prices = {
    "binance": {},
    "bybit": {},
    "okx": {}
}


# Индивидуальные данные для каждого клиента
class ClientData:
    def __init__(self):
        self.tracked_coins = set(["BTCUSDT"])  # начальная монета для каждого пользователя
        self.websocket = None


clients = {}  # client_id -> ClientData

# WebSocket соединения для бирж
binance_ws = None
bybit_ws = None
okx_ws = None

# Все монеты, на которые кто-либо подписан (для оптимизации биржевых подключений)
global_tracked_coins = set(["BTCUSDT"])


async def create_binance_connection():
    """Создание соединения с Binance с глобальным списком монет"""
    global binance_ws

    if not global_tracked_coins:
        return

    streams = [f"{coin.lower()}@ticker" for coin in global_tracked_coins]
    uri = f"wss://stream.binance.com:9443/stream?streams={'/'.join(streams)}"

    try:
        if binance_ws:
            await binance_ws.close()

        binance_ws = await websockets.connect(uri)
        print(f"Connected to Binance WebSocket for {len(global_tracked_coins)} coins: {list(global_tracked_coins)}")

        asyncio.create_task(listen_binance())

    except Exception as e:
        print(f"Binance connection error: {e}")


async def listen_binance():
    global binance_ws

    if not binance_ws:
        return

    try:
        async for message in binance_ws:
            data = json.loads(message)

            if 'data' in data:
                symbol = data['data']['s']
                last_price = data['data']['c']

                prices["binance"][symbol] = last_price
                await broadcast_prices_to_all()

    except Exception as e:
        print(f"Binance listening error: {e}")
        await asyncio.sleep(5)
        await create_binance_connection()


async def listen_bybit():
    global bybit_ws
    uri = "wss://stream.bybit.com/v5/public/spot"

    async with websockets.connect(uri) as ws:
        bybit_ws = ws
        print("Connected to Bybit WebSocket")

        # Подписываемся на глобальные монеты
        if global_tracked_coins:
            subscribe_msg = {
                "op": "subscribe",
                "args": [f"tickers.{coin}" for coin in global_tracked_coins]
            }
            await ws.send(json.dumps(subscribe_msg))

        while True:
            try:
                message = await ws.recv()
                data = json.loads(message)

                if "topic" in data and data["topic"].startswith("tickers."):
                    symbol = data["topic"].split(".")[1]
                    last_price = data["data"]["lastPrice"]

                    prices["bybit"][symbol] = last_price
                    await broadcast_prices_to_all()

            except Exception as e:
                print(f"Bybit error: {e}")
                await asyncio.sleep(5)
                break


async def listen_okx():
    global okx_ws
    uri = "wss://ws.okx.com:8443/ws/v5/public"

    async with websockets.connect(uri) as ws:
        okx_ws = ws
        print("Connected to OKX WebSocket")

        # Подписываемся на глобальные монеты
        if global_tracked_coins:
            subscribe_msg = {
                "op": "subscribe",
                "args": [
                    {
                        "channel": "tickers",
                        "instId": coin.replace("USDT", "-USDT")
                    } for coin in global_tracked_coins
                ]
            }
            await ws.send(json.dumps(subscribe_msg))
            print("Subscribed to OKX tickers")

        while True:
            try:
                message = await ws.recv()
                data = json.loads(message)

                if "event" in data:
                    continue

                if "arg" in data and data["arg"]["channel"] == "tickers":
                    if "data" in data and len(data["data"]) > 0:
                        inst_id = data["arg"]["instId"].replace("-USDT", "USDT")
                        last_price = data["data"][0]["last"]

                        prices["okx"][inst_id] = last_price
                        await broadcast_prices_to_all()

            except Exception as e:
                print(f"OKX error: {e}")
                await asyncio.sleep(5)
                break


async def subscribe_to_new_coin_global(coin: str):
    """Добавить монету в глобальный список и подписаться на биржах"""
    if coin in global_tracked_coins:
        return

    global_tracked_coins.add(coin)
    print(f"Adding new coin globally: {coin}")

    # Binance - переподключаемся
    await create_binance_connection()

    # Bybit - добавляем подписку
    if bybit_ws:
        try:
            subscribe_msg = {
                "op": "subscribe",
                "args": [f"tickers.{coin}"]
            }
            await bybit_ws.send(json.dumps(subscribe_msg))
            print(f"Subscribed to Bybit for {coin}")
        except Exception as e:
            print(f"Bybit subscription error for {coin}: {e}")

    # OKX - добавляем подписку
    if okx_ws:
        try:
            subscribe_msg = {
                "op": "subscribe",
                "args": [{
                    "channel": "tickers",
                    "instId": coin.replace("USDT", "-USDT")
                }]
            }
            await okx_ws.send(json.dumps(subscribe_msg))
            print(f"Subscribed to OKX for {coin}")
        except Exception as e:
            print(f"OKX subscription error for {coin}: {e}")


def calculate_spread(bybit_price, other_price, exchange_name):
    """Рассчет спреда относительно Bybit"""
    if not bybit_price or not other_price:
        return None

    try:
        bybit_val = float(bybit_price)
        other_val = float(other_price)
        spread = other_val - bybit_val
        spread_percent = (spread / bybit_val) * 100 if bybit_val != 0 else 0

        return {
            'value': spread,
            'percent': spread_percent,
            'formatted': f"{spread:+.4f} ({spread_percent:+.2f}%)"
        }
    except (ValueError, TypeError):
        return None


async def broadcast_prices_to_client(client_id: str):
    """Отправка цен конкретному клиенту"""
    if client_id not in clients:
        return

    client_data = clients[client_id]

    # Рассчитываем спреды только для монет этого клиента
    spreads = {}
    for coin in client_data.tracked_coins:
        bybit_price = prices["bybit"].get(coin)
        binance_price = prices["binance"].get(coin)
        okx_price = prices["okx"].get(coin)

        spreads[coin] = {
            'binance': calculate_spread(bybit_price, binance_price, 'binance'),
            'okx': calculate_spread(bybit_price, okx_price, 'okx')
        }

    data = {
        "prices": prices,
        "tracked_coins": list(client_data.tracked_coins),
        "spreads": spreads,
        "available_coins": config["available_coins"]  # Добавляем доступные монеты
    }

    try:
        await client_data.websocket.send_json(data)
    except:
        # Клиент отключился, удалим его позже
        pass


async def broadcast_prices_to_all():
    """Отправка цен всем подключенным клиентам"""
    disconnected_clients = []

    for client_id in list(clients.keys()):
        try:
            await broadcast_prices_to_client(client_id)
        except:
            disconnected_clients.append(client_id)

    # Удаляем отключенных клиентов
    for client_id in disconnected_clients:
        if client_id in clients:
            # Удаляем монеты клиента из глобального списка если больше никто не отслеживает
            client_coins = clients[client_id].tracked_coins
            del clients[client_id]

            # Оптимизация: можно добавить логику для удаления неиспользуемых монет из global_tracked_coins


async def robust_listener(listener_func, name):
    """Обертка для переподключения"""
    while True:
        try:
            await listener_func()
        except Exception as e:
            print(f"{name} reconnecting in 10 seconds... Error: {e}")
            await asyncio.sleep(10)


@app.on_event("startup")
async def startup_event():
    await create_binance_connection()
    asyncio.create_task(robust_listener(listen_bybit, "Bybit"))
    asyncio.create_task(robust_listener(listen_okx, "OKX"))


@app.websocket("/ws/prices")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()

    # Создаем уникальный ID для клиента
    client_id = str(uuid.uuid4())
    client_data = ClientData()
    client_data.websocket = websocket
    clients[client_id] = client_data

    print(f"New client connected: {client_id}")

    try:
        # Отправляем начальные данные
        await broadcast_prices_to_client(client_id)

        while True:
            message = await websocket.receive_text()
            data = json.loads(message)

            if data.get("action") == "add_coin" and "coin" in data:
                coin = data["coin"]
                # Добавляем монету для этого клиента
                if coin not in client_data.tracked_coins:
                    client_data.tracked_coins.add(coin)
                    print(f"Client {client_id} added coin: {coin}")

                    # Добавляем в глобальный список если нужно
                    await subscribe_to_new_coin_global(coin)

                    # Отправляем обновленные данные клиенту
                    await broadcast_prices_to_client(client_id)

            elif data.get("action") == "remove_coin" and "coin" in data:
                coin = data["coin"]
                # Удаляем монету у клиента
                if coin in client_data.tracked_coins:
                    client_data.tracked_coins.remove(coin)
                    print(f"Client {client_id} removed coin: {coin}")

                    # Отправляем обновленные данные клиенту
                    await broadcast_prices_to_client(client_id)

            elif data.get("action") == "clear_coins":
                # Очищаем все монеты кроме BTCUSDT
                client_data.tracked_coins.clear()
                client_data.tracked_coins.add("BTCUSDT")
                print(f"Client {client_id} cleared all coins")

                # Отправляем обновленные данные клиенту
                await broadcast_prices_to_client(client_id)

    except WebSocketDisconnect:
        print(f"Client disconnected: {client_id}")
    except Exception as e:
        print(f"WebSocket error for client {client_id}: {e}")
    finally:
        if client_id in clients:
            del clients[client_id]
            print(f"Client removed: {client_id}")


@app.get("/")
async def get():
    return templates.TemplateResponse("index.html", {
        "request": {},
        "available_coins": config["available_coins"]
    })


@app.get("/available-coins")
async def get_available_coins():
    return {"available_coins": config["available_coins"]}