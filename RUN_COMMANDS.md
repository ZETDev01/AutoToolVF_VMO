# Lenh chay he thong VinMotion

File nay chi gom cac lenh de ban tu chay bang terminal. Khong co lenh nao tu dong mo server neu ban chua tu go.

## 1. Di chuyen vao thu muc project

```sh
cd "/home/huyvt11/Documents/VinMotion/vinmotion-conv_voice_client 1"
```

## 2. Cai dependency he thong neu may chua co PortAudio

```sh
sudo apt install portaudio19-dev
```

## 3. Tao va kich hoat moi truong Python

```sh
python3 -m venv .venv
```

```sh
. .venv/bin/activate
```

```sh
pip install -r requirements.txt
```

## 4. Chay he thong voi port duoc chi dinh bang lenh

Chay voi port mac dinh cua project:

```sh
VINFAST_WEB_HOST=0.0.0.0 VINFAST_WEB_PORT=8080 VINFAST_ROBOT_HOST=0.0.0.0 VINFAST_ROBOT_PORT=9000 python3 main.py
```
Mo Web UI:
```txt
http://127.0.0.1:8080/
```
TCP robot server:
```txt
127.0.0.1:9000
```

## 5. Chay bang port khac neu port 8080 hoac 9000 dang ban

Vi du doi Web UI sang port `8081` va TCP robot sang port `9001`:

```sh
VINFAST_WEB_HOST=0.0.0.0 VINFAST_WEB_PORT=8081 VINFAST_ROBOT_HOST=0.0.0.0 VINFAST_ROBOT_PORT=9001 python3 main.py
```

Mo Web UI:

```txt
http://127.0.0.1:8081/
```

TCP robot server:

```txt
127.0.0.1:9001
```

## 6. Tuy chinh thoi gian realtime khi chay

```sh
VINFAST_WEB_HOST=0.0.0.0 VINFAST_WEB_PORT=8080 VINFAST_ROBOT_HOST=0.0.0.0 VINFAST_ROBOT_PORT=9000 VINFAST_REALTIME_WAKE_INTERVAL=30 VINFAST_REALTIME_STALE_SECONDS=90 VINFAST_REALTIME_WAKE_TIMEOUT=60 VINFAST_REALTIME_REQUEST_TIMEOUT=120 VINFAST_REALTIME_REQUEST_RETRIES=3 python3 main.py
```

Y nghia:

- `VINFAST_REALTIME_WAKE_INTERVAL`: so giay moi lan kiem tra giu realtime.
- `VINFAST_REALTIME_STALE_SECONDS`: neu realtime im lang qua so giay nay thi reconnect.
- `VINFAST_REALTIME_WAKE_TIMEOUT`: thoi gian cho reconnect realtime.
- `VINFAST_REALTIME_REQUEST_TIMEOUT`: so giay cho moi lan gui cau hoi toi realtime.
- `VINFAST_REALTIME_REQUEST_RETRIES`: so lan tu reconnect va gui lai cau hoi khi request bi timeout/rot ket noi.

## 7. Kiem tra port truoc khi chay

Kiem tra port `8080` va `9000`:

```sh
ss -ltnp | grep -E ':8080|:9000'
```

Neu khong co output thi port dang ranh.

## 8. Test Web API bang terminal

Kiem tra health:

```sh
curl -sS http://127.0.0.1:8080/health
```

Gui cau hoi toi Web API:

```sh
curl -sS -X POST http://127.0.0.1:8080/ask -H 'Content-Type: application/json' --data '{"question":"Ban la ai?"}'
```

Neu ban chay Web UI port khac, doi `8080` thanh port da chon.

## 9. Test TCP robot server bang terminal

```sh
printf 'Ban la ai?\n' | nc 127.0.0.1 9000
```

Neu ban chay TCP robot port khac, doi `9000` thanh port da chon.

## 10. Dung he thong

Tai terminal dang chay `main.py`, nhap:

```txt
q
```

roi nhan `Enter`.

Co the dung `Ctrl+C` neu can tat nhanh.

## 11. Chay lai nhanh sau khi da cai dependency

```sh
cd "/home/huyvt11/Documents/VinMotion/vinmotion-conv_voice_client 1"
```

```sh
. .venv/bin/activate
```

```sh
VINFAST_WEB_HOST=0.0.0.0 VINFAST_WEB_PORT=8080 VINFAST_ROBOT_HOST=0.0.0.0 VINFAST_ROBOT_PORT=9000 python3 main.py
```
