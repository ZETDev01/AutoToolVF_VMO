# Cấu trúc project VinMotion voice client

Tài liệu này dùng để đọc nhanh project, nắm được file nào làm gì, luồng chạy như thế nào, và có sẵn phần hỏi đáp để bạn ôn lại.

## 1. Project này làm gì?

Project là một realtime voice/text client cho VinMotion/VinBDI. Chức năng chính:

- Kết nối WebSocket realtime tới backend `groot.vizone.ai`.
- Gửi câu hỏi dạng text tới backend và nhận câu trả lời.
- Mở Web UI để chat, import testcase, chạy testcase, chấm PASS/FAIL, xem log, export Excel.
- Mở TCP robot server để hệ thống khác gửi text vào port robot.
- Có module mic/loa/record audio sẵn, nhưng luồng hiện tại trong `main.py` đang ưu tiên text question/answer.

Entry point chính: `main.py`.

## 2. Cách chạy nhanh

Chạy trực tiếp:

```sh
python3 main.py
```

Chạy bằng script có tự tìm port và restart:

```sh
./run_project_server.sh
```

Mặc định:

- Web UI: `http://127.0.0.1:8080/`
- TCP robot server: `0.0.0.0:9000`
- Thoát chương trình: nhập `q` trong terminal đang chạy `main.py`
- Wake/reconnect realtime thủ công: nhập `w`

## 3. Cây thư mục rút gọn

```txt
.
├── main.py
├── run_project_server.sh
├── requirements.txt
├── README.md
├── RUN_COMMANDS.md
├── details.md
├── PROJECT_STRUCTURE.md
├── src/
│   ├── common.py
│   ├── voice_client.py
│   ├── outputs.py
│   ├── mic.py
│   ├── speaker.py
│   ├── session_recorder.py
│   └── wuw_cli.py
├── web/
│   ├── testcase_evaluator.js
│   └── testcase_evaluator.test.js
├── scripts/
│   └── proxy_auth.py
├── image/
│   └── logo_vinfastvinfast.png
├── logs/
│   └── sessions/
├── runs/
│   └── <timestamp>/
│       ├── audio_in.wav
│       └── audio_out.wav
└── result_*.xlsx
```

Ghi chú:

- `logs/`, `runs/`, `result_*.xlsx` là dữ liệu sinh ra trong quá trình chạy/test.
- `.venv/`, `__pycache__/` là môi trường/cache local, không phải logic project.

## 4. File gốc làm gì?

| File | Vai trò |
| --- | --- |
| `main.py` | Entry point. Tạo config realtime, khởi động VoiceClient, HTTP Web UI, TCP robot server, terminal input, import/export Excel, log session. |
| `run_project_server.sh` | Script chạy server tiện lợi: tự chọn port rảnh, probe health, xử lý proxy auth, restart lại nếu server yêu cầu exit code restart. |
| `requirements.txt` | Dependency Python: `openai[realtime]`, `pyaudio`, `numpy`, `black`, `isort`. |
| `README.md` | Hướng dẫn cài đặt/chạy ngắn gọn. |
| `RUN_COMMANDS.md` | Các lệnh chạy, test API, test TCP, đổi port, cấu hình timeout. |
| `details.md` | Mô tả chi tiết hiện trạng project và các endpoint/lưu ý. |
| `PROJECT_STRUCTURE.md` | File bạn đang đọc: bản đồ project và Q&A. |

## 5. Thư mục `src/`

| File | Chức năng |
| --- | --- |
| `src/voice_client.py` | Lớp `VoiceClient`: quản lý kết nối realtime WebSocket, reconnect, gửi text/audio, nhận event từ backend. |
| `src/outputs.py` | `TextOut` gom text/audio transcript thành response hoàn chỉnh; tạo Future để Web UI chờ câu trả lời. `ActionOut` in action từ backend. |
| `src/session_recorder.py` | Tạo thư mục `runs/<timestamp>/` và ghi `audio_in.wav`, `audio_out.wav`. |
| `src/common.py` | Hằng số audio và object PyAudio global: PCM16, chunk size, sample rate 16000. |
| `src/mic.py` | Mở microphone, đọc chunk audio và gọi callback. Hiện đang chưa bật trong `main.py`. |
| `src/speaker.py` | Mở speaker, phát audio response qua queue. Hiện đang chưa bật trong `main.py`. |
| `src/wuw_cli.py` | Wake-up-word client/model. Nhận audio chunk, chạy model `.wuw`, gọi callback khi phát hiện wake word. |

## 6. Thư mục `web/`

| File | Chức năng |
| --- | --- |
| `web/testcase_evaluator.js` | Logic chấm testcase phía browser: normalize tiếng Việt, bỏ dấu, tách keyword, so khớp ngày/tháng/số, expected/forbidden keyword, trả PASS/FAIL. |
| `web/testcase_evaluator.test.js` | Test bằng Node.js cho evaluator, đảm bảo các case số, ngày tháng, keyword, forbidden keyword hoạt động đúng. |

Lưu ý: HTML/CSS/JS chính của Web UI không nằm riêng trong `web/`, mà đang được nhúng trực tiếp trong biến `WEB_UI_HTML` của `main.py`.

## 7. Luồng chạy tổng quát

```txt
Người dùng / Web UI / TCP client
        |
        v
main.py
        |
        |-- /ask hoặc TCP line hoặc terminal input
        v
send_text_and_wait()
        |
        v
VoiceClient.send_text()
        |
        v
Realtime backend groot.vizone.ai
        |
        v
VoiceClient.run() nhận event
        |
        v
TextOut gom delta thành output
        |
        v
HTTP response / log / testcase result
```

## 8. Luồng khởi động trong `main.py`

1. Tạo config realtime:
   - `voice`: `N_M02_TuanDuong`
   - `modalities`: `["audio"]`
   - `domain`: `robot`
   - `sample_rate`: `16000`
   - `robot_type`: `ambassador`
2. Tạo `VoiceClient(config, timeout=120)`.
3. Tạo `SessionRecorder(base_dir="runs")`.
4. Tạo `TextOut()` và `ActionOut()`.
5. Gắn output handler vào `voice_cli`.
6. Tạo task nền `voice_cli.run()`.
7. Tạo task `keep_realtime_warm()` để giữ/reconnect realtime.
8. Gọi `voice_cli.connect()` để đánh thức kết nối.
9. Mở TCP server theo `VINFAST_ROBOT_HOST`/`VINFAST_ROBOT_PORT`.
10. Mở HTTP Web UI theo `VINFAST_WEB_HOST`/`VINFAST_WEB_PORT`.
11. Vào terminal loop `message >`.

## 9. Realtime backend

Kết nối được cấu hình trong `src/voice_client.py`:

```txt
wss://groot.vizone.ai/api/v2/s2s/realtime?model=vsf&device_id=robot_03072026_official_qcd
```

Biến quan trọng:

- `REALTIME_BASE_URL`
- `REALTIME_MODEL`
- `REALTIME_DEVICE_ID`
- `REALTIME_API_KEY`

`REALTIME_API_KEY` lấy từ biến môi trường `VINFAST_REALTIME_API_KEY`.

Event quan trọng:

| Event | Ý nghĩa |
| --- | --- |
| `response.text.delta` | Text response đang trả về từng phần. |
| `response.audio_transcript.delta` | Transcript của audio response. |
| `response.audio.delta` | Audio bytes base64, chỉ phát nếu bật `AudioOut`. |
| `response.action.done` | Backend trả action. |
| `response.done` | Response hoàn tất, `TextOut.response_done()` sẽ resolve waiter cho Web UI. |
| `input_audio_buffer.speech_started` | Backend phát hiện bắt đầu nói. |
| `conversation.item.input_audio_transcription.completed` | Transcript input audio nếu bật mic. |

## 10. HTTP API trong Web UI server

HTTP server được viết trực tiếp bằng `asyncio.start_server`, không dùng Flask/FastAPI.

| Method | Path | Chức năng |
| --- | --- | --- |
| `GET` | `/` | Trả HTML Web UI từ `WEB_UI_HTML`. |
| `GET` | `/health` | Trả trạng thái realtime, reconnect count, last error, worker count. |
| `POST` | `/ask` | Nhận `{ "question": "...", "workerId": 0 }`, gửi tới realtime, trả `{ input, output }`. |
| `POST` | `/import-xlsx` | Đọc file `.xlsx`, parse testcase từ sheet đầu tiên. |
| `POST` | `/export-xlsx` | Export result workbook hoặc split workbook. |
| `GET` | `/web/testcase_evaluator.js` | Serve evaluator JS riêng. |
| `GET` | `/image/logo_vinfastvinfast.png` | Serve logo. |
| `GET` | `/log-sessions` | Liệt kê các session log. |
| `GET` | `/log-session?id=...` | Đọc một session log. |
| `POST` | `/log-session` | Ghi/cập nhật session log. |
| `POST` | `/log-session/append` | Thêm một log entry vào session log. |
| `DELETE` | `/log-session?id=...` | Xóa session log. |
| `POST` | `/realtime/restart` | Force reconnect realtime và chờ connected. |
| `POST` | `/server/restart` | Lên lịch restart process bằng exit code. |

Ví dụ gọi `/ask`:

```sh
curl -sS -X POST http://127.0.0.1:8080/ask \
  -H 'Content-Type: application/json' \
  --data '{"question":"Bạn là ai?"}'
```

## 11. TCP robot server

TCP server chạy trong `main.py`:

```txt
host: 0.0.0.0
port: 9000
```

Có thể đổi bằng:

```sh
VINFAST_ROBOT_HOST=0.0.0.0 VINFAST_ROBOT_PORT=9001 python3 main.py
```

Protocol:

- Client gửi text UTF-8, mỗi message kết thúc bằng `\n`.
- Server gọi `send_text_and_wait(text)`.
- Thành công trả `ok\n`.
- Lỗi trả `error: ...\n`.
- Gửi `q\n` thì server trả `bye\n` và đóng kết nối TCP đó.

Test nhanh:

```sh
printf 'Bạn là ai?\n' | nc 127.0.0.1 9000
```

## 12. Web UI có những màn hình nào?

Web UI nằm trong `WEB_UI_HTML` của `main.py`.

| Màn hình | Chức năng |
| --- | --- |
| `Chat` | Gửi câu hỏi tự do, xem response realtime, lưu log chat. |
| `Testcases` | Import testcase từ text/file, tìm kiếm, chạy từng case, chạy tất cả, chạy theo range, chạy nhiều luồng. |
| `Kết quả` | Xem kết quả đã chạy và export Excel. |
| `Logs` | Xem log session, lọc theo case, export log text, xóa log. |

Tính năng testcase:

- Import `.txt`, `.json`, `.csv`, `.xlsx`.
- Parse Excel bằng Python trong `/import-xlsx`.
- Lưu testcase/result/log session info vào `localStorage`.
- Chạy 1 luồng hoặc nhiều luồng.
- Retry/restart khi realtime lỗi hoặc không trả PASS/FAIL.
- Export result workbook `.xlsx`.
- Split testcase ra workbook sạch, không kèm cột runtime/result/log.

## 13. Chấm PASS/FAIL testcase

Logic chính nằm ở `web/testcase_evaluator.js`.

Thứ tự check:

1. Nếu có `expected_keywords`, ưu tiên check keyword.
2. Nếu không có keyword, check `expected_response`.
3. Check `forbidden_keywords`; nếu thấy keyword cấm thì FAIL.
4. Hỗ trợ match linh hoạt:
   - bỏ dấu tiếng Việt;
   - bỏ/làm lỏng dấu câu;
   - số có dấu phân cách hàng nghìn như `350,000` và `350.000`;
   - chuỗi chữ số có hoặc không có khoảng trắng/dấu phân cách như `1900232389` và `1900 23 23 89`;
   - ngày tháng dạng `19/5/1890` và `ngày 19 tháng 5 năm 1890`;
   - giờ dạng `10pm`, `10:00 pm`, `22 giờ`, `22:00`;
   - alias cụm từ như `quê hương` -> `quê`;
   - token subset và semantic coverage có giới hạn.

Kết quả trả về:

```js
{
  status: 'PASS' | 'FAIL',
  result: 'PASS' | 'FAIL: ...',
  details: { missing, blocked, matched }
}
```

## 14. Import/export Excel

Trong `main.py`:

| Hàm | Chức năng |
| --- | --- |
| `read_xlsx_testcases(data)` | Đọc workbook `.xlsx`, lấy sheet đầu, map header thành record testcase. |
| `build_result_xlsx(records, logs)` | Tạo file result Excel có sheet `Test Results`, và thêm sheet `Execution Logs` nếu có log. |
| `build_plain_xlsx(records, sheet_name)` | Tạo file Excel sạch dùng cho split testcase. |
| `result_filename(source_name)` | Đặt tên file result theo ngày và tên source. |
| `split_filename(source_name)` | Đặt tên file split theo ngày và tên source. |

Các cột result chính:

- `case_id`
- `step_id`
- `Type`
- `Language`
- `prompt_text`
- `expected_response`
- `expected_keywords`
- `forbidden_keywords`
- `RunTest`
- `log_session_id`
- `date`
- `actual_response`
- `test results`

## 15. Log session

Log session lưu trong:

```txt
logs/sessions/<session_id>.json
logs/sessions/<session_id>.txt
```

Hàm liên quan trong `main.py`:

| Hàm | Chức năng |
| --- | --- |
| `_log_session_paths()` | Tạo đường dẫn `.json` và `.txt`. |
| `_read_log_session()` | Đọc log session JSON. |
| `_write_log_session()` | Ghi log session JSON và render bản `.txt`. |
| `_append_log_session()` | Thêm một entry vào log session. |
| `_list_log_sessions()` | Liệt kê log session hiện có. |
| `_delete_log_session()` | Xóa log session. |

Một log entry thường có:

- `session_id`
- `time`
- `case_id`
- `step`
- `event`
- `message`
- `prompt`
- `actual_response`
- `test_result`

## 16. Biến môi trường quan trọng

| Biến | Mặc định | Ý nghĩa |
| --- | --- | --- |
| `VINFAST_WEB_HOST` | `0.0.0.0` | Host của Web UI server. |
| `VINFAST_WEB_PORT` | `8080` | Port Web UI. |
| `VINFAST_ROBOT_HOST` | `0.0.0.0` | Host TCP robot server. |
| `VINFAST_ROBOT_PORT` | `9000` | Port TCP robot server. |
| `VINFAST_REALTIME_API_KEY` | rỗng | API key realtime. |
| `VINFAST_REALTIME_WAKE_INTERVAL` | `30` | Số giây mỗi lần keep-warm realtime. |
| `VINFAST_REALTIME_STALE_SECONDS` | `90` | Nếu realtime im lặng quá ngưỡng này thì force reconnect. |
| `VINFAST_REALTIME_WAKE_TIMEOUT` | `60` | Thời gian chờ realtime kết nối lại. |
| `VINFAST_REALTIME_REQUEST_TIMEOUT` | `120` | Thời gian chờ phản hồi cho mỗi lần gửi câu hỏi. |
| `VINFAST_REALTIME_REQUEST_RETRIES` | `3` | Số lần tự reconnect và gửi lại khi request bị timeout/rớt kết nối. |
| `VINFAST_RESTART_EXIT_CODE` | `42` | Exit code để `run_project_server.sh` restart server. |
| `VINFAST_PROXY_USERNAME` | rỗng | Username proxy công ty nếu cần auth. |
| `VINFAST_PROXY_PASSWORD` | prompt nếu rỗng | Password proxy công ty. |
| `AUDIO_INPUT_DEVICE_INDEX` | auto/fallback | Chọn microphone trong `src/mic.py`. |
| `AUDIO_OUTPUT_DEVICE_INDEX` | auto/fallback | Chọn speaker trong `src/speaker.py`. |

## 17. Những điểm cần lưu ý khi sửa code

- `main.py` đang rất lớn vì gom cả backend HTTP, TCP, Excel, log, và HTML/JS inline. Sửa UI cần tìm trong `WEB_UI_HTML`.
- `VoiceClient.run()` là loop kết nối/reconnect realtime. Nếu sửa retry/reconnect, cần đọc kỹ `connect()`, `wait_connected()`, `keep_realtime_warm()`.
- `TextOut.prepare_response_waiter()` và `response_done()` là cầu nối giữa event realtime và `/ask`. Nếu response không về, Web UI sẽ timeout.
- Audio mic/loa đang comment trong `main.py`; bật lại cần gắn `AudioIn`, `AudioOut`, callback record và task run.
- API key phải được set qua `VINFAST_REALTIME_API_KEY`, không hardcode trong repo.
- `run_project_server.sh` có cơ chế restart khi process exit code `42`; endpoint `/server/restart` được sinh ra để dùng với cơ chế này.

## 18. Câu hỏi - đáp nhanh

### Project này có phải Flask/FastAPI không?

Không. HTTP server được viết trực tiếp bằng `asyncio.start_server()` trong `main.py`.

### File nào là file chạy chính?

`main.py`.

### UI nằm ở đâu?

HTML/CSS/JS chính nằm trong biến `WEB_UI_HTML` của `main.py`. Riêng logic chấm testcase nằm trong `web/testcase_evaluator.js`.

### Gửi câu hỏi từ Web UI đi đâu?

Web UI gọi `POST /ask`, sau đó `main.py` gọi `send_text_and_wait()`, tiếp theo `VoiceClient.send_text()` gửi message qua WebSocket realtime.

### Câu trả lời được lấy về như thế nào?

`VoiceClient.run()` nhận các event `response.text.delta` hoặc `response.audio_transcript.delta`, đẩy vào `TextOut`. Khi có `response.done`, `TextOut.response_done()` resolve Future đang chờ trong `/ask`.

### TCP robot server để làm gì?

Để chương trình/robot khác gửi text vào project qua TCP port `9000`, mỗi dòng là một câu hỏi.

### Tại sao có nhiều thư mục trong `runs/`?

Mỗi lần tạo `SessionRecorder`, project tạo một thư mục timestamp để ghi `audio_in.wav` và `audio_out.wav`.

### Tại sao mic và loa có code nhưng không nghe/nói?

Trong `main.py`, các dòng khởi tạo/chạy `AudioIn` và `AudioOut` đang bị comment. Hiện tại mode đang dùng là text.

### Chạy testcase chấm PASS/FAIL ở đâu?

Browser dùng `web/testcase_evaluator.js` để chấm output với `expected_response`, `expected_keywords`, `forbidden_keywords`.

### Log testcase lưu ở đâu?

Trong `logs/sessions/`, mỗi session có một file `.json` để đọc lại trên UI và một file `.txt` để xem nhanh/export.

### Nếu port 8080 hoặc 9000 bận thì làm sao?

Dùng `run_project_server.sh` để tự tìm port rảnh, hoặc set biến môi trường:

```sh
VINFAST_WEB_PORT=8081 VINFAST_ROBOT_PORT=9001 python3 main.py
```

### Nếu realtime bị timeout thì sao?

`keep_realtime_warm()` sẽ kiểm tra định kỳ cho cả worker chính và batch worker. `/ask` cũng gọi `ensure_realtime_ready()` trước khi gửi câu hỏi; nếu request timeout hoặc socket rớt, backend sẽ force reconnect và tự gửi lại theo `VINFAST_REALTIME_REQUEST_RETRIES`.

### Muốn restart realtime bằng UI/API thì gọi gì?

Gọi:

```sh
curl -sS -X POST http://127.0.0.1:8080/realtime/restart
```

### Muốn restart cả server thì sao?

Nếu đang chạy qua `run_project_server.sh`, gọi:

```sh
curl -sS -X POST http://127.0.0.1:8080/server/restart
```

Server sẽ exit code `42`, script sẽ khởi động lại.

## 19. Cách đọc code theo thứ tự để hiểu nhanh

1. Đọc `README.md` để biết cách chạy.
2. Đọc `main.py` phần đầu file: constants, Excel/log helpers.
3. Nhảy đến `async def main()` trong `main.py` để xem luồng khởi động.
4. Đọc `src/voice_client.py` để hiểu realtime.
5. Đọc `src/outputs.py` để hiểu cách gom response.
6. Đọc `web/testcase_evaluator.js` để hiểu cách chấm testcase.
7. Đọc `run_project_server.sh` để hiểu cách deploy local/restart/proxy.
