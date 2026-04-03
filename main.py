import logging
import os
import asyncio
import time
from typing import Dict, Tuple

from fastapi import FastAPI, Request
from telegram import Update, Bot, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from geopy.geocoders import Nominatim
from dotenv import load_dotenv
from httpx import AsyncClient, Timeout, RequestError

# ====================== CONFIG ======================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", 0))
OPENWEATHER_TOKEN = os.getenv("OPENWEATHER_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

assert BOT_TOKEN, "❌ BOT_TOKEN не установлен!"
assert OPENWEATHER_TOKEN, "❌ OPENWEATHER_TOKEN не установлен!"

WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"

# ====================== LOGGING ======================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ====================== APP ======================
app = FastAPI(title="Weather Bot ULTRA")
bot = Bot(token=BOT_TOKEN)
bot_app = ApplicationBuilder().token(BOT_TOKEN).build()

# ====================== SERVICES ======================
geolocator = Nominatim(user_agent="weather-bot")
http_client = AsyncClient(timeout=Timeout(10.0))

user_locations: Dict[int, Tuple[float, float]] = {}
last_request: Dict[int, float] = {}   # Анти-спам


def is_spam(user_id: int) -> bool:
    """Простая защита от спама (2 секунды между запросами)"""
    now = time.time()
    if user_id in last_request and now - last_request[user_id] < 2:
        return True
    last_request[user_id] = now
    return False


async def safe_request(url: str, params: dict, retries: int = 3):
    """Улучшенный запрос с умным retry"""
    for attempt in range(retries):
        try:
            response = await http_client.get(url, params=params)
            
            if response.status_code == 200:
                return response.json()
            elif response.status_code == 429:          # Rate limit
                await asyncio.sleep(2)
            elif response.status_code >= 500:          # Серверные ошибки
                await asyncio.sleep(1 * (attempt + 1))
            else:
                logger.warning(f"HTTP {response.status_code}")
                break
                
        except RequestError as e:
            logger.warning(f"Request error (attempt {attempt+1}): {e}")
            await asyncio.sleep(1 * (attempt + 1))
    return None


# ====================== CORE FUNCTIONS ======================
def get_address(lat: float, lon: float) -> str:
    try:
        location = geolocator.reverse((lat, lon), language="ru")
        return location.address if location else "Адрес не найден"
    except Exception as e:
        logger.error(f"Geocode error: {e}")
        return "Ошибка определения адреса"


async def get_weather(lat: float, lon: float) -> str:
    data = await safe_request(
        "https://api.openweathermap.org/data/2.5/weather",
        {"lat": lat, "lon": lon, "appid": OPENWEATHER_TOKEN, "units": "metric", "lang": "ru"},
    )
    if not data or "main" not in data:
        return "❌ Не удалось получить погоду"
    return (
        f"🌤 {data['weather'][0]['description'].capitalize()}\n"
        f"🌡 {data['main']['temp']}°C (ощущается {data['main']['feels_like']}°C)\n"
        f"💧 Влажность: {data['main']['humidity']}%\n"
        f"💨 Ветер: {data['wind']['speed']} м/с"
    )


async def get_forecast(lat: float, lon: float) -> str:
    data = await safe_request(
        "https://api.openweathermap.org/data/2.5/forecast",
        {"lat": lat, "lon": lon, "appid": OPENWEATHER_TOKEN, "units": "metric", "lang": "ru"},
    )
    if not data or "list" not in data:
        return "❌ Не удалось получить прогноз"
    lines = ["📅 Прогноз на ближайшие часы:"]
    for item in data["list"][:7]:
        lines.append(
            f"🕓 {item['dt_txt']} — {item['weather'][0]['description'].capitalize()}, "
            f"🌡 {item['main']['temp']}°C, 💨 {item['wind']['speed']} м/с"
        )
    return "\n".join(lines)


# ====================== HANDLERS ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[KeyboardButton("📍 Отправить геолокацию", request_location=True)]]
    await update.message.reply_text(
        "Нажми кнопку и получи погоду 👇",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
    )
    if OWNER_ID:
        user = update.message.from_user
        await context.bot.send_message(
            chat_id=OWNER_ID,
            text=f"👤 @{user.username or user.first_name} (ID: {user.id}) запустил бота",
        )


async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Анти-спам временно убран для теста
    try:
        user = update.message.from_user
        loc = update.message.location
        lat, lon = loc.latitude, loc.longitude

        user_locations[user.id] = (lat, lon)

        # Параллельные запросы к geolocator и API
        loop = asyncio.get_running_loop()
        address_task = loop.run_in_executor(None, get_address, lat, lon)
        
        weather, forecast, address = await asyncio.gather(
            get_weather(lat, lon),
            get_forecast(lat, lon),
            address_task
        )

        map_url = f"https://static-maps.yandex.ru/1.x/?ll={lon},{lat}&size=450,300&z=14&l=map&pt={lon},{lat},pm2rdm"
        
        caption = (
            f"📍 {address}\n"
            f"{'─'*25}\n\n"
            f"{weather}\n\n"
            f"{'─'*25}\n"
            f"{forecast}"
        )

        await update.message.reply_photo(photo=map_url, caption=caption)

        if OWNER_ID:
            await context.bot.send_photo(
                chat_id=OWNER_ID,
                photo=map_url,
                caption=f"👤 @{user.username or user.first_name}\n📍 {address}\n\n{weather}\n\n{forecast}",
            )

    except Exception as e:
        logger.error(f"handle_location error: {e}")
        await update.message.reply_text("Ошибка при обработке локации")

async def forecast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id not in user_locations:
        await update.message.reply_text("Сначала отправь геолокацию")
        return
    lat, lon = user_locations[user_id]
    forecast_text = await get_forecast(lat, lon)
    await update.message.reply_text(forecast_text)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Global error: {context.error}")


# ====================== BOT SETUP ======================
bot_app.add_handler(CommandHandler("start", start))
bot_app.add_handler(CommandHandler("forecast", forecast_cmd))
bot_app.add_handler(MessageHandler(filters.LOCATION, handle_location))
bot_app.add_error_handler(error_handler)


# ====================== FASTAPI ======================
@app.get("/")
async def health():
    return {"status": "ok"}


@app.post(WEBHOOK_PATH)
async def webhook(req: Request):
    data = await req.json()
    update = Update.de_json(data, bot)
    await bot_app.process_update(update)
    return {"ok": True}


@app.on_event("startup")
async def startup():
    await bot_app.initialize()
    await bot_app.start()
    if WEBHOOK_URL:
        await bot.set_webhook(WEBHOOK_URL)
        logger.info(f"✅ Webhook успешно установлен: {WEBHOOK_URL}")
    else:
        logger.error("❌ WEBHOOK_URL не задан в переменных окружения!")


@app.on_event("shutdown")
async def shutdown():
    await http_client.aclose()
    await bot_app.stop()
    await bot_app.shutdown()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
