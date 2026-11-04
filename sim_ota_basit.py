import time
import os
import board
import busio
import digitalio
import microcontroller

VERSION = "1.0"   # OTA sonrasi degisti mi diye bakmak icin: guncelleyince 2.0 yap

# ---------- AYARLAR ----------
DEVICE_ID   = "1"
APN         = "internet"

MQTT_BROKER = "j01fcece.ala.eu-central-1.emqxsl.com"
MQTT_PORT   = 8883
MQTT_USER   = "ilker"
MQTT_PASS   = "Deneme"
CLIENT_ID   = "anka_sim_" + DEVICE_ID

OTA_URL           = "https://raw.githubusercontent.com/KULLANICI/REPO/main/code.py"
TOPIC_OTA_COMMAND = "otacommand" + DEVICE_ID
TOPIC_OTA_STATUS  = "otastatus" + DEVICE_ID

# ---------- DONANIM ----------
uart = busio.UART(
    board.GP16, board.GP17,
    baudrate=115200, timeout=0.1,
    receiver_buffer_size=2048
)

sim_reset = digitalio.DigitalInOut(board.GP26)
sim_reset.direction = digitalio.Direction.OUTPUT


# ---------- AT KOMUT YARDIMCILARI ----------
def read_response(timeout=5, expected=None):
    buf = ""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if uart.in_waiting:
            buf += uart.read(uart.in_waiting).decode("utf-8", "replace")
            if expected:
                if expected in buf:
                    return buf, True
            elif "OK" in buf:
                return buf, True
            if "ERROR" in buf:
                return buf, False
        time.sleep(0.01)
    return buf, None


def send(cmd, timeout=5, expected=None):
    print(">>", cmd)
    uart.write((cmd + "\r\n").encode())
    resp, status = read_response(timeout, expected)
    print(resp.strip(), "[", status, "]")
    return status, resp


def must(cmd, timeout=5, expected=None):
    status, resp = send(cmd, timeout, expected)
    if status is not True:
        raise Exception("Komut basarisiz: " + cmd)
    return resp


def mqtt_publish(topic, payload):
    try:
        data = payload.encode()
        uart.write('AT+CMQTTPUB=0,"{}",1,{},0\r\n'.format(topic, len(data)).encode())
        _, st = read_response(5, expected=">")
        if st:
            uart.write(data)
            read_response(5, expected="+CMQTTPUB: 0,0")
    except Exception as e:
        print("Publish hatasi:", e)


# ---------- MODEM + MQTT BAGLANTISI ----------
def connect_all():
    sim_reset.value = False
    time.sleep(1)
    sim_reset.value = True
    time.sleep(5)

    for _ in range(10):
        status, _ = send("AT", 3)
        if status:
            break
        time.sleep(1)
    else:
        raise Exception("Modem cevap vermiyor")

    send("ATE0", 3)
    send('AT+CGDCONT=1,"IP","{}"'.format(APN), 5)
    send("AT+CGACT=1,1", 10)

    must('AT+CSSLCFG="sslversion",0,3', 5)
    must('AT+CSSLCFG="authmode",0,0', 5)
    must('AT+CSSLCFG="ignorelocaltime",0,1', 5)
    must('AT+CSSLCFG="enableSNI",0,1', 5)

    send("AT+CMQTTDISC=0,120", 3)
    send("AT+CMQTTREL=0", 3)
    send("AT+CMQTTSTOP", 3)

    must("AT+CMQTTSTART", 5)
    must('AT+CMQTTACCQ=0,"{}",1'.format(CLIENT_ID), 5)
    must("AT+CMQTTSSLCFG=0,0", 3)
    must('AT+CMQTTCFG="argtopic",0,1,1', 3)
    must(
        'AT+CMQTTCONNECT=0,"tcp://{}:{}",60,1,"{}","{}"'.format(
            MQTT_BROKER, MQTT_PORT, MQTT_USER, MQTT_PASS),
        20, expected="+CMQTTCONNECT: 0,0"
    )
    must('AT+CMQTTSUB=0,"{}",1'.format(TOPIC_OTA_COMMAND), 8, expected="+CMQTTSUB: 0,0")
    print("MQTT hazir, dinleniyor:", TOPIC_OTA_COMMAND)


# ---------- OTA ----------
def http_read(offset, n, timeout=10):
    while uart.in_waiting:          # eski artik veriyi temizle
        uart.read(uart.in_waiting)
    uart.write("AT+HTTPREAD={},{}\r\n".format(offset, n).encode())
    raw = b""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if uart.in_waiting:
            raw += uart.read(uart.in_waiting)
        i = raw.find(b"+HTTPREAD:")
        if i >= 0:
            lf = raw.find(b"\n", i)
            if lf >= 0:
                try:
                    length = int(raw[i + 10:lf].strip().split(b",")[-1])
                except ValueError:
                    length = 0
                s = lf + 1
                if length > 0 and len(raw) - s >= length:
                    return raw[s:s + length]
        time.sleep(0.01)
    return None


def ota_update(url):
    print("OTA basliyor:", url)
    try:
        mqtt_publish(TOPIC_OTA_STATUS, "OTA:BASLIYOR")

        # SIM7672E'de MQTT ve HTTP ayni anda calismaz: MQTT'yi kapat
        send("AT+CMQTTDISC=0,120", 5)
        send("AT+CMQTTREL=0", 3)
        send("AT+CMQTTSTOP", 3)

        send("AT+HTTPTERM", 3)
        time.sleep(0.3)
        must("AT+HTTPINIT", 5)
        must('AT+HTTPPARA="SSLCFG",0', 3)
        must('AT+HTTPPARA="URL","{}"'.format(url), 5)

        uart.write(b"AT+HTTPACTION=0\r\n")
        buf = ""
        http_code, total = 0, 0
        start = time.monotonic()
        while time.monotonic() - start < 60:
            if uart.in_waiting:
                buf += uart.read(uart.in_waiting).decode("utf-8", "replace")
                if "+HTTPACTION:" in buf:
                    parts = buf.split("+HTTPACTION:")[1].split("\r")[0].split(",")
                    if len(parts) >= 3:
                        http_code = int(parts[1])
                        total = int(parts[2])
                        break
            time.sleep(0.02)

        print("HTTP kod:", http_code, "boyut:", total)
        if http_code != 200 or total == 0:
            raise Exception("HTTP hata: {}".format(http_code))

        # Parca parca indir, dosyaya yaz
        offset = 0
        with open("/code_new.py", "wb") as f:
            while offset < total:
                n = min(1024, total - offset)
                data = http_read(offset, n)
                if not data:
                    raise Exception("Okuma hatasi, offset={}".format(offset))
                f.write(data)
                offset += len(data)
                print("Indirildi:", offset, "/", total)

        send("AT+HTTPTERM", 3)

        if os.stat("/code_new.py")[6] != total:
            raise Exception("Boyut uyusmuyor")

        # Yedekle ve uygula
        try:
            os.remove("/code_bak.py")
        except Exception:
            pass
        try:
            os.rename("/code.py", "/code_bak.py")
        except Exception:
            pass
        os.rename("/code_new.py", "/code.py")

        print("OTA tamam, reboot...")
        time.sleep(1)
        microcontroller.reset()

    except Exception as e:
        print("OTA hatasi:", e)
        try:
            os.remove("/code_new.py")
        except Exception:
            pass
        time.sleep(2)
        microcontroller.reset()   # MQTT kapali, reboot ile temiz baslasin


# ---------- ANA PROGRAM ----------
try:
    connect_all()
except Exception as e:
    print("Baglanti hatasi:", e)
    time.sleep(5)
    microcontroller.reset()

rx = ""
ota_pending = False
last_print = time.monotonic()

while True:
    now = time.monotonic()

    if now - last_print >= 5:
        last_print = now
        print("Calisiyor... versiyon:", VERSION)

    if uart.in_waiting:
        rx += uart.read(uart.in_waiting).decode("utf-8", "replace")
        while "\r\n" in rx:
            line, rx = rx.split("\r\n", 1)

            if line.startswith("+CMQTTRECV"):
                parts = line.split(",")
                topic = parts[1].replace('"', "")
                payload = ",".join(parts[3:]).strip().strip('"')
                print("Gelen:", topic, "->", payload)
                if topic == TOPIC_OTA_COMMAND and payload == "UPDATE":
                    ota_pending = True

            elif "+CMQTTCONNLOST" in line:
                print("MQTT baglantisi koptu, reboot")
                time.sleep(1)
                microcontroller.reset()

    # OTA, UART okuma bittikten sonra ayri calistirilir (cakisma olmasin)
    if ota_pending:
        ota_pending = False
        ota_update(OTA_URL)

    time.sleep(0.01)
