# Deploy Docker

## Chay local

```sh
docker compose up --build
```

Web UI:

```txt
http://127.0.0.1:8080/
```

Neu may dang ban port `8080` hoac `9000`, doi port host:

```sh
VINFAST_WEB_PUBLISHED_PORT=18080 VINFAST_ROBOT_PUBLISHED_PORT=19000 docker compose up --build
```

Web UI khi do:

```txt
http://127.0.0.1:18080/
```

Health:

```sh
curl -sS http://127.0.0.1:8080/health
```

## Chay tren VPS

```sh
docker compose up -d --build
```

Mo firewall/security group cho port `8080` neu can truy cap Web UI tu ngoai mang.
Neu can robot TCP server, mo them port `9000`.

## Chay tren cloud co bien PORT

Mot so nen tang cloud cap port dong qua bien `PORT`. Image nay tu dong gan `VINFAST_WEB_PORT=$PORT` khi `PORT` ton tai.

Neu nen tang cho cau hinh bien moi truong, giu:

```txt
VINFAST_WEB_HOST=0.0.0.0
VINFAST_REALTIME_MODALITIES=text
```

## Luu y

- Web UI/API chay tot trong Docker.
- Micro Python backend trong container cloud thuong khong co thiet bi micro vat ly; UI co fallback mic browser.
- Du lieu trong `logs/` va `runs/` chi ben vung khi mount volume nhu `docker-compose.yml`.
- Khong public app ra Internet neu chua co authentication hoac firewall allowlist.
