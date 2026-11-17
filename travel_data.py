import os
import json
import urllib.request
import urllib.parse
import urllib.error
from dotenv import load_dotenv

load_dotenv()

GEO_KEY = os.getenv("GEOAPIFY_API_KEY")
OWM_KEY = os.getenv("OPENWEATHER_API_KEY")

# Maps a user interest to Geoapify place categories
INTEREST_CATEGORIES = {
    "heritage": "heritage,tourism.sights",
    "history": "heritage,tourism.sights",
    "culture": "entertainment.museum,tourism.sights",
    "food": "catering.restaurant",
    "shopping": "commercial.shopping_mall",
    "nature": "natural",
    "beaches": "beach",
    "adventure": "sport,entertainment",
    "religious": "religion",
    "sightseeing": "tourism.sights",
}


def get_json(url, params):
    full_url = url + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(full_url, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print("HTTP error", e.code, e.read().decode("utf-8", errors="ignore"))
        raise


def get_places(city, interests, limit=5):
    """Return top places for each interest in the given city."""
    geo = get_json("https://api.geoapify.com/v1/geocode/search", {
        "text": city,
        "format": "json",
        "limit": 1,
        "apiKey": GEO_KEY,
    })
    lat = geo["results"][0]["lat"]
    lon = geo["results"][0]["lon"]

    results = {}
    seen = set()
    for interest in interests:
        cats = INTEREST_CATEGORIES.get(interest.lower().strip(), "tourism.sights")
        data = get_json("https://api.geoapify.com/v2/places", {
            "categories": cats,
            "filter": f"circle:{lon},{lat},40000",
            "bias": f"proximity:{lon},{lat}",
            "limit": 30,
            "apiKey": GEO_KEY,
        })
        picked = []
        for feature in data.get("features", []):
            props = feature.get("properties", {})
            name = ((props.get("name_international") or {}).get("en") or props.get("name") or "").strip()
            if name and name not in seen:
                seen.add(name)
                picked.append({
                    "name": name,
                    "category": (props.get("categories") or [""])[0],
                    "address": props.get("address_line2", ""),
                })
            if len(picked) == limit:
                break
        results[interest] = picked
    return results


def get_weather(city):
    """Return a simple day-wise forecast (next ~5 days)."""
    data = get_json("https://api.openweathermap.org/data/2.5/forecast", {
        "q": city,
        "appid": OWM_KEY,
        "units": "metric",
    })
    days = {}
    for item in data["list"]:
        date = item["dt_txt"].split(" ")[0]
        d = days.setdefault(date, {"temps": [], "desc": []})
        d["temps"].append(item["main"]["temp"])
        d["desc"].append(item["weather"][0]["description"])

    forecast = []
    for date, d in days.items():
        forecast.append({
            "date": date,
            "min_c": round(min(d["temps"])),
            "max_c": round(max(d["temps"])),
            "summary": max(set(d["desc"]), key=d["desc"].count),
        })
    return forecast


if __name__ == "__main__":
    city = "Goa"
    print("PLACES")
    print(json.dumps(get_places(city, ["beaches", "food"]), indent=2))
    print("WEATHER")
    print(json.dumps(get_weather(city), indent=2))