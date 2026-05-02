# Docker Desktop

Use this for local testing.

## Start

```bash
docker compose up --build -d
```

Open:

```text
http://127.0.0.1:48232
```

The app still listens on `48231` inside the container. Docker maps it to `48232` on your computer.

## Logs

```bash
docker compose logs -f
```

## Stop

```bash
docker compose down
```

## Reset Test Data

```bash
docker compose down -v
```
