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
from datetime import datetime
from collections import defaultdict

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
        desc = data['weather'][0]['description'].capitalize()
emoji = weather_emoji(desc)

sunrise = datetime.fromtimestamp(data["sys"]["sunrise"]).strftime("%H:%M")
sunset = datetime.fromtimestamp(data["sys"]["sunset"]).strftime("%H:%M")

return (
    f"╔════════════════╗\n"
    f"      🌍 WEATHER ULTRA\n"
    f"╚════════════════╝\n\n"

    f"{emoji} {desc}\n\n"

    f"🌡 Температура: {data['main']['temp']}°C\n"
    f"🥵 Ощущается: {data['main']['feels_like']}°C\n"
    f"💧 Влажность: {data['main']['humidity']}%\n"
    f"💨 Ветер: {data['wind']['speed']} м/с\n"
    f"📊 Давление: {data['main']['pressure']} hPa\n\n"

    f"🌅 Восход: {sunrise}\n"
    f"🌇 Закат: {sunset}"
)


async def get_forecast(lat: float, lon: float) -> str:
    data = await safe_request(
        "https://api.openweathermap.org/data/2.5/forecast",
        {
            "lat": lat,
            "lon": lon,
            "appid": OPENWEATHER_TOKEN,
            "units": "metric",
            "lang": "ru",
        },
    )

    if not data or "list" not in data:
        return "❌ Не удалось получить прогноз"

    result = []

    # ======================
    # ПРОГНОЗ КАЖДЫЕ 3 ЧАСА
    # ======================

    result.append("📍 СЕГОДНЯ ПО ЧАСАМ\n")

    today = datetime.now().strftime("%Y-%m-%d")

    hourly_count = 0

    for item in data["list"]:
        dt = item["dt_txt"]

        if not dt.startswith(today):
            continue

        hour = dt.split(" ")[1][:5]

        desc = item["weather"][0]["description"].capitalize()

        emoji = weather_emoji(desc)

        temp = round(item["main"]["temp"])

        result.append(
            f"🕒 {hour}   {emoji} {temp}°C   {desc}"
        )

        hourly_count += 1

        if hourly_count >= 8:
            break

    # ======================
    # ПРОГНОЗ НА 4 ДНЯ
    # ======================

    result.append("\n")
    result.append("📅 ПРОГНОЗ НА 4 ДНЯ\n")

    grouped = defaultdict(list)

    for item in data["list"]:
        dt = item["dt_txt"]

        date = dt.split(" ")[0]
        hour = int(dt.split(" ")[1][:2])

        if hour not in [9, 18]:
            continue

        label = "🌅 Утро" if hour == 9 else "🌙 Вечер"

        desc = item["weather"][0]["description"].capitalize()

        emoji = weather_emoji(desc)

        temp = round(item["main"]["temp"])

        grouped[date].append(
            f"{label}\n"
            f"{emoji} {desc}\n"
            f"🌡 {temp}°C"
        )

    day_count = 0

    for date, entries in grouped.items():

        if day_count >= 4:
            break

        pretty = datetime.strptime(date, "%Y-%m-%d").strftime("%d.%m")

        result.append("━━━━━━━━━━")
        result.append(f"🗓 {pretty}")

        for entry in entries:
            result.append(entry)
            result.append("")

        day_count += 1

    # ======================
    # AI WEATHER ADVICE
    # ======================

    current_temp = data["list"][0]["main"]["temp"]

    result.append("━━━━━━━━━━")

    if current_temp >= 30:
        result.append("🥵 Совет: сегодня лучше избегать солнца.")
    elif current_temp <= 0:
        result.append("🧥 Совет: одевайся теплее.")
    elif current_temp <= 10:
        result.append("☕ Совет: прохладно, лучше взять куртку.")
    else:
        result.append("😎 Отличная погода для прогулки.")

    return "\n".join(result)

def weather_emoji(desc: str) -> str:
    desc = desc.lower()

    if "ясно" in desc:
        return "☀️"
    if "обла" in desc:
        return "☁️"
    if "дожд" in desc:
        return "🌧"
    if "гроза" in desc:
        return "⛈"
    if "снег" in desc:
        return "❄️"
    if "туман" in desc:
        return "🌫"

    return "🌤"

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
            f"📍 {address}\n\n"
            f"{weather}\n\n"
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

    # 🔹 Логируем весь апдейт для отладки
    print("===== NEW UPDATE =====")
    print(data)

    update = Update.de_json(data, bot_app.bot)
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
