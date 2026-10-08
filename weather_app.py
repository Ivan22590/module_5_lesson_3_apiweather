"""Погодное CLI-приложение на базе OpenWeather API.

Возможности:
    * геокодинг города (Geocoding API) и запрос текущей погоды (Current Weather API);
    * кэширование последнего успешного ответа в weather_cache.json;
    * ретраи с экспоненциальной паузой при 429 и временных сетевых ошибках;
    * понятные сообщения об ошибках без трейсбеков.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional

import requests
from dotenv import load_dotenv

# --- Конфигурация -----------------------------------------------------------

# override=True — значение API_KEY из .env имеет приоритет над системными
# переменными окружения.
load_dotenv(override=True)

API_KEY: Optional[str] = os.getenv("API_KEY")

GEOCODE_URL = "https://api.openweathermap.org/geo/1.0/direct"
WEATHER_URL = "https://api.openweathermap.org/data/2.5/weather"

CACHE_FILE = "weather_cache.json"
CACHE_TTL_SECONDS = 3 * 60 * 60  # 3 часа

REQUEST_TIMEOUT = 10  # секунд
MAX_RETRIES = 3  # всего 3 повтора
RETRY_DELAYS = (1, 2, 4)  # экспоненциальная пауза в секундах


class WeatherError(Exception):
    """Понятная пользователю ошибка погодного приложения."""


# --- HTTP с ретраями --------------------------------------------------------


def _request_json(url: str, params: dict[str, Any]) -> dict[str, Any]:
    """Выполняет GET-запрос с ретраями и возвращает JSON.

    Повторяет запрос до MAX_RETRIES раз при HTTP 429 и временных сетевых
    ошибках, выдерживая экспоненциальную паузу 1s, 2s, 4s. При остальных
    HTTP-ошибках сразу поднимает WeatherError с понятным сообщением.
    """
    if not API_KEY or API_KEY == "your_openweather_api_key_here":
        raise WeatherError(
            "API-ключ не задан. Укажите действительный API_KEY в файле .env."
        )

    params = {**params, "appid": API_KEY}
    last_error = "неизвестная ошибка"

    for attempt in range(MAX_RETRIES + 1):
        try:
            response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        except requests.exceptions.Timeout:
            last_error = "превышено время ожидания ответа от сервиса"
        except requests.exceptions.ConnectionError:
            last_error = "не удалось подключиться к сервису (проверьте интернет)"
        except requests.exceptions.RequestException as exc:
            last_error = f"сетевая ошибка: {exc}"
        else:
            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError:
                    raise WeatherError("Сервис вернул некорректный ответ (не JSON).")

            if response.status_code == 401:
                raise WeatherError(
                    "Невалидный API-ключ (401). Проверьте значение API_KEY в .env."
                )
            if response.status_code == 404:
                raise WeatherError("Данные не найдены (404).")

            if response.status_code == 429:
                last_error = "превышен лимит запросов (429)"
            else:
                raise WeatherError(
                    f"Сервис вернул ошибку {response.status_code}: "
                    f"{response.text[:200]}"
                )

        # Пауза перед следующей попыткой (если она есть).
        if attempt < MAX_RETRIES:
            delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
            print(
                f"  ...{last_error}. Повтор через {delay} с "
                f"({attempt + 1}/{MAX_RETRIES})",
                file=sys.stderr,
            )
            time.sleep(delay)

    raise WeatherError(f"Не удалось получить данные: {last_error}.")


# --- Публичные функции ------------------------------------------------------


def get_coordinates(city: str) -> tuple[float, float]:
    """Возвращает (широта, долгота) для названия города через Geocoding API."""
    city = (city or "").strip()
    if not city:
        raise WeatherError("Название города не может быть пустым.")

    data = _request_json(
        GEOCODE_URL,
        {"q": city, "limit": 1, "lang": "ru"},
    )

    if not isinstance(data, list) or not data:
        raise WeatherError(f"Город «{city}» не найден.")

    location = data[0]
    try:
        lat = float(location["lat"])
        lon = float(location["lon"])
    except (KeyError, TypeError, ValueError):
        raise WeatherError(f"Не удалось определить координаты города «{city}».")

    return lat, lon


def get_weather_by_coordinates(lat: float, lon: float) -> dict[str, Any]:
    """Возвращает текущую погоду по координатам через Current Weather API."""
    try:
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError):
        raise WeatherError("Координаты должны быть числами.")

    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        raise WeatherError(
            "Некорректные координаты: широта от -90 до 90, долгота от -180 до 180."
        )

    return _request_json(
        WEATHER_URL,
        {"lat": lat, "lon": lon, "units": "metric", "lang": "ru"},
    )


def format_weather(city: str, data: dict[str, Any]) -> str:
    """Формирует строку вида «Погода в Москва: 12.4°C, пасмурно»."""
    try:
        temp = data["main"]["temp"]
        description = data["weather"][0]["description"]
    except (KeyError, IndexError, TypeError):
        raise WeatherError("Ответ сервиса не содержит ожидаемых данных о погоде.")

    return f"Погода в {city}: {float(temp):.1f}°C, {description}"


# --- Кэш --------------------------------------------------------------------


def save_cache(city: str, lat: float, lon: float, weather: dict[str, Any]) -> None:
    """Сохраняет последний успешный ответ в weather_cache.json."""
    payload = {
        "city": city,
        "lat": lat,
        "lon": lon,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "weather": weather,
    }
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
    except OSError as exc:
        print(f"  (не удалось сохранить кэш: {exc})", file=sys.stderr)


def load_cache() -> Optional[dict[str, Any]]:
    """Читает кэш из файла, если он существует и корректен."""
    if not os.path.exists(CACHE_FILE):
        return None
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def cache_age_seconds(cache: dict[str, Any]) -> Optional[float]:
    """Возвращает возраст кэша в секундах или None, если метка времени битая."""
    fetched_at = cache.get("fetched_at")
    if not fetched_at:
        return None
    try:
        moment = datetime.fromisoformat(fetched_at)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - moment).total_seconds()


def offer_cached_weather() -> bool:
    """Предлагает вывести данные из кэша, если он младше 3 часов."""
    cache = load_cache()
    if not cache:
        return False

    age = cache_age_seconds(cache)
    if age is None or age > CACHE_TTL_SECONDS:
        return False

    minutes = int(age // 60)
    answer = input(
        f"  Доступны сохранённые данные ({minutes} мин. назад). Вывести их? [y/N]: "
    ).strip().lower()
    if answer not in ("y", "yes", "д", "да"):
        return False

    city = cache.get("city", "неизвестно")
    weather = cache.get("weather", {})
    print(format_weather(city, weather))
    return True


# --- Сценарии запроса -------------------------------------------------------


def weather_for_city(city: str) -> None:
    """Получает и печатает погоду по названию города."""
    lat, lon = get_coordinates(city)
    weather = get_weather_by_coordinates(lat, lon)
    print(format_weather(city, weather))
    save_cache(city, lat, lon, weather)


def weather_for_coordinates(lat: float, lon: float) -> None:
    """Получает и печатает погоду по координатам."""
    weather = get_weather_by_coordinates(lat, lon)
    city = weather.get("name") or f"{lat}, {lon}"
    print(format_weather(city, weather))
    save_cache(city, lat, lon, weather)


def _read_coordinate(prompt: str) -> Optional[float]:
    """Считывает координату; возвращает None при пустом/некорректном вводе."""
    raw = input(prompt).strip().replace(",", ".")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        print("  Некорректное число, попробуйте снова.")
        return None


# --- CLI --------------------------------------------------------------------


MENU = """
Выберите режим:
  1 — погода по названию города
  2 — погода по координатам
  0 — выход
"""


def main() -> None:
    """Точка входа CLI-приложения."""
    print("=" * 50)
    print(" Погода через OpenWeather")
    print("=" * 50)

    if not API_KEY or API_KEY == "your_openweather_api_key_here":
        print(
            "API-ключ не задан. Укажите действительный API_KEY в файле .env "
            "и перезапустите программу."
        )
        return

    while True:
        print(MENU)
        choice = input("Ваш выбор: ").strip()

        if choice == "0":
            print("До встречи!")
            break

        try:
            if choice == "1":
                city = input("Введите название города: ").strip()
                if not city:
                    print("Название города не может быть пустым.")
                    continue
                weather_for_city(city)

            elif choice == "2":
                lat = _read_coordinate("Введите широту (например 55.75): ")
                if lat is None:
                    continue
                lon = _read_coordinate("Введите долготу (например 37.62): ")
                if lon is None:
                    continue
                weather_for_coordinates(lat, lon)

            else:
                print("Неизвестный пункт меню. Введите 1, 2 или 0.")

        except WeatherError as exc:
            print(f"Ошибка: {exc}")
            offer_cached_weather()
        except KeyboardInterrupt:
            print("\nПрервано пользователем. До встречи!")
            break


if __name__ == "__main__":
    main()
