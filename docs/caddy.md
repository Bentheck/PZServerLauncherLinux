# Caddy

Example only. The app listens on the VPS at `127.0.0.1:48231`; Caddy exposes it publicly.

## Domain

```caddy
your-domain.example {
    reverse_proxy 127.0.0.1:48231
}
```

Caddy can manage Let's Encrypt when DNS points to the VPS.

## IP Only

```caddy
http://SERVER_IP {
    reverse_proxy 127.0.0.1:48231
}
```

For `https://SERVER_IP`, configure your own certificate.
