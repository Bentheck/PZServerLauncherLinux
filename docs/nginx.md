# Nginx

Example only. The app listens on the VPS at `127.0.0.1:48231`; Nginx exposes it publicly.

## Domain

```nginx
server {
    server_name your-domain.example;

    location / {
        proxy_pass http://127.0.0.1:48231;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Use Let's Encrypt separately.

## IP Only

```nginx
server {
    listen 80 default_server;
    server_name _;

    location / {
        proxy_pass http://127.0.0.1:48231;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Use `http://SERVER_IP`. For `https://SERVER_IP`, configure your own certificate.
