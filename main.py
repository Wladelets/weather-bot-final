import logging
import os
import asyncio
import time
from typing import Dict, Tuple

from fastapi import FastAPI, Request
from telegram import (
    Update,
    Bot,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from geopy.geocoders import Nominatim
from dotenv import load_dotenv
from httpx import AsyncClient, Timeout, RequestError
from datetime import datetime
from collections import defaultdict
from difflib import get_close_matches

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

user_languages: Dict[int, str] = {}
weather_cache = {}
weather_alert_tasks = {}
city_cache = {}  # name -> (lat, lon)
geo_cache = {}   # normalized query -> result
KNOWN_CITIES = [
    "london", "paris", "tokyo", "new york", "berlin",
    "madrid", "rome"
]

def normalize_city(text: str) -> str:
    return text.strip().lower()

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
    cache_key = f"{lat}:{lon}"

    if cache_key in weather_cache:
    
        cached_data, cached_time = weather_cache[cache_key]
    
        if time.time() - cached_time < 600:
            data = cached_data
        else:
            data = None
    else:
        data = None

    data = await safe_request(
        "https://api.openweathermap.org/data/2.5/weather",
        {
            "lat": lat,
            "lon": lon,
            "appid": OPENWEATHER_TOKEN,
            "units": "metric",
            "lang": "ru",
        },
    )

    if not data:
        data = await safe_request(...)
        weather_cache[cache_key] = (
        data,
        time.time()
    )
        return "❌ Не удалось получить погоду"

    desc = data["weather"][0]["description"].capitalize()

    emoji = weather_emoji(desc)
    theme = "🌙 NIGHT MODE"

    hour_now = datetime.now().hour
    
    if 6 <= hour_now <= 18:
        theme = "☀️ DAY MODE"

    sunrise = datetime.fromtimestamp(
        data["sys"]["sunrise"]
    ).strftime("%H:%M")

    sunset = datetime.fromtimestamp(
        data["sys"]["sunset"]
    ).strftime("%H:%M")

    return (
        f"╔════════════════╗\n"
        f"   🌍 WEATHER ULTRA\n"
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

    if not data:
        return "❌ Forecast unavailable"

    result = []

    # ==================================
    # СЕГОДНЯ КАЖДЫЕ 3 ЧАСА
    # ==================================

    result.append("🕒 <b>СЕГОДНЯ ПО ЧАСАМ</b>\n")

    from datetime import timezone

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

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
            f"🕒 {hour}  {emoji} {temp}°C  {desc}"
        )

        hourly_count += 1

        if hourly_count >= 8:
            break

    # ==================================
    # СЛЕДУЮЩИЕ 4 ДНЯ
    # ==================================

    result.append("\n📅 <b>ПРОГНОЗ НА 4 ДНЯ</b>\n")

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

        pretty = datetime.strptime(
            date,
            "%Y-%m-%d"
        ).strftime("%d.%m")

        result.append("━━━━━━━━━━")
        result.append(f"🗓 <b>{pretty}</b>")

        for entry in entries:
            result.append(entry)
            result.append("")

        day_count += 1

    return "\n".join(result)
    # ====================== UV INDEX ======================
async def get_uv_index(lat: float, lon: float) -> str:
    data = await safe_request(
        "https://api.openweathermap.org/data/3.0/onecall",
        {
            "lat": lat,
            "lon": lon,
            "appid": OPENWEATHER_TOKEN,
            "exclude": "minutely,hourly,daily,alerts",
            "units": "metric",
        },
    )

    if not data:
        return "❌ UV unavailable"

    uv = data.get("current", {}).get("uvi", 0)

    if uv < 3:
        level = "🟢 Low"
    elif uv < 6:
        level = "🟡 Medium"
    elif uv < 8:
        level = "🟠 High"
    else:
        level = "🔴 Extreme"

    return f"🌈 UV Index: {uv} ({level})"


# ====================== AIR QUALITY ======================
async def get_air_quality(lat: float, lon: float) -> str:
    data = await safe_request(
        "http://api.openweathermap.org/data/2.5/air_pollution",
        {
            "lat": lat,
            "lon": lon,
            "appid": OPENWEATHER_TOKEN,
        },
    )

    if not data:
        return "❌ Air quality unavailable"

    air = data["list"][0]

    aqi = air["main"]["aqi"]

    levels = {
        1: "🟢 Good",
        2: "🟡 Fair",
        3: "🟠 Moderate",
        4: "🔴 Poor",
        5: "⚫ Very Poor",
    }

    comp = air["components"]

    return (
        f"🌬 AIR QUALITY\n"
        f"AQI: {aqi} ({levels.get(aqi)})\n"
        f"PM2.5: {comp['pm2_5']}\n"
        f"PM10: {comp['pm10']}\n"
        f"CO: {comp['co']}\n"
        f"O₃: {comp['o3']}"
    )


# ====================== SUN INFO ======================
async def get_sun_info(lat: float, lon: float) -> str:
    data = await safe_request(
        "https://api.openweathermap.org/data/2.5/weather",
        {
            "lat": lat,
            "lon": lon,
            "appid": OPENWEATHER_TOKEN,
            "units": "metric",
        },
    )

    if not data:
        return "❌ Sun info unavailable"

    sunrise = datetime.fromtimestamp(data["sys"]["sunrise"]).strftime("%H:%M")
    sunset = datetime.fromtimestamp(data["sys"]["sunset"]).strftime("%H:%M")

    return (
        f"☀️ Sunrise: {sunrise}\n"
        f"🌙 Sunset: {sunset}"
    )


# ====================== AI WEATHER ADVICE ======================
def generate_ai_advice(temp, wind, humidity, uv):
    advice = []

    if temp > 30:
        advice.append("🥵 Очень жарко — пей больше воды")

    if wind > 10:
        advice.append("🌪 Сильный ветер")

    if humidity > 85:
        advice.append("💧 Высокая влажность")

    if uv > 6:
        advice.append("🧴 Используй SPF")

    if not advice:
        advice.append("✅ Погода комфортная")

    return "\n".join(advice)

async def weather_alert_loop(user_id: int, context):

    while True:

        try:

            if user_id not in user_locations:
                return

            lat, lon = user_locations[user_id]

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

            if not data:
                await asyncio.sleep(1800)
                continue

            next_forecast = data["list"][0]

            weather_main = next_forecast["weather"][0]["main"].lower()

            if "rain" in weather_main:

                await context.bot.send_message(
                    chat_id=user_id,
                    text="⚠️ Через несколько часов ожидается дождь 🌧"
                )

            if "snow" in weather_main:

                await context.bot.send_message(
                    chat_id=user_id,
                    text="❄️ Ожидается снег"
                )

        except Exception as e:
            logger.error(e)

        await asyncio.sleep(3600)


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

def tr(lang: str, ru: str, en: str):

    if lang.startswith("ru"):
        return ru

        return en

# ====================== HANDLERS ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    lang = update.effective_user.language_code or "en"

    user_languages[update.effective_user.id] = lang

    keyboard = [[
        KeyboardButton(
            "📍 Отправить геолокацию",
            request_location=True
        )
    ]]

    await update.message.reply_text(
        tr(
            lang,
            "Нажми кнопку и получи погоду 👇",
            "Press button to get weather 👇"
        ),
        reply_markup=ReplyKeyboardMarkup(
            keyboard,
            resize_keyboard=True
        ),
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
        lang = update.effective_user.language_code or "en"

        user_languages[user.id] = lang
        loc = update.message.location
        lat, lon = loc.latitude, loc.longitude

        hour_now = datetime.now().hour
        
        theme = "🌙 NIGHT MODE"
        
        if 6 <= hour_now <= 18:
            theme = "☀️ DAY MODE"
        
        user_locations[user.id] = (lat, lon)
        # запуск alert loop
        if user.id not in weather_alert_tasks:
        
            weather_alert_tasks[user.id] = asyncio.create_task(
                weather_alert_loop(user.id, context)
            )

        # Параллельные запросы к geolocator и API
        loop = asyncio.get_running_loop()
        address_task = loop.run_in_executor(None, get_address, lat, lon)
        
        weather_data = await safe_request(
            "https://api.openweathermap.org/data/2.5/weather",
            {
                "lat": lat,
                "lon": lon,
                "appid": OPENWEATHER_TOKEN,
                "units": "metric",
                "lang": "ru",
            },
        )
        
        weather, forecast, address, uv, air, sun = await asyncio.gather(
            get_weather(lat, lon),
            get_forecast(lat, lon),
            address_task,
            get_uv_index(lat, lon),
            get_air_quality(lat, lon),
            get_sun_info(lat, lon),
        )
        ai_advice = generate_ai_advice(
            weather_data["main"]["temp"],
            weather_data["wind"]["speed"],
            weather_data["main"]["humidity"],
            weather_data.get("uvi", 0),
        )
        
        map_url = (
            f"https://tile.openweathermap.org/map/precipitation_new/5/16/10.png"
            f"?appid={OPENWEATHER_TOKEN}"
        )
        map_url = (
            f"https://static-maps.yandex.ru/1.x/"
            f"?ll={lon},{lat}"
            f"&size=650,450"
            f"&z=9"
            f"&l=map"
            f"&pt={lon},{lat},pm2rdm"
        )

        radar_url = "https://tilecache.rainviewer.com/v2/radar/latest/512/6/32/22/2/1_1.png"

        await update.message.reply_photo(
            photo=radar_url,
            caption="🛰 Radar snapshot (last 10 min)"
        )
        
        radar_map = (
            f"https://tile.openweathermap.org/map/precipitation_new/"
            f"5/{int(lon)}/{int(lat)}.png?appid={OPENWEATHER_TOKEN}"
        )
        
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔄 Refresh", callback_data="refresh"),
                InlineKeyboardButton("📅 Weekly", callback_data="weekly"),
            ],
            [
                InlineKeyboardButton("🌍 Change location", callback_data="change")
            ]
        ])
        await update.message.reply_photo(
            photo=map_url,
            caption=(
                f"🌍 WEATHER ULTRA\n\n"
                f"📍 {address}\n\n"
                f"{theme}\n\n"
                f"{weather}\n\n"
                f"{uv}\n"
                f"{air}\n"
                f"{sun}\n\n"
                f"🧠 SMART INSIGHT:\n{ai_advice}"
            ),
            parse_mode="HTML",
            reply_markup=keyboard
        )
        await update.message.reply_text(
            forecast,
            parse_mode="HTML"
        )
        
        await update.message.reply_animation(
            animation=radar_gif,
            caption="🛰 Live Weather Radar""🌧 Rain probability next hour: HIGH"
        )

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
    # =========================
    # 🧠 SMART GEO RESOLVER
    # =========================
async def resolve_city(text: str, user_lat=None, user_lon=None):

    raw = normalize_city(text)
    key = raw
    # =========================
    # 1. NEAR ME SUPPORT
    # =========================

    if raw in ["near me", "me", "my location"] and user_lat and user_lon:
        return user_lat, user_lon, "📍 Your location"
    # =========================
    # 2. CACHE CHECK
    # =========================

    if key in geo_cache:
        return geo_cache[key]
    # =========================
    # 3. AUTOCORRECTION
    # =========================

    match = get_close_matches(raw, KNOWN_CITIES, n=1, cutoff=0.7)
    if match:
        raw = match[0]
    # =========================
    # 4. OPENWEATHER DIRECT (FAST PATH)
    # =========================

    data = await safe_request(
        "https://api.openweathermap.org/data/2.5/weather",
        {
            "q": raw,
            "appid": OPENWEATHER_TOKEN,
            "units": "metric",
        },
    )

    if data:
        result = (
            data["coord"]["lat"],
            data["coord"]["lon"],
            raw.title()
        )
        geo_cache[key] = result
        return result
    # =========================
    # 5. FALLBACK GEOPY
    # =========================

    location = geolocator.geocode(raw)

    if location:
        result = (location.latitude, location.longitude, location.address)
        geo_cache[key] = result
        return result

    return None


async def city_weather(update: Update, context: ContextTypes.DEFAULT_TYPE):

    text = normalize_city(update.message.text)

    try:
        result = await resolve_city(text)

        if not result:
            await update.message.reply_text("❌ City not found")
            return

        lat, lon, name = result

        weather = await get_weather(lat, lon)
        forecast = await get_forecast(lat, lon)

        await update.message.reply_text(
            f"📍 {name}\n\n{weather}\n\n{forecast}",
            parse_mode="HTML"
        )

    except Exception as e:
        logger.error(e)
        await update.message.reply_text("❌ Error getting city weather")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id

    if user_id not in user_locations:
        await query.message.reply_text(
            "📍 Отправь геолокацию"
        )
        return

    lat, lon = user_locations[user_id]

    if query.data == "refresh":

        weather = await get_weather(lat, lon)

        forecast = await get_forecast(lat, lon)

        await query.message.reply_text(
            f"{weather}\n\n{forecast}",
            parse_mode="HTML"
        )

    elif query.data == "weekly":

        forecast = await get_forecast(lat, lon)

        await query.message.reply_text(
            forecast,
            parse_mode="HTML"
        )

    elif query.data == "change":

        keyboard = [[
            KeyboardButton(
                "📍 Отправить геолокацию",
                request_location=True
            )
        ]]

        await query.message.reply_text(
            "🌍 Отправь новую геолокацию",
            reply_markup=ReplyKeyboardMarkup(
                keyboard,
                resize_keyboard=True
            )
        )



async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Global error: {context.error}")

parse_mode="HTML"


# ====================== BOT SETUP ======================
bot_app.add_handler(CommandHandler("start", start))
bot_app.add_handler(CommandHandler("forecast", forecast_cmd))
bot_app.add_handler(
    CallbackQueryHandler(button_callback)
)
bot_app.add_handler(MessageHandler(filters.LOCATION, handle_location))
bot_app.add_handler(
    MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        city_weather
    )
)
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
