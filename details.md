# VinMotion realtime voice client

## Mục đích

Project này là client Python dùng để test gửi câu hỏi text tới realtime backend của VinMotion/VinBDI. File chạy chính là `main.py`.

Khi chạy, chương trình đồng thời mở:

- Terminal input: nhập tại prompt `message >`.
- Web GUI VinFast: `http://127.0.0.1:8080/`.
- TCP robot server: `0.0.0.0:9000`.
- WebSocket realtime tới backend `groot.vizone.ai`.
// request #2 
json: {robo_type:#4,input: "Bạn là ai", send_event: 200_OK, status: OK, timeo_out = 200ms}


Hiện tại mic và loa đã có module sẵn nhưng đang bị comment trong `main.py`, nên chế độ đang dùng chính là text question/answer.

## Công nghệ

- Ngôn ngữ: Python 3.
- Async runtime: `asyncio`.
- Realtime client: `openai[realtime]`.
- Audio I/O: `pyaudio`.
- Audio buffer: `numpy`.
- GUI: HTML/CSS/JavaScript được nhúng trực tiếp trong `main.py`, serve bằng HTTP server tự viết bằng `asyncio`.
- Không dùng framework web ngoài như Flask/FastAPI.

Dependency nằm trong `requirements.txt`:

```txt
openai[realtime]
pyaudio
numpy
black
isort
```

## Cách chạy

Không cần active venv nếu dependency đã cài trong user-site:

```sh
python3 main.py
```

Sau khi chạy thành công sẽ thấy:

```txt
>>> Robot TCP server running port 9000
>>> VinFast test UI running http://127.0.0.1:8080
message >
```
Mở GUI:

```txt
http://127.0.0.1:8080/
```
Nhập câu hỏi rồi:

- Nhấn `Enter` để gửi.
- Nhấn `Shift+Enter` để xuống dòng trong ô nhập.
- Hoặc bấm nút `Gửi`.

Muốn tắt chương trình: nhập `q` ở terminal đang chạy `main.py`.

## Web GUI VinFast

GUI nằm trong biến `WEB_UI_HTML` của `main.py`.

Giao diện có:

- Logo VinFast dạng SVG wordmark sát thực tế hơn, không còn icon `VF` tự vẽ.
- Navigation dạng tab: `Chat`, `Testcases`, `Kết quả`.
- Ô nhập câu hỏi.
- Nút `Gửi`.
- Hỗ trợ `Enter` gửi, `Shift+Enter` xuống dòng.
- Danh sách câu hỏi nhanh.
- Import prompt testcase từ textarea hoặc file `.txt`, `.json`, `.csv`.
- Chạy từng testcase hoặc chạy lần lượt toàn bộ testcase.
- Lưu testcases và kết quả trong `localStorage` của browser.
- Khu vực hiển thị câu hỏi và phản hồi.
- Trạng thái kết nối realtime.
- Nếu realtime connection đang ngủ do timeout, `/ask` sẽ tự gọi `voice_cli.connect()` để wakeup lại trước khi gửi câu hỏi.

### Navigation

- `Chat`: nhập câu hỏi tự do, xem hội thoại realtime.
- `Testcases`: import prompt testcase, quản lý danh sách testcase, bấm `Gửi`, `Đưa vào chat`, `Xóa`, hoặc `Chạy tất cả`.
- `Kết quả`: xem lại prompt đã chạy và output nhận được.

### Import testcase

Có thể import theo từng dòng:

```txt
Bạn là ai?
VinFast có những dòng xe nào?
Hãy giới thiệu ngắn gọn về VF 8.
```

Hoặc đặt tên testcase bằng dấu `|`:

```txt
TC giới thiệu | Bạn là ai?
TC VinFast | VinFast có những dòng xe nào?
```

Hoặc import JSON array:

```json
[
  {"name":"Giới thiệu","prompt":"Bạn là ai?"},
  {"name":"VinFast models","prompt":"VinFast có những dòng xe nào?"}
]
```

JSON object cũng được hỗ trợ nếu có key `testcases`, `prompts`, hoặc `items`.

Các endpoint HTTP:

```txt
GET  /
GET  /health
POST /ask
```

`GET /health` trả trạng thái kết nối:

```json
{"connected":true}
```

`POST /ask` nhận body:

```json
{"question":"Bạn là ai?"}
```

Response:

```json
{"input":"Bạn là ai?","output":"..."}
```

Test bằng terminal:

```sh
curl -sS -X POST http://127.0.0.1:8080/ask \
  -H 'Content-Type: application/json' \
  --data '{"question":"Bạn là ai?"}'
```

## TCP robot server port 9000

TCP server nhận text theo từng dòng UTF-8:

```py
server = await asyncio.start_server(handle_robot_tcp, "0.0.0.0", 9000)
```

Protocol:

- Client kết nối tới port `9000`.
- Mỗi message kết thúc bằng `\n`.
- Server gọi `voice_cli.send_text(text)`.
- Server trả `ok\n`.
- Nếu client gửi `q\n`, server trả `bye\n` và chỉ đóng client TCP đó.

Test local:

```sh
printf 'Bạn là ai?\n' | nc 127.0.0.1 9000
```

## Luồng xử lý chính

1. `main.py` tạo config session:
   - `voice`: `N_M02_TuanDuong`
   - `modalities`: `["audio"]`
   - `domain`: `robot`
   - `sample_rate`: `16000`
   - `robot_type`: `ambassador`

2. Tạo `VoiceClient(config, timeout=120)`.

3. Tạo `SessionRecorder(base_dir="runs")`.

4. Gắn output handler:
   - `voice_cli.text_out = TextOut()`
   - `voice_cli.action_out = ActionOut()`

5. Chạy task nền `voice_cli.run()`.

6. Gọi `voice_cli.connect()` để đánh thức client realtime.

7. Mở TCP server port `9000`.

8. Mở Web GUI port `8080`.

9. `VoiceClient.startup()` gửi config session và gửi câu `xin chào`.

10. Người dùng gửi text qua terminal, GUI, hoặc TCP.

11. Nếu GUI gửi câu hỏi lúc `voice_cli.conn` đang rỗng, `send_text_and_wait()` tự wakeup realtime và chờ connection được tạo lại.

12. Mỗi lần WebSocket reconnect, `VoiceClient.run()` gửi lại `session.update` để backend có config session. Câu `xin chào` chỉ gửi ở lần startup đầu tiên.

13. Khi realtime backend trả `response.done`, `TextOut.response_done()` gom output hoàn chỉnh.

14. Với GUI, `/ask` chờ future từ `TextOut.prepare_response_waiter()` rồi trả JSON về browser.

## Cấu trúc file

- `main.py`: entrypoint, GUI HTML, HTTP server, TCP server, terminal loop, shutdown.
- `src/voice_client.py`: kết nối realtime WebSocket, gửi text/audio, nhận event.
- `src/outputs.py`: gom response text/transcript, cung cấp future để GUI đợi câu trả lời.
- `src/mic.py`: mở microphone bằng PyAudio, hiện chưa bật trong `main.py`.
- `src/speaker.py`: phát audio bằng PyAudio, hiện chưa bật trong `main.py`.
- `src/session_recorder.py`: tạo thư mục `runs/<timestamp>/` và file wav.
- `src/common.py`: hằng số audio và object PyAudio global.
- `src/wuw_cli.py`: wake-up-word client, chưa dùng trong luồng hiện tại.

## Realtime backend

`src/voice_client.py` kết nối tới:

```txt
wss://groot.vizone.ai/api/v2/s2s/realtime?model=vsf&device_id=robot_03072026_official_qcd
```

Các event quan trọng:

- `session.created`, `session.updated`: backend xác nhận session.
- `response.text.delta`: text trả lời từng phần.
- `response.audio_transcript.delta`: transcript của audio response.
- `response.audio.delta`: audio bytes base64, chỉ phát nếu bật `AudioOut`.
- `response.done`: response hoàn tất.
- `response.action.done`: action từ backend.
- `conversation.item.input_audio_transcription.completed`: transcript input audio nếu bật mic.

## Các lưu ý đang tồn tại

1. `api_key` đang hardcode trong `src/voice_client.py`. Nên chuyển sang biến môi trường nếu dùng lâu dài.

2. `main.py` vẫn import `src.mic`, nên máy phải có `pyaudio` dù mic đang comment.

3. PyAudio có thể in cảnh báo ALSA/PulseAudio khi chạy. Với chế độ text hiện tại, các cảnh báo này không làm hỏng GUI.

4. `pip install --user -r requirements.txt` từng nâng `typing-extensions` lên `4.15.0`, có thể xung đột với TensorFlow 2.13.0 trên cùng Python hệ thống. Dùng venv riêng sẽ sạch hơn nếu cần ổn định lâu dài.

5. `sudo apt-get update` từng lỗi do repo ngoài project:

```txt
https://librealsense.intel.com/Debian/apt-repo jammy InRelease
NO_PUBKEY FB0B24895113F120
```

Lỗi này thuộc cấu hình apt của máy, không thuộc project.

## Muốn bật mic và loa

Trong `main.py`, các dòng này đang bị comment:

```py
# audio_in = AudioIn()
# audio_out = AudioOut()
# audio_in.callbacks.append(send_audio_with_record)
# audio_out.send_callbacks.append(recorder.write_audio_out)
# voice_cli.audio_out = audio_out
# t_audio_out = asyncio.create_task(audio_out.run())
# atask = asyncio.create_task(audio_in.run())
# await audio_out.wait_ready()
```

Nếu bật lại, có thể chọn device bằng biến môi trường:

```sh
AUDIO_INPUT_DEVICE_INDEX=12 AUDIO_OUTPUT_DEVICE_INDEX=12 python3 main.py
```

Nếu không set biến môi trường, code ưu tiên device có tên chứa `pipewire`, `pulse`, `default`, rồi fallback index `12`.
