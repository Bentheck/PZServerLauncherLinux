# Nginx Reverse Proxy

These snippets are examples only. They are meant to be adapted into an operator-managed Nginx setup, not copied as a guaranteed full server config.

The application itself should stay on:

`127.0.0.1:48231`

Nginx becomes the public surface.

## Domain Example

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

Pair this with Let's Encrypt for the clean production setup.

## IP-Only Example

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

This supports:

- `http://SERVER_IP`

If you want `https://SERVER_IP`, use a self-signed certificate or an IP-capable certificate from your provider.
