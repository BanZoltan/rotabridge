import os
import json
import time
import glob
import logging
import smtplib
from email.mime.text import MIMEText
from pathlib import Path
from typing import Dict, List, Any

import requests

# --- Beállítások ---
TRACCAR_BASE_URL = os.environ.get("TRACCAR_BASE_URL")
POLLING_INTERVAL = int(os.environ.get("POLLING_INTERVAL", "900"))
ANISETTE_URL = os.environ.get("ANISETTE_URL", "http://anisette:6969")

# E-mail küldéshez
EMAIL_HOST = os.environ.get("EMAIL_HOST")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_USER = os.environ.get("EMAIL_USER")
EMAIL_PASS = os.environ.get("EMAIL_PASS")
EMAIL_TO   = os.environ.get("EMAIL_TO")

KEYS_DIR = Path("/bridge/data/keys")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# --- EMAIL ---
def send_error_email(subject, msg):
    if not (EMAIL_HOST and EMAIL_USER and EMAIL_PASS and EMAIL_TO):
        logging.info("No email configuration set, skipping email notification.")
        return
    try:
        smtp = smtplib.SMTP(EMAIL_HOST, EMAIL_PORT)
        smtp.starttls()
        smtp.login(EMAIL_USER, EMAIL_PASS)
        message = MIMEText(msg)
        message["Subject"] = subject
        message["From"] = EMAIL_USER
        message["To"] = EMAIL_TO
        smtp.sendmail(EMAIL_USER, EMAIL_TO, message.as_string())
        smtp.quit()
        logging.info("Error notification email sent.")
    except Exception as e:
        logging.error(f"Failed to send error email: {e}")

# --- KULCSOK BETÖLTÉSE ---
def load_all_keys() -> Dict[str, List[Dict[str, str]]]:
    keys_by_id = {}
    for file in KEYS_DIR.glob("*"):
        if file.suffix == ".json":
            try:
                with open(file, "r") as f:
                    keys = json.load(f)
                traccar_id = file.stem
                keys_by_id[traccar_id] = keys
            except Exception as e:
                logging.error(f"Hiba a keyfile ({file}): {e}")
                send_error_email("FindMy Bridge - keyfile hiba", f"Hiba a keyfile ({file}): {e}")
        else:
            try:
                with open(file, "r") as f:
                    lines = [l.strip() for l in f if l.strip()]
                keys = []
                for line in lines:
                    if line.startswith("Private key:"):
                        keys.append({
                            "private_key": line.split(":", 1)[1].strip(),
                            "adv_key": "",
                            "hashed_private_key": ""
                        })
                traccar_id = file.stem
                keys_by_id[traccar_id] = keys
                logging.info(f"Csak privát kulcsos keyfile betöltve: {file} ({len(keys)} kulcs)")
            except Exception as e:
                logging.error(f"Hiba a privát kulcsos keyfile ({file}): {e}")
                send_error_email("FindMy Bridge - privát kulcsos keyfile hiba", f"Hiba a file ({file}): {e}")
    return keys_by_id

# --- DEDUPLIKÁLÁS persistent_data-val ---
def deduplicate_reports_with_persistence(new_reports: List[Dict[str, Any]], uploaded: List[Dict[str, Any]], pending: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = {
        (r["id"], r["timestamp"])
        for r in uploaded + pending
    }
    return [r for r in new_reports if (r["id"], r["timestamp"]) not in seen]


# --- FINDMY API MOCK/VALÓS HÍVÁS ---
def fetch_location_from_apple(adv_key: str, private_key: str, hashed_private_key: str) -> List[Dict[str, Any]]:
    """
    TODO: Integrálni a valós FindMy API Python könyvtárat!
    Jelenleg csak teszt dummy adatot ad vissza.
    """
    # TODO: Itt hívd a findmy-python (pl. dchristl/macless-haystack, openhaystack, vagy jannisko bridge könyvtárát)
    # a valós hely lekérdezéshez. Most csak minta adat:
    return [{
        "timestamp": int(time.time()),
        "lat": 47.0,
        "lon": 19.0,
        "accuracy": 50
    }]

# --- TRACCAR FELTÖLTÉS ---
def upload_to_traccar(traccar_id: str, report: Dict[str, Any]):
    try:
        url = f"{TRACCAR_BASE_URL}/api/positions"
        payload = {
            "deviceId": int(traccar_id),
            "latitude": report["lat"],
            "longitude": report["lon"],
            "accuracy": report.get("accuracy", 50),
            "fixTime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(report["timestamp"])),
            "valid": True,
            "attributes": {}
        }
        resp = requests.post(url, json=payload)
        if resp.status_code not in (200, 201):
            raise Exception(f"Traccar HTTP {resp.status_code}: {resp.text}")
        logging.info(f"Pozíció feltöltve Traccar-ba (ID {traccar_id}): {payload['latitude']}, {payload['longitude']}")
    except Exception as e:
        logging.error(f"Traccar feltöltés hiba ({traccar_id}): {e}")
        send_error_email("FindMy Bridge - Traccar feltöltés hiba", f"Traccar feltöltés hiba ({traccar_id}): {e}")

# --- FŐ FOLYAMAT ---
def main_loop():
    while True:
        try:
            logging.info("Kulcsok betöltése...")
            all_keys = load_all_keys()
            for traccar_id, keys in all_keys.items():
                all_reports = []
                for key in keys:
                    try:
                        adv = key["adv_key"]
                        priv = key["private_key"]
                        hashed = key["hashed_private_key"]
                        # Helyadat lekérdezés Apple-től (TODO: valódi FindMy integráció!)
                        reports = fetch_location_from_apple(adv, priv, hashed)
                        all_reports.extend(reports)
                    except Exception as e:
                        logging.error(f"Tracker kulcs hiba ({traccar_id}): {e}")
                        send_error_email("FindMy Bridge - tracker kulcs hiba", f"Tracker kulcs hiba ({traccar_id}): {e}")
                # Dedup
                dedup_reports = deduplicate_reports(all_reports)
                if dedup_reports:
                    # Csak a legfrissebbet töltjük fel!
                    report = dedup_reports[-1]
                    upload_to_traccar(traccar_id, report)
                else:
                    logging.info(f"Nincs új pozíció ({traccar_id})")
            logging.info(f"Polling kész, {POLLING_INTERVAL} mp múlva újra.")
        except Exception as e:
            logging.error(f"Fő ciklushiba: {e}")
            send_error_email("FindMy Bridge - fő ciklushiba", str(e))
        time.sleep(POLLING_INTERVAL)

if __name__ == "__main__":
    logging.info("FindMy-Traccar-Bridge-Rotating indul...")
    main_loop()
