import os
import queue
import threading
import asyncio
import tempfile
import re
import datetime
import webbrowser
import pyautogui
import serial
import time
import schedule
import edge_tts
import json
import requests
import pygame
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from dotenv import load_dotenv
from vosk import Model, KaldiRecognizer
from flask import Flask, request, jsonify, send_file, after_this_request
from flask_cors import CORS

load_dotenv("secrets.env")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
SPOTIPY_CLIENT_ID = os.getenv("SPOTIPY_CLIENT_ID")
SPOTIPY_CLIENT_SECRET = os.getenv("SPOTIPY_CLIENT_SECRET")
SPOTIPY_REDIRECT_URI = os.getenv("SPOTIPY_REDIRECT_URI")

if not GROQ_API_KEY:
    print("[ERROR]: GROQ_API_KEY could not be found! Check your secrets.env file.")
    exit(1)

sp = None
if SPOTIPY_CLIENT_ID and SPOTIPY_CLIENT_SECRET and SPOTIPY_REDIRECT_URI:
    try:
        auth_manager = SpotifyOAuth(
            client_id=SPOTIPY_CLIENT_ID,
            client_secret=SPOTIPY_CLIENT_SECRET,
            redirect_uri=SPOTIPY_REDIRECT_URI,
            scope="user-modify-playback-state user-read-playback-state user-library-read",
            cache_path=".spotify_token_cache",
            open_browser=True
        )
        sp = spotipy.Spotify(auth_manager=auth_manager)
        print("[SPOTIFY]: Initialized successfully.")
    except Exception as e:
        print(f"[SPOTIFY WARNING]: Could not initialize Spotipy: {e}")

MODEL_PATH = "model"
VOICE_NAME = "en-GB-RyanNeural"
DATA_FILE = "data.json"
WINDOW_SIZE = 6          
COMPRESS_THRESHOLD = 10  

PORT = '/dev/ttyACM0'
BAUD = 9600
arduino = None

try:
    arduino = serial.Serial(PORT, BAUD, timeout=0.1)
    time.sleep(2)
    print(f"[ARDUINO]: Connected successfully on {PORT}")
except Exception as e:
    print(f"[ARDUINO WARNING]: Could not open serial port {PORT}: {e}")

speaking_lock = threading.Lock()
is_speaking = False
is_asleep = False

# Flask App for Web/Phone Chat Integration
app = Flask(__name__)
CORS(app)

@app.route('/ask', methods=['POST'])
def web_ask():
    data = request.json or {}
    cmd = data.get("command", "").strip()
    print(f"[WEB COMMAND]: {cmd}")
    
    reply = process_command_logic(cmd, source="web")
    return jsonify({"reply": reply})

@app.route('/speak', methods=['POST'])
def web_speak():
    data = request.json or {}
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"error": "No text provided"}), 400

    try:
        temp_filename = asyncio.run(_generate_tts_mp3(text))

        @after_this_request
        def remove_file(response):
            try:
                if os.path.exists(temp_filename):
                    os.remove(temp_filename)
            except Exception as e:
                print(f"[CLEANUP ERROR]: {e}")
            return response

        return send_file(temp_filename, mimetype="audio/mpeg")
    except Exception as e:
        print(f"[WEB TTS ERROR]: {e}")
        return jsonify({"error": str(e)}), 500

def run_flask_server():
    app.run(host='127.0.0.1', port=5000, debug=False, use_reloader=False)

def load_data():
    default_data = {
        "system_prompt": (
            "You are JARVIS, Tony Stark's advanced AI assistant. Keep responses concise, witty, and helpful. "
            "CRITICAL RULES:\n"
            "1. Never use emojis or special symbols.\n"
            "2. You can place [GET_TIME] or [GET_DATE] anywhere naturally inside your sentence.\n"
            "3. Include [OPEN_TAB(url)] for browsing.\n"
            "4. Include [TYPE(string)] when asked to type something on the computer.\n"
            "5. Include [CLICK_TEXT(word)] when asked to click a button or text on screen by its label.\n"
            "6. Include [PRESS(key)] when asked to press a specific key.\n"
            "7. Include [PIN_ON(pin)], [PIN_OFF(pin)], or [PIN_TOGGLE(pin)] for digital hardware control (supports pins and analog pins like A0).\n"
            "8. Include [PIN_READ(pin)] or [A_READ(pin)] to read digital or analog pin values.\n"
            "9. Include [A_WRITE(pin,value)] to write PWM analog output values (0-255).\n"
            "10. Include [UNO_VAR(name|data)] to store or update custom variables/states.\n"
            "11. Include [SET_ALARM(HH:MM|repeat|message)] to set alarms.\n"
            "12. Include [ALARM_VOLUME(percent)] to adjust system volume.\n"
            "13. Include [SLEEP] when told to go to sleep.\n"
            "14. Include [SONG_PLAY(song|author)], [SONG_PAUSE], [SONG_RESUME], [SONG_SKIP], [PREV_SONG], [REWIND_SONG], [SHUFFLE_SONG], [LOOP_SONG(boolean)], or [PLAYLIST_PLAY(name)] for Spotify control."
        ),
        "compressed_memory": "",
        "messages": [],
        "alarms": [],
        "jarvis_vars": {}
    }
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                for k, v in default_data.items():
                    if k not in data:
                        data[k] = v
                return data
        except Exception as e:
            print(f"[DATA LOAD ERROR]: {e}")
    return default_data

def save_data(data):
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        print(f"[DATA SAVE ERROR]: {e}")

def strip_emojis(text):
    emoji_pattern = re.compile(
        r"["
        r"\U0001f000-\U0001f9ff"
        r"\U00020000-\U0002ffff"
        r"\u2600-\u26ff"
        r"\u2700-\u27bf"
        r"]+", flags=re.UNICODE
    )
    return emoji_pattern.sub(r"", text)

async def _generate_tts_mp3(text):
    clean_text = strip_emojis(text)
    if not clean_text.strip():
        clean_text = "Yes, sir."
    with tempfile.NamedTemporaryFile(delete=False, suffix='.mp3') as fp:
        temp_filename = fp.name
    communicate = edge_tts.Communicate(clean_text, VOICE_NAME)
    await communicate.save(temp_filename)
    return temp_filename

async def _play_audio(text):
    temp_filename = await _generate_tts_mp3(text)
    os.system(f"mpg123 -q {temp_filename}")
    if os.path.exists(temp_filename):
        os.remove(temp_filename)

def speak(text):
    global is_speaking
    with speaking_lock:
        is_speaking = True
    
    print(f"[J.A.R.V.I.S.]: {text}")
    try:
        asyncio.run(_play_audio(text))
    except Exception as e:
        print(f"[TTS ERROR]: {e}")
        
    with speaking_lock:
        is_speaking = False

def trigger_alarm(message):
    print(f"[ALARM TRIGGERED]: {message}")
    os.system("amixer -q -D default sset Master 100% unmute")
    
    alarm_file = "alarm.mp3"
    if os.path.exists(alarm_file):
        try:
            pygame.mixer.init()
            pygame.mixer.music.load(alarm_file)
            pygame.mixer.music.play()
            while pygame.mixer.music.get_busy():
                time.sleep(1)
            pygame.mixer.quit()
        except Exception as e:
            print(f"[PYGAME ALARM ERROR]: {e}")
            for _ in range(5):
                os.system(f"mpg123 -q {alarm_file}")
    else:
        print("\a\a\a")

def save_alarm_to_data(alarm_time, repeat_type, message):
    data = load_data()
    data["alarms"].append({"time": alarm_time, "repeat": repeat_type, "message": message})
    save_data(data)

def load_saved_alarms():
    data = load_data()
    alarms = data.get("alarms", [])
    if not alarms:
        return
    try:
        for alarm in alarms:
            alarm_time = alarm["time"]
            repeat_type = alarm["repeat"]
            alarm_msg = alarm["message"]
            
            if repeat_type == "weekdays":
                for day in ["monday", "tuesday", "wednesday", "thursday", "friday"]:
                    getattr(schedule.every(), day).at(alarm_time).do(trigger_alarm, message=alarm_msg)
            elif repeat_type == "weekends":
                for day in ["saturday", "sunday"]:
                    getattr(schedule.every(), day).at(alarm_time).do(trigger_alarm, message=alarm_msg)
            elif repeat_type in ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]:
                getattr(schedule.every(), repeat_type).at(alarm_time).do(trigger_alarm, message=alarm_msg)
            elif repeat_type == "once":
                def one_time_alarm(msg=alarm_msg):
                    trigger_alarm(msg)
                    return schedule.CancelJob
                schedule.every().day.at(alarm_time).do(one_time_alarm)
            else:
                schedule.every().day.at(alarm_time).do(trigger_alarm, message=alarm_msg)
        print(f"[ALARM]: Loaded {len(alarms)} saved alarms from {DATA_FILE}")
    except Exception as e:
        print(f"[ALARM LOAD ERROR]: {e}")

def morning_routine():
    print("[AUTOMATION]: Running scheduled morning routine...")
    speak("Routine initiated, sir.")
    if arduino and arduino.is_open:
        arduino.write(b"[PIN_ON(8)]\n")
        print("[SERIAL OUT]: [PIN_ON(8)] sent automatically via scheduler")

def run_scheduler():
    schedule.every().day.at("07:00").do(morning_routine)
    load_saved_alarms()
    while True:
        schedule.run_pending()
        time.sleep(1)

def handle_local_commands(command):
    cmd = command.lower()
    if "youtube" in cmd and ("open" in cmd or "launch" in cmd):
        speak("Opening YouTube for you, sir.")
        webbrowser.open("https://www.youtube.com")
        return True
    elif "google" in cmd and ("open" in cmd or "launch" in cmd):
        speak("Opening Google.")
        webbrowser.open("https://www.google.com")
        return True
    return False

def handle_ai_tags_silent(reply_text):
    global is_asleep
    action_performed = False
    cleaned_text = reply_text

    match_type = re.search(r"\[TYPE\((.*?)\)\]", cleaned_text)
    if match_type:
        pyautogui.write(match_type.group(1), interval=0.05)
        action_performed = True

    match_press = re.search(r"\[PRESS\((.*?)\)\]", cleaned_text)
    if match_press:
        try:
            pyautogui.press(match_press.group(1).strip().lower())
        except Exception as e:
            print(f"[PRESS ERROR]: {e}")
        action_performed = True

    match_text_click = re.search(r"\[CLICK_TEXT\((.*?)\)\]", cleaned_text)
    if match_text_click:
        search_word = match_text_click.group(1).strip()
        try:
            import pytesseract
            import mss
            from PIL import Image
            with mss.MSS() as sct:
                monitor = sct.monitors[0]
                sct_img = sct.grab(monitor)
                img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
            data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
            for i in range(len(data['text'])):
                word = data['text'][i]
                if search_word.lower() in word.lower() and word.strip():
                    x = data['left'][i] + data['width'][i] // 2
                    y = data['top'][i] + data['height'][i] // 2
                    pyautogui.click(x, y)
                    break
        except Exception as e:
            print(f"[OCR CLICK ERROR]: {e}")
        action_performed = True

    match_pin_on = re.search(r"\[PIN_ON\(([^)]+)\)\]", cleaned_text)
    if match_pin_on and arduino and arduino.is_open:
        pin = match_pin_on.group(1).strip()
        arduino.write(f"[PIN_ON({pin})]\n".encode('utf-8'))
        action_performed = True

    match_pin_off = re.search(r"\[PIN_OFF\(([^)]+)\)\]", cleaned_text)
    if match_pin_off and arduino and arduino.is_open:
        pin = match_pin_off.group(1).strip()
        arduino.write(f"[PIN_OFF({pin})]\n".encode('utf-8'))
        action_performed = True

    match_pin_toggle = re.search(r"\[PIN_TOGGLE\(([^)]+)\)\]", cleaned_text)
    if match_pin_toggle and arduino and arduino.is_open:
        pin = match_pin_toggle.group(1).strip()
        arduino.write(f"[PIN_TOGGLE({pin})]\n".encode('utf-8'))
        action_performed = True

    match_pin_read = re.search(r"\[PIN_READ\(([^)]+)\)\]", cleaned_text)
    if match_pin_read and arduino and arduino.is_open:
        pin = match_pin_read.group(1).strip()
        arduino.write(f"[PIN_READ({pin})]\n".encode('utf-8'))
        time.sleep(0.1)
        response = arduino.readline().decode('utf-8').strip()
        print(f"[ARDUINO DIGITAL READ Pin {pin}]: {response}")
        
        data = load_data()
        data["jarvis_vars"][f"pin_{pin}"] = response
        save_data(data)
        action_performed = True

    match_aread = re.search(r"\[A_READ\(([^)]+)\)\]", cleaned_text)
    if match_aread and arduino and arduino.is_open:
        pin = match_aread.group(1).strip()
        arduino.write(f"[A_READ({pin})]\n".encode('utf-8'))
        time.sleep(0.1)
        response = arduino.readline().decode('utf-8').strip()
        print(f"[ARDUINO ANALOG READ Pin {pin}]: {response}")
        
        data = load_data()
        data["jarvis_vars"][f"apin_{pin}"] = response
        save_data(data)
        action_performed = True

    match_awrite = re.search(r"\[A_WRITE\(([^,]+),\s*(\d+)\)\]", cleaned_text)
    if match_awrite and arduino and arduino.is_open:
        pin = match_awrite.group(1).strip()
        val = max(0, min(255, int(match_awrite.group(2).strip())))
        arduino.write(f"[A_WRITE({pin},{val})]\n".encode('utf-8'))
        action_performed = True

    match_uno_var = re.search(r"\[UNO_VAR\((.*?)\|(.*?)\)\]", cleaned_text)
    if match_uno_var:
        var_name = match_uno_var.group(1).strip()
        var_data = match_uno_var.group(2).strip()
        
        data = load_data()
        data["jarvis_vars"][var_name] = var_data
        save_data(data)
        
        print(f"[UNO_VAR STORED] {var_name} = {var_data}")
        if arduino and arduino.is_open:
            arduino.write(f"[VAR({var_name}:{var_data})]\n".encode('utf-8'))
        action_performed = True

    match_alarm = re.search(r"\[SET_ALARM\((.*?)\|(.*?)\|(.*?)\)\]", cleaned_text)
    if match_alarm:
        alarm_time = match_alarm.group(1).strip()
        repeat_type = match_alarm.group(2).strip().lower()
        alarm_msg = match_alarm.group(3).strip()
        try:
            if repeat_type == "weekdays":
                for day in ["monday", "tuesday", "wednesday", "thursday", "friday"]:
                    getattr(schedule.every(), day).at(alarm_time).do(trigger_alarm, message=alarm_msg)
            elif repeat_type == "weekends":
                for day in ["saturday", "sunday"]:
                    getattr(schedule.every(), day).at(alarm_time).do(trigger_alarm, message=alarm_msg)
            elif repeat_type in ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]:
                getattr(schedule.every(), repeat_type).at(alarm_time).do(trigger_alarm, message=alarm_msg)
            elif repeat_type == "once":
                def one_time_alarm(msg=alarm_msg):
                    trigger_alarm(msg)
                    return schedule.CancelJob
                schedule.every().day.at(alarm_time).do(one_time_alarm)
            else:
                schedule.every().day.at(alarm_time).do(trigger_alarm, message=alarm_msg)
            save_alarm_to_data(alarm_time, repeat_type, alarm_msg)
        except Exception as e:
            print(f"[ALARM ERROR]: {e}")
        action_performed = True

    match_vol = re.search(r"\[ALARM_VOLUME\((\d+)\)\]", cleaned_text)
    if match_vol:
        os.system(f"amixer -q -D default sset Master {match_vol.group(1)}% unmute")
        action_performed = True

    match_song_play = re.search(r"\[SONG_PLAY\((.*?)\|(.*?)\)\]", cleaned_text)
    if match_song_play and sp:
        try:
            results = sp.search(q=f"track:{match_song_play.group(1).strip()} artist:{match_song_play.group(2).strip()}", type='track', limit=1)
            tracks = results['tracks']['items']
            if tracks:
                sp.start_playback(uris=[tracks[0]['uri']])
        except Exception as e:
            print(f"[SPOTIFY ERROR]: {e}")
        action_performed = True

    if "[SONG_PAUSE]" in cleaned_text and sp:
        try:
            sp.pause_playback()
        except Exception as e:
            print(f"[SPOTIFY ERROR]: {e}")
        action_performed = True

    if "[SONG_RESUME]" in cleaned_text and sp:
        try:
            sp.start_playback()
        except Exception as e:
            print(f"[SPOTIFY ERROR]: {e}")
        action_performed = True

    if "[SONG_SKIP]" in cleaned_text and sp:
        try:
            sp.next_track()
        except Exception as e:
            print(f"[SPOTIFY ERROR]: {e}")
        action_performed = True

    if "[PREV_SONG]" in cleaned_text and sp:
        try:
            sp.previous_track()
        except Exception as e:
            print(f"[SPOTIFY ERROR]: {e}")
        action_performed = True

    if "[REWIND_SONG]" in cleaned_text and sp:
        try:
            sp.seek_track(position_ms=0)
        except Exception as e:
            print(f"[SPOTIFY ERROR]: {e}")
        action_performed = True

    if "[SHUFFLE_SONG]" in cleaned_text and sp:
        try:
            sp.shuffle(state=True)
        except Exception as e:
            print(f"[SPOTIFY ERROR]: {e}")
        action_performed = True

    match_loop = re.search(r"\[LOOP_SONG\((.*?)\)\]", cleaned_text)
    if match_loop and sp:
        try:
            arg = match_loop.group(1).strip().lower()
            state = "track" if arg in ["true", "track"] else ("context" if arg == "context" else "off")
            sp.repeat(state=state)
        except Exception as e:
            print(f"[SPOTIFY ERROR]: {e}")
        action_performed = True

    match_playlist = re.search(r"\[PLAYLIST_PLAY\((.*?)\)\]", cleaned_text)
    if match_playlist and sp:
        try:
            query = match_playlist.group(1).strip()
            if "liked songs" in query.lower():
                res = sp.current_user_saved_tracks(limit=50)
                uris = [item['track']['uri'] for item in res['items']]
                if uris:
                    sp.start_playback(uris=uris)
            else:
                res = sp.search(q=query, type='playlist', limit=1)
                playlists = res['playlists']['items']
                if playlists:
                    sp.start_playback(context_uri=playlists[0]['uri'])
        except Exception as e:
            print(f"[SPOTIFY ERROR]: {e}")
        action_performed = True

    if "[SLEEP]" in cleaned_text:
        is_asleep = True
        print("[STATUS]: JARVIS is now asleep.")
        action_performed = True

    match_tab = re.search(r"\[OPEN_TAB\((.*?)\)\]", cleaned_text)
    if match_tab:
        webbrowser.open(match_tab.group(1))
        action_performed = True

    return action_performed

def shrink_memory(data):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }

    to_compress = data["messages"][:-WINDOW_SIZE]
    recent_msgs = data["messages"][-WINDOW_SIZE:]

    old_chat_text = "\n".join([f"{m['role']}: {m['content']}" for m in to_compress])
    existing_facts = data.get("compressed_memory", "")

    shrink_prompt = (
        "Merge the existing memory facts with any new permanent user preferences, names, "
        "reminders, or important facts from the recent conversation. Drop small talk, greetings, "
        "and redundant chatter. Output ONLY a dense, ultra-short semicolon-separated list of facts.\n\n"
        f"EXISTING MEMORY: {existing_facts}\n\n"
        f"NEW CONVERSATION TO SHRINK:\n{old_chat_text}"
    )

    payload = {
        "model": "openai/gpt-oss-120b",
        "messages": [{"role": "user", "content": shrink_prompt}]
    }

    try:
        res = requests.post(url, json=payload, headers=headers)
        if res.status_code == 200:
            data["compressed_memory"] = res.json()['choices'][0]['message']['content'].strip()
            data["messages"] = recent_msgs
            print(f"[MEMORY SHRUNK]: {data['compressed_memory']}")
    except Exception as e:
        print(f"[SHRINK ERROR]: {e}")

def ask_groq(prompt):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }

    data = load_data()
    now_iso = datetime.datetime.now().isoformat(timespec="seconds")

    data["messages"].append({
        "role": "user",
        "content": prompt,
        "timestamp": now_iso
    })

    system_content = data["system_prompt"]
    if data.get("compressed_memory"):
        system_content += f"\n\nLONG-TERM USER MEMORY: {data['compressed_memory']}"
    if data.get("jarvis_vars"):
        system_content += f"\nCurrent Local Variables: {json.dumps(data['jarvis_vars'])}"

    api_messages = [{"role": "system", "content": system_content}]
    for msg in data["messages"][-WINDOW_SIZE:]:
        api_messages.append({"role": msg["role"], "content": msg["content"]})

    payload = {
        "model": "openai/gpt-oss-120b",
        "messages": api_messages
    }

    try:
        response = requests.post(url, json=payload, headers=headers)
        if response.status_code == 200:
            content = response.json()['choices'][0]['message']['content']
            now = datetime.datetime.now().strftime("%I:%M %p")
            today = datetime.datetime.now().strftime("%A, %B %d, %Y")
            content = content.replace("[GET_TIME]", now).replace("[GET_DATE]", today)
            clean_reply = strip_emojis(content)

            data["messages"].append({
                "role": "assistant",
                "content": clean_reply,
                "timestamp": datetime.datetime.now().isoformat(timespec="seconds")
            })
            
            if len(data["messages"]) >= COMPRESS_THRESHOLD:
                shrink_memory(data)
            
            save_data(data)
            return clean_reply
    except Exception as e:
        print(f"[API EXCEPTION]: {e}")
    return "Apologies, sir, network error encountered."

def process_command_logic(command_text, source="voice"):
    global is_asleep
    
    if is_asleep:
        if "awaken" in command_text.lower() or "wake up" in command_text.lower():
            is_asleep = False
            msg = "Systems fully restored, sir."
            print(f"[STATUS]: JARVIS awakened via {source}.")
            if source == "voice":
                speak(msg)
            return msg
        return None

    if not command_text:
        msg = "Yes, sir?"
        if source == "voice":
            speak(msg)
        return msg

    if source == "voice" and handle_local_commands(command_text):
        return None

    if source == "voice":
        speak("Processing...")
    
    reply = ask_groq(command_text)
    print(f"[GROQ RAW]: {reply}")

    handle_ai_tags_silent(reply)
    
    final_text = re.sub(r"\[.*?\]", "", reply).strip()
    if not final_text:
        final_text = "Going to sleep, sir." if is_asleep else "Done, sir."

    if source == "voice":
        speak(final_text)

    return final_text

def voice_assistant_loop():
    global is_asleep
    import sounddevice as sd
    if not os.path.exists(MODEL_PATH):
        print(f"[ERROR]: Vosk model folder '{MODEL_PATH}' not found.")
        return

    model = Model(MODEL_PATH)
    rec = KaldiRecognizer(model, 16000)
    q = queue.Queue()

    def audio_callback(indata, frames, time, status):
        if not is_speaking:
            q.put(bytes(indata))

    speak("Voice systems online, sir.")

    with sd.RawInputStream(samplerate=16000, blocksize=8000, dtype='int16', channels=1, callback=audio_callback):
        while True:
            data = q.get()
            if rec.AcceptWaveform(data):
                result = json.loads(rec.Result())
                text = result.get("text", "").strip()
                if text:
                    print(f"[HEARD RAW]: {text}")
                    if "jarvis" in text.lower():
                        command = text.lower().replace("jarvis", "").strip()
                        process_command_logic(command, source="voice")

def main():
    voice_thread = threading.Thread(target=voice_assistant_loop, daemon=True)
    voice_thread.start()

    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()

    flask_thread = threading.Thread(target=run_flask_server, daemon=True)
    flask_thread.start()

    print("[INIT]: J.A.R.V.I.S. Voice, Automation, and Web Core online. Press Ctrl+C to shut down.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[SHUTDOWN]: Shutting down J.A.R.V.I.S., sir.")

if __name__ == "__main__":
    main()
